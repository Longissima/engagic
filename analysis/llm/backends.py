"""Provider-neutral chat transport for the summarizer.

Architectural decision (2026-09-08): the summarizer owns prompts, effort
tiering, and response parsing. Everything provider-specific -- request shape,
retry semantics, token accounting, pricing -- lives behind ChatBackend so the
model follows price x quality with no provider allegiance. A swap is a config
change, not a rewrite.

OpenRouter is the bench: one request shape reaches every open and closed
model, provider pinning keeps a quality number honest (no silent quantized
mirror), and usage accounting returns the real cost per call. It also
throttles harder than a native key, so once a model is established there,
production volume moves to the provider's own endpoint (ZaiBackend today).
The native Gemini backend stays for rollback and the Gemini-only Batch lane.
"""

import json
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

import requests
from google import genai
from google.genai import types

from config import config, get_logger
from exceptions import LLMError

logger = get_logger(__name__).bind(component="llm_backend")

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ZAI_ENDPOINT = "https://api.z.ai/api/paas/v4/chat/completions"

# Effort tiers are provider-neutral names; each backend maps them to its own
# knob (OpenRouter reasoning.effort, Gemini thinkingLevel/thinkingBudget).
EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
EFFORT_LEVELS = (EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH)

# USD per 1M tokens, used only when the provider does not report cost itself
# (OpenRouter does; Z.AI and Gemini do not). Confidence 7/10: list prices on
# 2026-09-08. GLM-5.3-flash is billed at half list through 2026-09-09; the
# table carries list so the estimate never undershoots after the promo.
# Tuple is (input, output, cached input). Z.AI prefix caching is implicit
# and bills cached prefix tokens at a fifth of list; the prompt is laid out
# so the instruction block is that prefix.
MODEL_PRICING_PER_MILLION: Dict[str, tuple[float, float, float]] = {
    "z-ai/glm-5.3-flash": (0.15, 0.50, 0.03),
    "glm-5.3-flash": (0.15, 0.50, 0.03),
    "glm-5.3": (1.40, 4.40, 0.26),
    "google/gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-3.1-flash-lite-preview": (0.25, 1.50, 0.025),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01),
    "gemini-2.5-flash": (0.30, 2.50, 0.03),
}

_RETRYABLE_HTTP = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


@dataclass
class Completion:
    """One finished chat call, in provider-neutral terms."""

    text: str
    model: str
    provider: Optional[str]
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    cost_usd: Optional[float]
    finish_reason: Optional[str]
    latency_seconds: float


class ChatBackend(Protocol):
    """Everything the summarizer needs from a provider, and nothing else."""

    name: str
    model: str
    supports_context_cache: bool
    supports_batch: bool

    def complete(
        self,
        *,
        user: str,
        system: Optional[str],
        schema: Optional[dict],
        effort: str,
        max_tokens: int,
        temperature: float,
    ) -> Completion: ...

    def count_tokens(self, text: str) -> int: ...

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> float: ...


def strip_code_fence(text: str) -> str:
    """Open models sometimes wrap JSON in a markdown fence despite a schema."""
    return _FENCE.sub("", text)


# GLM in json_object mode occasionally escapes the delimiters of strings
# inside a top-level array: "topics": [\"budget\"]. Everything else in the
# document is valid, so repair that one shape rather than lose the summary.
_ESCAPED_ARRAY_RE = re.compile(r'("topics"\s*:\s*\[)(.*?)(\])', re.S)
# Unescaped double quotes inside the summary string (the model wrote
# "benefitted" raw). The field boundaries are unambiguous: it opens after the
# key and closes right before the topics key, so quotes between them can be
# escaped without touching anything else.
_SUMMARY_FIELD_RE = re.compile(r'("summary_markdown"\s*:\s*")(.*?)("\s*,\s*"topics"\s*:)', re.S)
_UNESCAPED_QUOTE_RE = re.compile(r'(?<!\\)"')


def _load_first_object(candidate: str) -> Any:
    """json.loads, falling back to the first complete value in the text.

    Repair 4 (trailing extra data) cannot go through the brace-trimming path:
    when the junk is itself a brace, rfind("}") lands on the junk and the trim
    is a no-op. raw_decode stops at the end of the first complete value, so it
    handles a duplicate "}" and any other trailing garbage alike. If the model
    ever emits two real objects, the first one is the one we want.
    """
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        if start < 0:
            raise
        return json.JSONDecoder().raw_decode(candidate, start)[0]


def parse_json_lenient(text: str) -> Any:
    """json.loads with four targeted repairs for known open-model quirks.

    1. Escaped delimiters inside the topics array ("topics": [\\"budget\\"]).
    2. Raw double quotes inside the summary string.
    3. Stray characters outside the object (a lone backtick after the closing
       brace, a preamble before the opening one). The object itself is kept
       byte-for-byte; only what lies outside its outermost braces is cut.
    4. Extra data after the closing brace, including a duplicate "}".

    GLM is asked for json_object mode, which promises only that the output is
    JSON, never that it matches the schema we inline into the system message;
    Z.AI's native endpoint offers no json_schema mode to tighten that. So the
    repair list grows with the observed malformation distribution rather than
    shrinking. Confidence that raw_decode subsumes future trailing-junk
    shapes: 8/10.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        first_error = exc
    candidates = [text]

    def repair_fields(candidate: str) -> str:
        candidate = _ESCAPED_ARRAY_RE.sub(
            lambda m: m.group(1) + m.group(2).replace('\\"', '"') + m.group(3), candidate
        )
        return _SUMMARY_FIELD_RE.sub(
            lambda m: m.group(1) + _UNESCAPED_QUOTE_RE.sub('\\\\"', m.group(2)) + m.group(3),
            candidate,
        )

    repaired = repair_fields(text)
    if repaired != text:
        candidates.append(repaired)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start and (start > 0 or end < len(text.rstrip()) - 1):
        trimmed = text[start : end + 1]
        candidates.append(trimmed)
        trimmed_repaired = repair_fields(trimmed)
        if trimmed_repaired != trimmed:
            candidates.append(trimmed_repaired)
    for candidate in candidates:
        try:
            return _load_first_object(candidate)
        except json.JSONDecodeError:
            continue
    raise first_error


def price_estimate(
    model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0
) -> float:
    rates = MODEL_PRICING_PER_MILLION.get(model)
    if rates is None:
        # Unknown model: charge nothing rather than invent a number. Metrics
        # consumers treat 0 as "unpriced".
        return 0.0
    input_rate, output_rate, cached_rate = rates
    cached = min(cached_tokens, input_tokens)
    return (
        ((input_tokens - cached) / 1_000_000) * input_rate
        + (cached / 1_000_000) * cached_rate
        + (output_tokens / 1_000_000) * output_rate
    )


def _retry_delay(attempt: int, error_text: str, rate_limited: bool) -> float:
    if rate_limited:
        match = re.search(r"retry[^0-9]{0,40}(\d+(?:\.\d+)?)\s*s", error_text, re.IGNORECASE)
        if match:
            return float(match.group(1)) + 1.0
        return 30.0 * (attempt + 1)
    return (2 ** (attempt + 1)) + random.uniform(0, 1)


class OpenAICompatibleBackend:
    """Shared HTTP transport for chat-completions APIs.

    Subclasses own the endpoint, headers, and provider-specific body shaping
    (reasoning knobs, structured-output dialect). Retry, parsing, and token
    accounting are identical across providers, so they live here once.
    """

    name = "openai_compatible"
    endpoint = ""
    supports_context_cache = False
    supports_batch = False
    # Providers that only offer json_object mode get the schema inlined into
    # the system message instead; the summarizer's parser validates anyway.
    supports_json_schema = True

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 300.0,
        max_retries: int = 4,
        max_retry_seconds: int = 180,
        max_inflight: int = 0,
    ):
        if not api_key:
            raise ValueError(f"API key required for the {self.name} backend")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_retry_seconds = max_retry_seconds
        # One session per backend: connection reuse across a job's many calls.
        # requests.Session is thread-safe for concurrent requests.
        self.session = requests.Session()
        # Provider concurrency ceilings are per key (Z.AI: 50 for
        # GLM-5.3-Flash). JOB_CONCURRENCY x LLM_CONCURRENCY can exceed that,
        # so the backend holds the real cap; callers block here instead of
        # burning retries on 429s. 0 disables.
        self._inflight = threading.BoundedSemaphore(max_inflight) if max_inflight > 0 else None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _shape(self, body: Dict[str, Any], *, effort: str, schema: Optional[dict]) -> None:
        """Provider-specific fields; mutate body in place."""
        raise NotImplementedError

    def _body(
        self,
        *,
        user: str,
        system: Optional[str],
        schema: Optional[dict],
        effort: str,
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
        if schema is not None and not self.supports_json_schema:
            schema_note = (
                "Respond with a single JSON object matching this JSON Schema exactly:\n"
                + json.dumps(schema)
            )
            system = f"{system}\n\n{schema_note}" if system else schema_note
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            # Reasoning tokens count against max_tokens on every provider we
            # use; the summarizer sizes this so a long think cannot starve
            # the answer.
            "max_tokens": max_tokens,
        }
        self._shape(body, effort=effort, schema=schema)
        return body

    def complete(
        self,
        *,
        user: str,
        system: Optional[str],
        schema: Optional[dict],
        effort: str,
        max_tokens: int,
        temperature: float,
    ) -> Completion:
        body = self._body(
            user=user,
            system=system,
            schema=schema,
            effort=effort,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        started = time.monotonic()
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries):
            try:
                response = self._post(body)
                if response.status_code in _RETRYABLE_HTTP:
                    raise _TransientError(
                        f"HTTP {response.status_code}: {response.text[:300]}",
                        rate_limited=response.status_code == 429,
                    )
                if response.status_code != 200:
                    raise LLMError(
                        f"{self.name} HTTP {response.status_code}: {response.text[:600]}",
                        model=self.model,
                        prompt_type="unknown",
                    )
                data = response.json()
                if "error" in data:
                    error = data["error"]
                    code = error.get("code") if isinstance(error, dict) else None
                    message = json.dumps(error)[:600]
                    if code in _RETRYABLE_HTTP:
                        raise _TransientError(f"provider error: {message}", rate_limited=code == 429)
                    raise LLMError(
                        f"{self.name} provider error: {message}",
                        model=self.model,
                        prompt_type="unknown",
                    )
                return self._parse(data, started)
            except (_TransientError, requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
                rate_limited = getattr(exc, "rate_limited", False)
                delay = _retry_delay(attempt, str(exc), rate_limited)
                elapsed = time.monotonic() - started
                if attempt + 1 >= self.max_retries or elapsed + delay > self.max_retry_seconds:
                    break
                logger.warning(
                    "transient provider error, retrying",
                    backend=self.name,
                    model=self.model,
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    delay_seconds=round(delay, 1),
                    error=str(exc)[:200],
                )
                time.sleep(delay)

        elapsed = time.monotonic() - started
        raise LLMError(
            f"{self.name} retries exhausted after {round(elapsed)}s",
            model=self.model,
            prompt_type="unknown",
            original_error=last_error,
        )

    def _post(self, body: Dict[str, Any]) -> requests.Response:
        if self._inflight is None:
            return self.session.post(
                self.endpoint, json=body, headers=self._headers(), timeout=self.timeout_seconds
            )
        with self._inflight:
            return self.session.post(
                self.endpoint, json=body, headers=self._headers(), timeout=self.timeout_seconds
            )

    def _parse(self, data: Dict[str, Any], started: float) -> Completion:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(
                f"{self.name} returned no choices: {json.dumps(data)[:400]}",
                model=self.model,
                prompt_type="unknown",
            )
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        finish_reason = choice.get("finish_reason")
        usage = data.get("usage") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cost = usage.get("cost")
        if not content.strip():
            # A reasoning model that spent its whole budget thinking returns
            # an empty answer with finish_reason=length. Surface that plainly.
            raise LLMError(
                f"{self.name} returned empty content (finish_reason={finish_reason}, "
                f"completion_tokens={usage.get('completion_tokens')})",
                model=self.model,
                prompt_type="unknown",
            )
        return Completion(
            text=content,
            model=data.get("model") or self.model,
            provider=data.get("provider") or self.name,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            cost_usd=float(cost) if cost is not None else None,
            finish_reason=finish_reason,
            latency_seconds=time.monotonic() - started,
        )

    def count_tokens(self, text: str) -> int:
        # No tokenizer endpoint. Three chars per token is pessimistic for
        # English prose on GLM/Qwen tokenizers, which is the right direction
        # for a ceiling check.
        return max(1, len(text) // 3)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> float:
        return price_estimate(self.model, input_tokens, output_tokens, cached_tokens)


class OpenRouterBackend(OpenAICompatibleBackend):
    """OpenRouter: the bake-off bench. One request shape reaches every model,
    provider pinning keeps a number honest, usage accounting returns real
    cost. It throttles harder than a native key, so production volume moves
    to the provider once a model is established there.
    """

    name = "openrouter"
    endpoint = OPENROUTER_ENDPOINT

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        provider_order: tuple[str, ...] = (),
        provider_sort: Optional[str] = None,
        quantizations: tuple[str, ...] = (),
        allow_fallbacks: bool = True,
        app_title: str = "engagic",
        **kwargs: Any,
    ):
        super().__init__(api_key, model, **kwargs)
        self.provider_order = tuple(provider_order)
        # sort="throughput" spreads load across every mirror (a single
        # first-party endpoint can throttle a key to a few calls a minute);
        # the quantization floor stops a fast mirror from serving a degraded
        # model. Motioncount's scale test settled on throughput + fp8 floor.
        self.provider_sort = provider_sort
        self.quantizations = tuple(quantizations)
        self.allow_fallbacks = allow_fallbacks
        self.app_title = app_title

    def provider_policy(self) -> Dict[str, Any]:
        policy: Dict[str, Any] = {"allow_fallbacks": self.allow_fallbacks}
        if self.provider_sort:
            policy["sort"] = self.provider_sort
        elif self.provider_order:
            policy["order"] = list(self.provider_order)
        if self.quantizations:
            policy["quantizations"] = list(self.quantizations)
        return policy

    def _headers(self) -> Dict[str, str]:
        headers = super()._headers()
        headers["HTTP-Referer"] = "https://engagic.org"
        headers["X-Title"] = self.app_title
        return headers

    def _shape(self, body: Dict[str, Any], *, effort: str, schema: Optional[dict]) -> None:
        body["usage"] = {"include": True}
        body["provider"] = self.provider_policy()
        if effort in EFFORT_LEVELS:
            body["reasoning"] = {"effort": effort, "exclude": True}
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": False, "schema": schema},
            }


class ZaiBackend(OpenAICompatibleBackend):
    """Native Z.AI (GLM) endpoint for production volume.

    GLM-5.3-flash cannot disable thinking; reasoning_effort takes low/high/max
    (default max, which we never want for a summary). Structured output is
    json_object only, so the schema rides in the system message.
    """

    name = "zai"
    endpoint = ZAI_ENDPOINT
    supports_json_schema = False

    _EFFORT_MAP = {EFFORT_LOW: "low", EFFORT_MEDIUM: "high", EFFORT_HIGH: "high"}

    def _shape(self, body: Dict[str, Any], *, effort: str, schema: Optional[dict]) -> None:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = self._EFFORT_MAP.get(effort, "low")
        if schema is not None:
            body["response_format"] = {"type": "json_object"}


class _TransientError(Exception):
    def __init__(self, message: str, *, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


class GeminiBackend:
    """Native google-genai transport. Kept for rollback and the Batch lane."""

    name = "gemini"
    supports_context_cache = True
    supports_batch = True

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 300.0,
        max_retries: int = 4,
        max_retry_seconds: int = 180,
    ):
        if not api_key:
            raise ValueError("GEMINI_API_KEY required for the gemini backend")
        self.model = model
        self.max_retries = max_retries
        self.max_retry_seconds = max_retry_seconds
        # The SDK-level timeout closes the socket on a stall; asyncio.wait_for
        # around a to_thread call cannot cancel the thread on its own.
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )

    def thinking_config(self, effort: str) -> Optional[types.ThinkingConfig]:
        """Gemini 3.x takes thinking_level, 2.5 takes thinking_budget."""
        is_gemini3 = "gemini-3" in self.model
        if effort == EFFORT_LOW:
            if is_gemini3:
                return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL)
            return types.ThinkingConfig(thinking_budget=0)
        if effort == EFFORT_MEDIUM:
            if is_gemini3:
                return types.ThinkingConfig(thinking_level=types.ThinkingLevel.MEDIUM)
            return types.ThinkingConfig(thinking_budget=2048)
        if effort == EFFORT_HIGH:
            if is_gemini3:
                return types.ThinkingConfig(thinking_level=types.ThinkingLevel.HIGH)
            return types.ThinkingConfig(thinking_budget=-1)
        return None

    def complete(
        self,
        *,
        user: str,
        system: Optional[str],
        schema: Optional[dict],
        effort: str,
        max_tokens: int,
        temperature: float,
    ) -> Completion:
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            system_instruction=system or None,
            response_mime_type="application/json" if schema is not None else None,
            response_schema=schema,
            thinking_config=self.thinking_config(effort),
        )
        started = time.monotonic()
        response = self._call_with_retry(user, config)
        text = response.text or self._text_from_candidates(response)
        if not text:
            raise LLMError(
                "Gemini returned no text",
                model=self.model,
                prompt_type="unknown",
            )
        usage = getattr(response, "usage_metadata", None)
        return Completion(
            text=text,
            model=self.model,
            provider="google",
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            reasoning_tokens=int(getattr(usage, "thoughts_token_count", 0) or 0),
            cached_tokens=int(getattr(usage, "cached_content_token_count", 0) or 0),
            cost_usd=None,
            finish_reason=self._finish_reason(response),
            latency_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _finish_reason(response: Any) -> Optional[str]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        reason = getattr(candidates[0], "finish_reason", None)
        return str(reason) if reason is not None else None

    @staticmethod
    def _text_from_candidates(response: Any) -> Optional[str]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", False):
                return text
        return None

    def _call_with_retry(self, prompt: str, config: types.GenerateContentConfig):
        """Retry 429/5xx with Gemini's own retryDelay when it offers one."""
        last_error: Optional[Exception] = None
        started = time.time()
        for attempt in range(self.max_retries):
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
            except Exception as exc:  # Intentionally broad: retry classification
                last_error = exc
                error_text = str(exc)
                rate_limited = "429" in error_text or "RESOURCE_EXHAUSTED" in error_text
                server_busy = any(
                    marker in error_text
                    for marker in ("503", "UNAVAILABLE", "500", "INTERNAL", "504", "DEADLINE_EXCEEDED")
                )
                if not (rate_limited or server_busy):
                    raise
                delay = _retry_delay(attempt, error_text, rate_limited)
                elapsed = time.time() - started
                if elapsed + delay > self.max_retry_seconds:
                    break
                logger.warning(
                    "transient gemini error, retrying",
                    reason="rate_limit" if rate_limited else "server_busy",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    delay_seconds=round(delay, 1),
                )
                time.sleep(delay)
        elapsed = time.time() - started
        raise LLMError(
            f"Transient-error retries exhausted after {round(elapsed)}s",
            model=self.model,
            prompt_type="unknown",
            original_error=last_error,
        )

    def count_tokens(self, text: str) -> int:
        response = self.client.models.count_tokens(model=self.model, contents=text)
        total = getattr(response, "total_tokens", None)
        if total is None:
            raise ValueError("Gemini token counter returned no total_tokens")
        return int(total)

    def estimate_cost(
        self, input_tokens: int, output_tokens: int, cached_tokens: int = 0
    ) -> float:
        return price_estimate(self.model, input_tokens, output_tokens, cached_tokens)


def build_backend(api_key: Optional[str] = None) -> ChatBackend:
    """Construct the transport named by config.LLM_BACKEND.

    api_key overrides the configured key for that backend; callers that
    predate the backend split pass whatever config.get_api_key() returned,
    which is already backend-aware.
    """
    if config.LLM_BACKEND == "gemini":
        return GeminiBackend(
            api_key or config.GEMINI_API_KEY or config.LLM_API_KEY or "",
            config.PRIMARY_MODEL,
        )
    if config.LLM_BACKEND == "zai":
        return ZaiBackend(
            api_key or config.ZAI_API_KEY or "",
            config.PRIMARY_MODEL,
            max_inflight=config.LLM_MAX_INFLIGHT,
        )
    return OpenRouterBackend(
        api_key or config.OPENROUTER_API_KEY or "",
        config.PRIMARY_MODEL,
        provider_order=config.OPENROUTER_PROVIDER_ORDER,
        provider_sort=config.OPENROUTER_PROVIDER_SORT,
        quantizations=config.OPENROUTER_QUANTIZATIONS,
        max_inflight=config.LLM_MAX_INFLIGHT,
    )
