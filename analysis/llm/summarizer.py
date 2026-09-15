"""
LLM orchestration: prompt management, effort tiering, response parsing.

Responsibilities:
- Load prompts from prompts.json
- Pick reasoning effort from document size
- Drive the configured ChatBackend (analysis/llm/backends.py)
- Gemini Batch lane (native backend only)
- Parse and validate responses
"""

import asyncio
import hashlib
import json
import os
import random
import re
import tempfile
import time
from importlib.resources import files
from json import JSONDecodeError
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from google.genai import types

from config import config, get_logger
from analysis.llm.input_budget import (
    limit_item_title,
    limit_shared_context,
    prepare_item_text,
)
from analysis.llm.document_representation import (
    build_compact_representation,
    needs_proactive_representation,
)
from analysis.llm.backends import (
    EFFORT_HIGH,
    EFFORT_LEVELS,
    EFFORT_LOW,
    EFFORT_MEDIUM,
    ChatBackend,
    Completion,
    build_backend,
    parse_json_lenient,
    price_estimate,
    strip_code_fence,
)
from pipeline.protocols import MetricsCollector, NullMetrics
from exceptions import LLMError

logger = get_logger(__name__).bind(component="analyzer")

# Model thresholds
FLASH_LITE_MAX_CHARS = 200000  # Use Flash-Lite for documents under ~200K chars
FLASH_LITE_MAX_PAGES = 50  # Or under 50 pages

# The google-genai client used here is synchronous. Keep every Batch/Files/
# Caches call behind one bounded async boundary so provider I/O cannot stall
# the event loop or grow the default thread pool without limit.
BATCH_SDK_CONCURRENCY = 4
BATCH_SUBMIT_CONCURRENCY = 2

# Gemini 3.1 Flash-Lite accepts 1,048,576 input tokens. Large extracted
# documents are preflighted with the provider tokenizer and held below the
# published ceiling so small differences between cached and inline framing do
# not turn into per-line INVALID_ARGUMENT failures. Smaller requests skip the
# extra API round trip; even a pessimistic one-token-per-character estimate
# leaves them comfortably inside the guarded limit.
GEMINI_INPUT_TOKEN_LIMIT = 1_048_576
BATCH_INPUT_TOKEN_LIMIT = GEMINI_INPUT_TOKEN_LIMIT - 48_576
BATCH_TOKEN_PREFLIGHT_CHARS = 500_000

_PERMANENT_PROVIDER_ERROR_CODES = frozenset({3, 5, 7, 9, 11, 12, 16})
_PERMANENT_PROVIDER_ERROR_STATUSES = frozenset(
    {
        "INVALID_ARGUMENT",
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "FAILED_PRECONDITION",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "UNAUTHENTICATED",
    }
)
_SAFETY_FINISH_REASONS = frozenset(
    {
        "SAFETY",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "RECITATION",
    }
)



class Summarizer:
    """LLM orchestrator: prompts, effort tiering, response parsing.

    Transport lives in analysis/llm/backends.py. The class is named for its
    job, not its provider; GeminiSummarizer remains as an import alias.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        prompts_path: Optional[str] = None,
        metrics: Optional[MetricsCollector] = None,
        backend: Optional[ChatBackend] = None,
    ):
        """Initialize summarizer

        Args:
            api_key: Provider API key (defaults to config for the configured backend)
            prompts_path: Path to prompts.json (defaults to same directory)
            metrics: Metrics collector for LLM call tracking (uses NullMetrics if not provided)
            backend: Explicit transport; built from config when omitted
        """
        self.metrics = metrics or NullMetrics()
        self.backend: ChatBackend = backend or build_backend(api_key)
        # Gemini-only surfaces (Batch lane, context caches, native tokenizer)
        # reach the SDK through this handle; None on every other transport.
        self.client = getattr(self.backend, "client", None)
        self._batch_sdk_semaphore = asyncio.Semaphore(BATCH_SDK_CONCURRENCY)
        self._batch_submit_semaphore = asyncio.Semaphore(BATCH_SUBMIT_CONCURRENCY)

        # Model ids are backend-native. primary = default workhorse;
        # small_doc = Gemini-only cost-saver when USE_FLASH_LITE + small input.
        self.primary_model = self.backend.model
        self.small_doc_model = config.SMALL_DOC_MODEL

        # Load prompts from JSON. Version bumps when the template materially
        # changes -- items carry prompts_version so stale summaries are
        # queryable for backfill (v3.1: status-aware transactional policy;
        # v3.2: policy broadened to legislative redlines, fiscal
        # characterizations, and internal-conflict reconciliation;
        # v3.3: model swap to GLM-5.3-flash, privacy floor for private
        # individuals' names and home addresses, sentence budget enforced).
        # v3.4: explicit jurisdiction/body context and attachment availability.
        self.prompts_version = "v3.4"

        if prompts_path is None:
            # Load from package resources (works in installed packages)
            prompts_text = files("analysis.llm").joinpath("prompts_v3.json").read_text()
            self.prompts = json.loads(prompts_text)
        else:
            with open(prompts_path, "r") as f:
                self.prompts = json.load(f)

        logger.info(
            "prompts loaded",
            prompt_categories=len(self.prompts),
            version=self.prompts_version,
            backend=self.backend.name,
            model=self.backend.model,
        )

    async def _run_batch_sdk(self, call: Callable[..., Any], /, *args, **kwargs):
        """Run one synchronous Batch/Files/Caches SDK call off-loop."""
        async with self._batch_sdk_semaphore:
            return await asyncio.to_thread(call, *args, **kwargs)

    @staticmethod
    def _provider_error_metadata(error_data: Any) -> Dict[str, Any]:
        """Normalize one provider error and classify an unchanged retry.

        Batch result files use canonical gRPC codes (for example code 3 for
        INVALID_ARGUMENT), while SDK exceptions sometimes expose only the
        symbolic status in their string form. Unknown failures remain
        retryable; only statuses that cannot improve without changing the
        request are terminal.
        """
        if isinstance(error_data, dict):
            code = error_data.get("code")
            status = error_data.get("status")
            message = error_data.get("message")
        else:
            code = getattr(error_data, "code", None)
            status = getattr(error_data, "status", None)
            message = getattr(error_data, "message", None)

        try:
            numeric_code = int(code) if code is not None else None
        except (TypeError, ValueError):
            numeric_code = None

        error_text = str(error_data)
        normalized_status = str(status or "").upper()
        if not normalized_status:
            normalized_status = next(
                (
                    marker
                    for marker in _PERMANENT_PROVIDER_ERROR_STATUSES
                    if marker in error_text.upper()
                ),
                "",
            )
        retryable = not (
            numeric_code in _PERMANENT_PROVIDER_ERROR_CODES
            or normalized_status in _PERMANENT_PROVIDER_ERROR_STATUSES
        )
        return {
            "error": error_text,
            "error_code": numeric_code,
            "error_status": normalized_status or None,
            "error_message": str(message) if message is not None else None,
            "retryable": retryable,
        }

    @staticmethod
    def _batch_response_diagnostics(response_data: Dict[str, Any]) -> Dict[str, Any]:
        """Retain bounded response metadata needed to explain empty output."""
        candidates = response_data.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        finish_reason = candidate.get("finishReason") or candidate.get(
            "finish_reason"
        )
        finish_message = candidate.get("finishMessage") or candidate.get(
            "finish_message"
        )
        prompt_feedback = response_data.get("promptFeedback") or response_data.get(
            "prompt_feedback"
        )
        safety_ratings = candidate.get("safetyRatings") or candidate.get(
            "safety_ratings"
        )
        usage_metadata = response_data.get("usageMetadata") or response_data.get(
            "usage_metadata"
        )
        diagnostics = {
            "finish_reason": str(finish_reason) if finish_reason else None,
            "finish_message": str(finish_message)[:1000] if finish_message else None,
            "prompt_feedback": prompt_feedback,
            "safety_ratings": safety_ratings,
            "usage_metadata": usage_metadata,
        }
        return {key: value for key, value in diagnostics.items() if value is not None}

    @staticmethod
    def _empty_response_is_retryable(diagnostics: Dict[str, Any]) -> bool:
        finish_reason = str(diagnostics.get("finish_reason") or "").upper()
        if finish_reason in _SAFETY_FINISH_REASONS or finish_reason == "MAX_TOKENS":
            return False
        feedback = diagnostics.get("prompt_feedback")
        if isinstance(feedback, dict):
            block_reason = feedback.get("blockReason") or feedback.get("block_reason")
            if block_reason and str(block_reason).upper() not in {
                "BLOCK_REASON_UNSPECIFIED",
                "NONE",
            }:
                return False
        return True

    async def _count_input_tokens(
        self,
        prompt: str,
        *,
        cached_context: Optional[str] = None,
    ) -> int:
        """Count one completed request with the backend tokenizer (or its estimate)."""
        count_input = (
            f"{cached_context}\n\n{prompt}" if cached_context else prompt
        )
        return int(await self._run_batch_sdk(self.backend.count_tokens, count_input))

    # Compatibility for narrow callers/tests that used the former private name.
    _count_batch_input_tokens = _count_input_tokens


    async def prepare_document_input(
        self,
        *,
        kind: str,
        text: str,
        documents: List[Any] | None = None,
        title: str = "",
        shared_context: Optional[str] = None,
        cache_name: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Prepare and preflight one document request before transport selection.

        Streaming, cached Batch API, inline Batch API, and packet fallbacks all
        enter through this boundary.  The returned ``_model_text`` is the exact
        text that belongs in the prompt; cached shared context is counted but is
        intentionally absent from that field because the provider attaches it.
        """
        if kind not in {"item", "meeting"}:
            raise ValueError(f"unsupported document input kind: {kind}")

        normalized_documents = list(documents or [])
        normalized_shared = limit_shared_context(shared_context)
        item_title = limit_item_title(title)
        source_text = text or ""
        self_contained = False
        adapters: tuple[str, ...] = ()
        proactive_representation = None

        if needs_proactive_representation(normalized_documents):
            proactive_representation = build_compact_representation(
                normalized_documents
            )
            if proactive_representation.compacted:
                source_text = proactive_representation.text
                self_contained = True
                adapters = proactive_representation.adapters
                logger.info(
                    "provider-compatible public-record representation applied",
                    request_id=request_id,
                    kind=kind,
                    adapters=adapters,
                    source_chars=proactive_representation.source_chars,
                    represented_chars=proactive_representation.represented_chars,
                )

        def assemble_model_text(candidate: str, contained: bool) -> str:
            if kind == "meeting":
                return candidate
            return prepare_item_text(
                item_title,
                candidate,
                None if contained else normalized_shared,
                inline_shared=not cache_name and not contained,
            )

        def render_prompt(model_text: str) -> str:
            if kind == "meeting":
                return self._get_prompt("meeting", "fallback", text=model_text)
            return self._get_prompt(
                "item",
                self._select_prompt_type(),
                title=item_title,
                text=model_text,
            )

        model_text = assemble_model_text(source_text, self_contained)
        prompt = render_prompt(model_text)
        cached_context = (
            normalized_shared if cache_name and not self_contained else None
        )
        system_chars = (
            len(self._system_instruction(self._select_prompt_type()) or "")
            if kind == "item"
            else 0
        )
        input_chars = len(prompt) + len(cached_context or "") + system_chars
        input_tokens = max(1, input_chars // 4)
        failure: Dict[str, Any] | None = None

        if input_chars >= BATCH_TOKEN_PREFLIGHT_CHARS:
            try:
                input_tokens = await self._count_input_tokens(
                    prompt,
                    cached_context=cached_context,
                )
                logger.info(
                    "large document request token preflight",
                    request_id=request_id,
                    kind=kind,
                    input_chars=input_chars,
                    input_tokens=input_tokens,
                    token_limit=BATCH_INPUT_TOKEN_LIMIT,
                )
            except Exception as exc:
                metadata = self._provider_error_metadata(exc)
                failure = {
                    **metadata,
                    "error": f"Gemini token preflight failed: {exc}",
                    "error_type": "token_preflight_failed",
                    "stage": "preflight",
                }

        needs_representation = input_tokens > BATCH_INPUT_TOKEN_LIMIT or (
            failure is not None and bool(normalized_documents)
        )
        if needs_representation:
            representation = proactive_representation or build_compact_representation(
                normalized_documents
            )
            if representation.compacted:
                compact_text = representation.text
                compact_model_text = assemble_model_text(compact_text, True)
                compact_prompt = render_prompt(compact_model_text)
                try:
                    compact_tokens = await self._count_input_tokens(compact_prompt)
                except Exception as exc:
                    metadata = self._provider_error_metadata(exc)
                    failure = {
                        **metadata,
                        "error": (
                            "Gemini compact-representation token preflight "
                            f"failed: {exc}"
                        ),
                        "error_type": "token_preflight_failed",
                        "stage": "representation_preflight",
                    }
                else:
                    failure = None
                    source_text = compact_text
                    model_text = compact_model_text
                    self_contained = True
                    adapters = representation.adapters
                    input_tokens = compact_tokens
                    logger.info(
                        "oversized request represented deterministically",
                        request_id=request_id,
                        kind=kind,
                        adapters=adapters,
                        source_chars=representation.source_chars,
                        represented_chars=representation.represented_chars,
                        represented_input_tokens=compact_tokens,
                    )

            if failure is None and input_tokens > BATCH_INPUT_TOKEN_LIMIT:
                attempted = f" after adapters {adapters}" if adapters else ""
                failure = {
                    "error": (
                        "Input has no compatible representation that fits: "
                        f"{input_tokens:,} input tokens exceeds the guarded "
                        f"{BATCH_INPUT_TOKEN_LIMIT:,}-token limit{attempted}"
                    ),
                    "error_type": "representation_required",
                    "retryable": False,
                    "stage": "preflight",
                    "input_tokens": input_tokens,
                    "token_limit": BATCH_INPUT_TOKEN_LIMIT,
                }

        prepared: Dict[str, Any] = {
            "text": source_text,
            "_model_text": model_text,
            "_batch_input_tokens": input_tokens,
            "_representation_self_contained": self_contained,
        }
        if adapters:
            prepared["_representation_adapters"] = adapters
        if failure is not None:
            prepared["_batch_preflight_failure"] = failure
        return prepared

    def _calculate_cost(self, model_name: str, input_tokens: int, output_tokens: int) -> float:
        """List-price estimate; callers prefer the provider-reported cost when present."""
        return price_estimate(model_name, input_tokens, output_tokens)

    def _system_instruction(self, prompt_type: str) -> Optional[str]:
        """Static instruction block for item prompts. It lives in the system
        message so provider prefix caches cover it (Z.AI keys its implicit
        cache on the system message; a user-turn prefix never hits)."""
        prompts = getattr(self, "prompts", None) or {}
        return (prompts.get("item", {}).get(prompt_type) or {}).get("system_instruction")

    def _response_schema(self, prompt_type: str) -> Optional[dict]:
        prompts = getattr(self, "prompts", None) or {}
        return (prompts.get("item", {}).get(prompt_type) or {}).get("response_schema")

    def _select_prompt_type(self) -> str:
        """Select prompt type for item summarization.

        Returns:
            Prompt type: always "unified"
        """
        return "unified"

    def _select_model(self, page_count: int, text_size: int) -> tuple[str, str]:
        """Select model based on config and document size.

        One model per backend (no per-case routing); the Gemini small-doc
        split survives only on the native backend for rollback parity.

        Returns:
            Tuple of (model_name, display_name)
        """
        if self.backend.name == "gemini" and config.USE_FLASH_LITE:
            if text_size < FLASH_LITE_MAX_CHARS and page_count <= FLASH_LITE_MAX_PAGES:
                return self.small_doc_model, "flash-lite"
        return self.primary_model, self.primary_model.rsplit("/", 1)[-1]

    def _effort_for(self, page_count: int, text_size: int) -> str:
        """Reasoning effort tier by document size, unless config pins one.

        Same cutoffs the Gemini thinking tiers used: short agendas need no
        deliberation, long packets with many attachments do.
        """
        if config.LLM_REASONING_EFFORT in EFFORT_LEVELS:
            return config.LLM_REASONING_EFFORT
        if page_count <= 10 and text_size <= 30000:
            return EFFORT_LOW
        if page_count <= 50 and text_size <= 150000:
            return EFFORT_MEDIUM
        return EFFORT_HIGH

    def _record_call(
        self,
        *,
        model_display: str,
        prompt_type: str,
        duration: float,
        completion: Optional[Completion],
    ) -> None:
        if completion is None:
            self.metrics.record_llm_call(
                model=model_display,
                prompt_type=prompt_type,
                duration_seconds=duration,
                input_tokens=0,
                output_tokens=0,
                cost_dollars=0,
                success=False,
            )
            return
        cost = completion.cost_usd
        if cost is None:
            cost = self.backend.estimate_cost(
                completion.input_tokens, completion.output_tokens, completion.cached_tokens
            )
        self.metrics.record_llm_call(
            model=model_display,
            prompt_type=prompt_type,
            duration_seconds=duration,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_dollars=cost,
            success=True,
        )

    def summarize_meeting(self, text: str) -> str:
        """Summarize a full meeting agenda

        Args:
            text: Extracted text from agenda PDF(s)

        Returns:
            Summary text
        """
        text_size = len(text)
        page_count = self._estimate_page_count(text)
        model_name, model_display = self._select_model(page_count, text_size)
        effort = self._effort_for(page_count, text_size)
        prompt_type = "meeting_fallback"

        logger.info(
            "summarizing meeting",
            page_count=page_count,
            text_size=text_size,
            model=model_display,
            effort=effort,
        )

        # Single fallback prompt for meeting-level summarization (v3)
        prompt = self._get_prompt("meeting", "fallback", text=text)
        start_time = time.time()

        try:
            completion = self.backend.complete(
                user=prompt,
                system=None,
                schema=None,
                effort=effort,
                max_tokens=config.LLM_MAX_OUTPUT_TOKENS,
                temperature=0.3,
            )
            duration = time.time() - start_time
            self._record_call(
                model_display=model_display,
                prompt_type=prompt_type,
                duration=duration,
                completion=completion,
            )
            logger.info(
                "meeting summarized",
                duration_seconds=round(duration, 1),
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                reasoning_tokens=completion.reasoning_tokens,
                cached_tokens=completion.cached_tokens,
                cost_usd=completion.cost_usd,
                provider=completion.provider,
                model=model_display,
            )
            return completion.text

        except Exception as e:  # Intentionally broad: API boundary, convert to LLMError
            duration = time.time() - start_time
            self._record_call(
                model_display=model_display,
                prompt_type=prompt_type,
                duration=duration,
                completion=None,
            )
            self.metrics.record_error(component="analyzer", error=e)
            logger.error("meeting summarization failed", duration_seconds=round(duration, 1), error=str(e), error_type=type(e).__name__)
            raise LLMError(
                f"Meeting summarization failed after {duration:.1f}s",
                model=model_display,
                prompt_type=prompt_type,
                original_error=e
            ) from e

    def summarize_item(self, item_title: str, text: str, page_count: Optional[int] = None) -> Tuple[str, List[str]]:
        """Summarize a single agenda item and extract topics (adaptive based on size)

        Args:
            item_title: Title of the agenda item
            text: Combined text from all attachments
            page_count: Actual page count from PDF extractor (optional, will estimate if not provided)

        Returns:
            Tuple of (summary, topics_list)
            summary = Combined markdown with thinking trace, summary, and citizen impact
            topics = List of canonical topic strings
        """
        item_title = limit_item_title(item_title)
        text_size = len(text)

        # Use actual page count from PDF if available, otherwise estimate
        if page_count is None:
            page_count = self._estimate_page_count(text)

        prompt_type = self._select_prompt_type()
        model_name, model_display = self._select_model(page_count, text_size)
        effort = self._effort_for(page_count, text_size)
        metric_type = f"item_{prompt_type}"

        logger.info(
            "item processing",
            item_title=item_title[:50],
            page_count=page_count,
            text_size=text_size,
            prompt_type=prompt_type,
            model=model_display,
            effort=effort,
        )

        prompt = self._get_prompt("item", prompt_type, title=item_title, text=text)
        start_time = time.time()

        try:
            completion = self.backend.complete(
                user=prompt,
                system=self._system_instruction(prompt_type),
                schema=self._response_schema(prompt_type),
                effort=effort,
                max_tokens=config.LLM_MAX_OUTPUT_TOKENS,
                temperature=0.3,
            )
            duration = time.time() - start_time
            self._record_call(
                model_display=model_display,
                prompt_type=metric_type,
                duration=duration,
                completion=completion,
            )
            logger.info(
                "item summarized",
                item_title=item_title[:50],
                duration_seconds=round(duration, 1),
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                reasoning_tokens=completion.reasoning_tokens,
                cached_tokens=completion.cached_tokens,
                cost_usd=completion.cost_usd,
                provider=completion.provider,
                finish_reason=completion.finish_reason,
                model=model_display,
            )
            return self._parse_item_response(completion.text)

        except Exception as e:  # Intentionally broad: API boundary, convert to LLMError
            duration = time.time() - start_time
            self._record_call(
                model_display=model_display,
                prompt_type=metric_type,
                duration=duration,
                completion=None,
            )
            self.metrics.record_error(component="analyzer", error=e)
            logger.error("item summarization failed", duration_seconds=round(duration, 1), error=str(e), error_type=type(e).__name__, prompt_type=prompt_type)
            raise LLMError(
                f"Item summarization failed after {duration:.1f}s",
                model=model_display,
                prompt_type=metric_type,
                original_error=e
            ) from e


    async def create_shared_context_cache(
        self, shared_context: Optional[str], meeting_id: Optional[str]
    ) -> Optional[str]:
        """Create a Gemini cache for meeting-level shared documents.

        Returns the cache name, or None when there's no shared context, it's
        below the caching threshold, or creation fails (callers then inline the
        context per request instead).

        The TTL covers the full decoupled job lifecycle. A batch job can sit on
        Gemini's queue for up to ~24h, and every request in it references this
        cache -- an expiry mid-flight fails the whole job. Storage at this size
        is pennies, so we size the TTL past the job ceiling rather than gamble.
        """
        shared_context = limit_shared_context(shared_context)
        if not shared_context or not self.backend.supports_context_cache:
            return None

        # Rough estimate: 1 token ~ 4 chars
        token_count = len(shared_context) // 4
        min_tokens = 1024  # Minimum for Flash caching
        if token_count < min_tokens:
            logger.info(
                "shared context too small for caching",
                token_count=token_count,
                min_tokens=min_tokens,
            )
            return None

        try:
            logger.info("creating gemini cache for shared context", token_count=token_count)
            cache = await self._run_batch_sdk(
                self.client.caches.create,
                model=self.primary_model,
                config=types.CreateCachedContentConfig(
                    display_name=f"meeting-{meeting_id}-shared-docs",
                    contents=[types.Content(parts=[types.Part(text=shared_context)])],
                    ttl="172800s",  # 48h -- past Gemini's batch job lifecycle
                ),
            )
            logger.info("cache created", cache_name=cache.name, token_count=token_count, ttl="48h")
            return cache.name
        except Exception as e:  # Best-effort cache optimization; submission can inline.
            logger.warning(
                "failed to create cache proceeding without caching",
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    async def delete_shared_context_cache(self, cache_name: Optional[str]) -> None:
        """Best-effort cache deletion once a meeting's last chunk is collected.

        Not load-bearing: if this is skipped (e.g. a crash between collect and
        cleanup) the cache expires on its own TTL. Pennies either way.
        """
        if not cache_name:
            return
        try:
            await self._run_batch_sdk(self.client.caches.delete, name=cache_name)
            logger.info("cache deleted", cache_name=cache_name)
        except Exception as e:  # Best-effort cleanup; TTL is the final backstop.
            logger.warning(
                "failed to delete cache",
                cache_name=cache_name,
                error=str(e),
                error_type=type(e).__name__,
            )

    async def submit_item_batches(
        self,
        item_requests: List[Dict[str, Any]],
        cache_name: Optional[str] = None,
        shared_context: Optional[str] = None,
        *,
        submission_scope: Optional[str] = None,
        reserve_submission: Optional[
            Callable[[Dict[str, Any]], Awaitable[bool]]
        ] = None,
        record_submission: Optional[
            Callable[[Dict[str, Any]], Awaitable[None]]
        ] = None,
        fail_submission: Optional[
            Callable[[Dict[str, Any]], Awaitable[None]]
        ] = None,
        include_failures: bool = False,
    ) -> List[Dict[str, Any]]:
        """Submit item summarization to the Gemini Batch API, fire-and-forget.

        Chunks items to respect TPM, submits each chunk as its own batch job,
        and returns one descriptor per submitted chunk. Does NOT wait for
        results -- a collector polls the jobs later (see collect_item_batch).
        This is the decoupled replacement for the old poll-inline summarize_batch.

        Args:
            item_requests: [{'item_id', 'title', 'text', 'sequence',
                             'page_count'?}, ...]
            cache_name: Gemini cache for shared context, if one was created
            shared_context: shared text inlined per request when not cached

        Returns:
            [{'gemini_job_name': str, 'item_ids': List[str], 'chunk_num': int}]
            -- one entry per chunk that submitted successfully.
        """
        if not item_requests:
            return []

        shared_context = limit_shared_context(shared_context)

        total_items = len(item_requests)
        logger.info("submitting batch", total_items=total_items, batch_enabled=True, cost_savings_percent=50)

        # Chunk to respect TPM, sizing by estimated tokens rather than a fixed
        # item count: the old 30-per-chunk assumed <=50K tokens/item, but items
        # can legitimately carry hundreds of thousands of tokens of contract
        # text. Flash Lite 4M TPM; cap a chunk at ~1.2M estimated tokens.
        max_chunk_items = 30
        max_chunk_est_tokens = 1_200_000
        prepared_requests: List[Dict[str, Any]] = []
        for req in item_requests:
            prepared = dict(req)
            prepared.update(
                await self.prepare_document_input(
                    kind="item",
                    title=req.get("title", ""),
                    text=req.get("text", ""),
                    documents=req.get("documents") or [],
                    shared_context=shared_context,
                    cache_name=cache_name,
                    request_id=str(req.get("item_id") or ""),
                )
            )
            prepared_requests.append(prepared)

        preflight_failures = [
            req for req in prepared_requests if req.get("_batch_preflight_failure")
        ]
        if preflight_failures:
            # Keep the meeting submission atomic at the provider boundary. If
            # one representation is known-invalid, do not create successful
            # sibling jobs that could race the queue's terminal transition.
            logger.warning(
                "batch submission stopped by item preflight",
                failed_items=len(preflight_failures),
                total_items=len(prepared_requests),
            )
            prepared_requests = preflight_failures

        chunks: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        current_tokens = 0
        for req in prepared_requests:
            # A preflight failure is isolated so successful neighbours can be
            # submitted and durably collected without ever uploading the
            # rejected request.
            if req.get("_batch_preflight_failure"):
                if current:
                    chunks.append(current)
                    current, current_tokens = [], 0
                chunks.append([req])
                continue
            est_tokens = int(req.get("_batch_input_tokens") or 0)
            if current and (
                len(current) >= max_chunk_items
                or current_tokens + est_tokens > max_chunk_est_tokens
            ):
                chunks.append(current)
                current, current_tokens = [], 0
            current.append(req)
            current_tokens += est_tokens
        if current:
            chunks.append(current)
        logger.info("split into chunks", num_chunks=len(chunks), max_chunk_items=max_chunk_items)

        planned: List[tuple[List[Dict[str, Any]], Dict[str, Any]]] = []
        for chunk_idx, chunk in enumerate(chunks):
            chunk_num = chunk_idx + 1
            item_ids = [str(req["item_id"]) for req in chunk]
            key_material = "\x1f".join(
                [
                    submission_scope or "unscoped",
                    self.prompts_version,
                    self.primary_model,
                    str(chunk_num),
                    *item_ids,
                ]
            )
            descriptor: Dict[str, Any] = {
                "submission_key": hashlib.sha256(
                    key_material.encode("utf-8")
                ).hexdigest(),
                "item_ids": item_ids,
                "chunk_num": chunk_num,
            }
            planned.append((chunk, descriptor))

        async def reserve_chunk(descriptor: Dict[str, Any]) -> Dict[str, Any]:
            if reserve_submission is not None:
                try:
                    reserved = await reserve_submission(descriptor)
                except Exception as exc:
                    logger.error(
                        "failed to reserve batch submission",
                        chunk_num=descriptor["chunk_num"],
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    return {**descriptor, "error": str(exc), "stage": "reserve"}
                if not reserved:
                    # Another submitter already owns this exact logical chunk.
                    # Its durable submitted row is the idempotency authority.
                    return {**descriptor, "already_reserved": True}
            return descriptor

        # Seal every logical chunk durably before any provider create call.
        # Besides eliminating partial intent visibility, this makes exact
        # shared-cache reference counting safe even if a tiny first chunk
        # completes while a slower sibling is still being submitted.
        reserved_descriptors = await asyncio.gather(
            *(reserve_chunk(descriptor) for _, descriptor in planned)
        )

        async def submit_chunk(
            chunk: List[Dict[str, Any]], descriptor: Dict[str, Any]
        ) -> Dict[str, Any]:
            if descriptor.get("error") or descriptor.get("already_reserved"):
                return descriptor
            chunk_num = int(descriptor["chunk_num"])

            async with self._batch_submit_semaphore:
                provider_descriptor = await self._submit_one_chunk(
                    chunk,
                    chunk_num,
                    cache_name,
                    shared_context,
                    submission_key=descriptor["submission_key"],
                )
            descriptor.update(provider_descriptor)

            if descriptor.get("gemini_job_name") and record_submission is not None:
                # The provider job now exists. Retry durable activation before
                # yielding, collapsing the create/record window to process
                # death or a sustained database outage.
                last_error: Optional[Exception] = None
                for attempt in range(4):
                    try:
                        await record_submission(descriptor)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        if attempt < 3:
                            await asyncio.sleep(2**attempt)
                if last_error is not None:
                    logger.error(
                        "provider batch created but durable activation failed",
                        chunk_num=chunk_num,
                        gemini_job_name=descriptor["gemini_job_name"],
                        submission_key=descriptor["submission_key"],
                        error=str(last_error),
                    )
                    descriptor.update(
                        error=str(last_error), stage="record_submission"
                    )

            elif descriptor.get("error") and fail_submission is not None:
                try:
                    await fail_submission(descriptor)
                except Exception as exc:
                    logger.error(
                        "failed to persist batch submit failure",
                        chunk_num=chunk_num,
                        submission_key=descriptor["submission_key"],
                        error=str(exc),
                    )
            return descriptor

        # gather preserves input order while tasks execute independently, so
        # descriptors remain deterministic without serial provider latency.
        descriptors = await asyncio.gather(
            *(
                submit_chunk(chunk, descriptor)
                for (chunk, _), descriptor in zip(planned, reserved_descriptors)
            )
        )
        if include_failures:
            return descriptors
        return [
            descriptor
            for descriptor in descriptors
            if descriptor.get("gemini_job_name") and not descriptor.get("error")
        ]

    def _extract_response_text(self, response_data: Dict[str, Any]) -> Optional[str]:
        """Extract text from nested Gemini response structure

        Args:
            response_data: Response data from batch API

        Returns:
            Extracted text or None if not found
        """
        # Direct text field (simple case)
        if 'text' in response_data:
            return response_data['text']

        # Navigate candidates structure (complex case)
        candidates = response_data.get('candidates')
        if not candidates:
            return None

        candidate = candidates[0]
        content = candidate.get('content')
        if not content:
            return None

        parts = content.get('parts')
        if not parts:
            return None

        text_parts = [
            str(part.get("text"))
            for part in parts
            if isinstance(part, dict)
            and part.get("text")
            and not part.get("thought")
        ]
        return "".join(text_parts) or None

    def _parse_batch_response_line(
        self,
        line: str,
        line_num: int,
        request_map: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Parse single JSONL response line and extract result

        Args:
            line: JSONL line to parse
            line_num: Line number for logging
            request_map: Map of item_id -> original request

        Returns:
            Result dict with item_id, success, summary/topics/error
            None if line should be skipped
        """
        if not line.strip():
            return None

        try:
            response_obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.error("failed to parse jsonl line", line_num=line_num, error=str(e), error_type=type(e).__name__)
            return None

        # Extract key from response
        key = response_obj.get('key')
        if not key:
            logger.error("response line missing key field", line_num=line_num)
            return None

        if key not in request_map:
            logger.warning(
                "no mapping found for key",
                key=key,
                sample_keys=list(request_map.keys())[:5]
            )
            return None

        original_req = request_map[key]

        # Handle error response
        if 'error' in response_obj:
            error_data = response_obj['error']
            error_metadata = self._provider_error_metadata(error_data)
            error_str = error_metadata["error"]

            # Log quota errors but DON'T retry the whole chunk
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                logger.warning(
                    "item hit quota limit individual failure",
                    key=key,
                    error=error_str
                )
            logger.error("item failed", key=key, error=error_str)

            return {
                "item_id": original_req["item_id"],
                "success": False,
                **error_metadata,
            }

        # Handle success response
        if 'response' not in response_obj:
            return {
                "item_id": original_req["item_id"],
                "success": False,
                "error": "Batch result contained neither response nor error",
                "error_type": "malformed_batch_result",
                "retryable": True,
            }

        response_data = response_obj['response']
        response_text = None
        diagnostics = self._batch_response_diagnostics(response_data)

        try:
            # Extract text from nested structure
            response_text = self._extract_response_text(response_data)

            # Check finish_reason
            candidates = response_data.get('candidates')
            if candidates:
                finish_reason = diagnostics.get("finish_reason")
                if finish_reason and finish_reason != "STOP":
                    logger.warning(
                        "non-normal finish reason",
                        item_key=key,
                        finish_reason=finish_reason
                    )
                    if finish_reason == "MAX_TOKENS":
                        logger.error(
                            "item hit max tokens response truncated",
                            item_key=key
                        )

            # Log response
            logger.info(
                "response received",
                key=key,
                response_length=len(response_text) if response_text else 0
            )

            if not response_text:
                retryable = self._empty_response_is_retryable(diagnostics)
                logger.warning(
                    "empty response from gemini",
                    key=key,
                    retryable=retryable,
                    **diagnostics,
                )
                return {
                    "item_id": original_req["item_id"],
                    "success": False,
                    "error": "Empty response from Gemini",
                    "error_type": (
                        "blocked_or_terminal_empty_response"
                        if not retryable
                        else "empty_response"
                    ),
                    "retryable": retryable,
                    "diagnostics": diagnostics,
                }

            # Parse response
            summary, topics = self._parse_item_response(response_text)

            return {
                "item_id": original_req["item_id"],
                "success": True,
                "summary": summary,
                "topics": topics,
            }

        except (JSONDecodeError, ValueError, KeyError, AttributeError) as e:
            logger.error(
                "error parsing response",
                key=key,
                error=str(e),
                error_type=type(e).__name__
            )
            # Batch-collector request maps carry only item_id (see
            # collect_item_batch), so title/text must not be assumed here:
            # a raise inside this handler escapes the collector and wedges
            # the batch in 'submitted' forever.
            logger.error(
                "input that caused failure",
                title=str(original_req.get('title', '<unknown>'))[:100],
                text_length=len(original_req.get('text') or '')
            )
            logger.error(
                "raw response that failed",
                response_preview=str(response_text)[:1000] if response_text else 'None'
            )
            return {
                "item_id": original_req["item_id"],
                "success": False,
                "error": str(e),
                "error_type": "response_parse_error",
                "retryable": self._empty_response_is_retryable(diagnostics),
                "diagnostics": diagnostics,
            }

    async def collect_item_batch(
        self, gemini_job_name: str, item_ids: List[str]
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        """Poll a submitted batch job once and, if terminal, return its results.

        Single GET -- no blocking loop. The collector calls this on its own
        cadence and decides what to do with each verdict. We never cancel: a
        running job is simply re-polled next tick. Gemini's terminal state is
        the only authority on done-ness, since the compute is already paid for.

        Returns:
            ('running', None)      -- not yet terminal; poll again later
            ('succeeded', results) -- terminal SUCCEEDED; results parsed
            ('failed', None)       -- Gemini FAILED/CANCELLED/EXPIRED, or
                                      SUCCEEDED with no output file
        results match the old per-chunk shape:
            [{'item_id', 'success', 'summary'?, 'topics'?, 'error'?}, ...]
        """
        terminal_states = {
            "JOB_STATE_SUCCEEDED",
            "JOB_STATE_FAILED",
            "JOB_STATE_CANCELLED",
            "JOB_STATE_EXPIRED",
        }

        batch_job = await self._run_batch_sdk(
            self.client.batches.get, name=gemini_job_name
        )
        state = batch_job.state.name if batch_job.state else "unknown"

        if state not in terminal_states:
            return "running", None

        if state != "JOB_STATE_SUCCEEDED":
            # Gemini's own terminal failure -- not us killing it.
            logger.warning(
                "batch job terminal non-success",
                gemini_job_name=gemini_job_name,
                state=state,
            )
            return "failed", None

        if not batch_job.dest or not batch_job.dest.file_name:
            logger.error("batch succeeded but no response file", gemini_job_name=gemini_job_name)
            return "failed", None

        response_file_name = batch_job.dest.file_name
        logger.info("downloading response file", file_name=response_file_name)
        response_content = await self._run_batch_sdk(
            self.client.files.download, file=response_file_name
        )
        response_text = response_content.decode("utf-8")

        # Response parsing keys off the per-line 'key' (== item_id); the minimal
        # map below is all _parse_batch_response_line needs to re-associate.
        request_map = {iid: {"item_id": iid} for iid in item_ids}
        results: List[Dict[str, Any]] = []
        for line_num, line in enumerate(response_text.strip().split("\n")):
            result = self._parse_batch_response_line(line, line_num, request_map)
            if result:
                results.append(result)

        # Provider success does not guarantee one response per input line.
        # Surface omissions explicitly so the collector promptly requeues
        # those items instead of silently declaring them complete.
        returned_ids = {str(result.get("item_id")) for result in results}
        for item_id in item_ids:
            if str(item_id) not in returned_ids:
                results.append(
                    {
                        "item_id": item_id,
                        "success": False,
                        "error": "Batch response omitted item",
                        "error_type": "omitted_batch_result",
                        "retryable": True,
                    }
                )

        successful = sum(1 for r in results if r.get("success"))
        logger.info(
            "batch chunk collected",
            gemini_job_name=gemini_job_name,
            successful=successful,
            total=len(results),
        )
        return "succeeded", results

    async def _submit_one_chunk(
        self,
        chunk_requests: List[Dict[str, Any]],
        chunk_num: int,
        cache_name: Optional[str] = None,
        shared_context: Optional[str] = None,
        submission_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build, upload, and submit one chunk as a Gemini batch job.

        Fire-and-forget: returns as soon as the job is created. Does NOT wait
        for or download results -- that's the collector's job (collect_item_batch).

        Args:
            chunk_requests: item requests for this chunk
            chunk_num: chunk number for logging/display
            cache_name: Gemini cache for shared context, if created
            shared_context: shared text inlined per request when not cached

        Returns:
            {'gemini_job_name', 'item_ids', 'chunk_num', 'attempts'} on success,
            or the same identity fields plus {'error', 'attempts'} on failure.
        """
        max_retries = 4
        retry_delay = 5
        item_ids = [str(req["item_id"]) for req in chunk_requests]

        preflight_failures = [
            req for req in chunk_requests if req.get("_batch_preflight_failure")
        ]
        if preflight_failures:
            # submit_item_batches isolates these as single-item chunks. Keep a
            # defensive aggregate here so direct callers still fail closed.
            failure = dict(preflight_failures[0]["_batch_preflight_failure"])
            logger.error(
                "batch chunk rejected by input preflight",
                chunk_num=chunk_num,
                item_ids=item_ids,
                error=failure.get("error"),
                error_type=failure.get("error_type"),
                input_tokens=failure.get("input_tokens"),
            )
            return {
                "item_ids": item_ids,
                "chunk_num": chunk_num,
                "attempts": 0,
                **failure,
            }

        for attempt in range(max_retries):
            temp_path = None

            try:
                temp_file = tempfile.NamedTemporaryFile(
                    mode='w', suffix='.json', delete=False
                )
                temp_path = temp_file.name
                item_ids = []

                for i, req in enumerate(chunk_requests):
                    item_title = limit_item_title(req["title"])
                    item_id = req["item_id"]
                    self_contained = bool(
                        req.get("_representation_self_contained")
                    )
                    text = req.get("_model_text")
                    if text is None:
                        # Defensive compatibility for direct private-method
                        # callers. Normal submissions are prepared once by
                        # prepare_document_input and use its exact preflighted
                        # model text here.
                        text = prepare_item_text(
                            item_title,
                            req["text"],
                            None if self_contained else shared_context,
                            inline_shared=not cache_name and not self_contained,
                        )

                    # Use actual page count if available, otherwise estimate
                    page_count = req.get("page_count")
                    if page_count is None:
                        page_count = self._estimate_page_count(text)

                    # Prompt selection
                    prompt_type = self._select_prompt_type()

                    # Build prompt and config
                    prompt = self._get_prompt(
                        "item", prompt_type, title=item_title, text=text
                    )

                    generation_config = {
                        "temperature": 0.3,
                        "maxOutputTokens": 8192,
                        "responseMimeType": "application/json",
                    }
                    # Same structured-output enforcement as the streaming path:
                    # there the SDK normalizes JSON-schema types ("object") into
                    # REST enums ("OBJECT"); raw JSONL bypasses it, so mirror.
                    response_schema = self.prompts["item"][prompt_type].get("response_schema")
                    if response_schema:
                        generation_config["responseSchema"] = (
                            types.Schema.model_validate(response_schema)
                            .model_dump(mode="json", exclude_none=True)
                        )

                    # Same adaptive thinking tiers as the streaming path
                    thinking = self._thinking_config_json(
                        page_count, len(text), self.primary_model
                    )
                    if thinking:
                        generation_config["thinkingConfig"] = thinking

                    logger.info(
                        "batch request details",
                        request_index=i,
                        item_title=item_title[:80],
                        text_length=len(text),
                        input_tokens=req.get("_batch_input_tokens"),
                        page_count=page_count,
                        prompt_type=prompt_type
                    )

                    # Write JSONL line with key for matching
                    jsonl_line = {
                        "key": item_id,
                        "request": {
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": generation_config,
                        }
                    }
                    system_instruction = self._system_instruction(prompt_type)
                    if system_instruction:
                        jsonl_line["request"]["systemInstruction"] = {
                            "parts": [{"text": system_instruction}]
                        }

                    if cache_name and not self_contained:
                        jsonl_line["request"]["cachedContent"] = cache_name

                    temp_file.write(json.dumps(jsonl_line) + '\n')
                    item_ids.append(item_id)

                temp_file.close()

                # Upload JSONL file
                logger.info(
                    "uploading jsonl file",
                    num_items=len(chunk_requests),
                    attempt=attempt + 1,
                    max_retries=max_retries
                )

                display_key = submission_key[:24] if submission_key else str(time.time())
                uploaded_file = await self._run_batch_sdk(
                    self.client.files.upload,
                    file=temp_path,
                    config={"display_name": f"engagic-input-{display_key}"},
                )

                if not uploaded_file.name:
                    raise ValueError("File uploaded but no name returned")

                logger.info("uploaded file", file_name=uploaded_file.name)

                # Submit batch job (no wait -- the collector polls it later)
                logger.info("submitting batch job", chunk_num=chunk_num)

                try:
                    batch_job = await self._run_batch_sdk(
                        self.client.batches.create,
                        model=self.primary_model,
                        src=uploaded_file.name,
                        config={"display_name": f"engagic-batch-{display_key}"},
                    )
                except Exception:
                    # Don't leak the uploaded JSONL into the Files quota
                    try:
                        await self._run_batch_sdk(
                            self.client.files.delete, name=uploaded_file.name
                        )
                    except Exception:
                        pass
                    raise

                if not batch_job.name:
                    raise ValueError("Batch job created but no name returned")

                logger.info(
                    "submitted batch",
                    batch_name=batch_job.name,
                    chunk_num=chunk_num,
                    item_count=len(item_ids),
                )
                return {
                    "gemini_job_name": batch_job.name,
                    "item_ids": item_ids,
                    "chunk_num": chunk_num,
                    "attempts": attempt + 1,
                }

            except Exception as e:  # Intentionally broad: retry logic with specific error checks
                error_str = str(e)
                error_metadata = self._provider_error_metadata(e)
                is_quota_error = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                is_transient = is_quota_error or any(
                    marker in error_str.upper()
                    for marker in (
                        "408",
                        "500",
                        "502",
                        "503",
                        "504",
                        "UNAVAILABLE",
                        "INTERNAL",
                        "DEADLINE_EXCEEDED",
                        "TIMEOUT",
                        "CONNECTION",
                    )
                )

                self.metrics.record_error(component="analyzer", error=e)

                if is_transient and attempt < max_retries - 1:
                    base_delay = 30 if is_quota_error else retry_delay
                    backoff_delay = min(120, base_delay * (2**attempt))
                    logger.warning(
                        "transient chunk submit failure retrying",
                        chunk_num=chunk_num,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        backoff_delay_seconds=backoff_delay
                    )
                    await asyncio.sleep(backoff_delay)
                    continue

                # Final attempt failed or non-quota error
                logger.error("batch chunk submit failed", chunk_num=chunk_num, attempts=attempt + 1, error=error_str, error_type=type(e).__name__)
                return {
                    "item_ids": item_ids,
                    "chunk_num": chunk_num,
                    "attempts": attempt + 1,
                    "error": error_str,
                    "error_code": error_metadata.get("error_code"),
                    "error_status": error_metadata.get("error_status"),
                    "retryable": is_transient and error_metadata["retryable"],
                    "stage": "submit",
                }

            finally:
                # The uploaded copy lives server-side now; drop the local temp.
                try:
                    if temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError as cleanup_error:
                    logger.warning("failed to cleanup temp file", path=temp_path, error=str(cleanup_error), error_type=type(cleanup_error).__name__)

        return {
            "item_ids": item_ids,
            "chunk_num": chunk_num,
            "attempts": max_retries,
            "error": "Batch submission retries exhausted",
            "retryable": True,
            "stage": "submit",
        }

    def _get_prompt(self, category: str, prompt_type: str, **variables) -> str:
        """Get prompt from JSON and format with variables

        Args:
            category: Top-level category (e.g., 'meeting', 'item')
            prompt_type: Specific prompt type (e.g., 'short_agenda', 'standard')
            **variables: Variables to interpolate into template

        Returns:
            Formatted prompt string
        """
        try:
            prompt_data = self.prompts[category][prompt_type]
            template = prompt_data["template"]
        except KeyError as e:
            raise ValueError(f"Prompt not found: {category}.{prompt_type}") from e

        # Validate the TEMPLATE for missing variables before substitution.
        # Substituted values often include user content (PDF text, agenda items)
        # that legitimately contains literal {word} tokens -- e.g. CAD notation,
        # engineering drawings, code snippets -- which would false-positive a
        # post-substitution scan.
        template_vars = set(re.findall(r"\{(\w+)\}", template))
        missing = template_vars - set(variables.keys())
        if missing:
            logger.warning(
                "template variables not provided to formatter",
                category=category,
                prompt_type=prompt_type,
                missing_variables=sorted(missing),
            )

        result = template
        for key, value in variables.items():
            result = result.replace("{" + key + "}", str(value))

        return result

    def _thinking_config_json(
        self, page_count: int, text_size: int, model_name: str
    ) -> Dict[str, Any]:
        """REST-JSON form of the Gemini thinking tiers (Batch lane only).

        Batch JSONL requests bypass the SDK config objects, so the same
        three complexity tiers are mirrored in camelCase. Gemini 3.x takes
        thinkingLevel, 2.5 takes thinkingBudget -- mixing the two errors.
        """
        is_gemini3 = "3." in model_name or "3-" in model_name
        if page_count <= 10 and text_size <= 30000:
            return {"thinkingLevel": "MINIMAL"} if is_gemini3 else {"thinkingBudget": 0}
        if page_count <= 50 and text_size <= 150000:
            return {"thinkingLevel": "MEDIUM"} if is_gemini3 else {"thinkingBudget": 2048}
        return {"thinkingLevel": "HIGH"} if is_gemini3 else {"thinkingBudget": -1}

    def _parse_item_response(self, response_text: str) -> Tuple[str, List[str]]:
        """Parse item response into summary and topics

        Args:
            response_text: Raw JSON response from Gemini

        Returns:
            Tuple of (summary, topics_list)
            summary = Combined markdown with thinking trace, summary, and citizen impact
            topics = List of canonical topic strings (validated against taxonomy)
        """
        response_text = strip_code_fence(response_text).strip()

        try:
            data = parse_json_lenient(response_text)

            # Validate JSON structure
            required_fields = ["summary_markdown", "topics"]
            missing_fields = [f for f in required_fields if f not in data]
            if missing_fields:
                logger.error("json missing required fields", missing_fields=missing_fields)
                raise ValueError(f"Invalid JSON response: missing {missing_fields}")

            # Build summary
            summary_md = data.get("summary_markdown", "")

            # Validate and normalize topics
            raw_topics = data.get("topics", [])
            if not isinstance(raw_topics, list):
                logger.error("topics field is not a list", topics_type=type(raw_topics).__name__)
                raw_topics = []

            # Validate topics against canonical taxonomy
            from analysis.topics.normalizer import get_normalizer
            normalizer = get_normalizer()
            canonical_topics = normalizer.get_all_canonical_topics()

            validated_topics = []
            invalid_topics = []

            for topic in raw_topics:
                if topic in canonical_topics:
                    validated_topics.append(topic)
                else:
                    invalid_topics.append(topic)
                    logger.warning("llm returned invalid topic not in taxonomy", topic=topic)

            if invalid_topics:
                logger.warning(
                    "rejected invalid topics",
                    num_invalid=len(invalid_topics),
                    invalid_topics=invalid_topics,
                    valid_topics=validated_topics
                )

            # If all topics were invalid, use "other" as fallback
            if not validated_topics and raw_topics:
                logger.warning("all topics invalid using other as fallback")
                validated_topics = ["other"]

            topics = validated_topics

            summary = summary_md

            return summary, topics

        except json.JSONDecodeError as e:
            logger.error("failed to parse json response", error=str(e), error_type=type(e).__name__)
            logger.error("full malformed json response", response_text=response_text)

            # Attempt to salvage truncated responses
            # Truncation typically happens mid-field, but summary_markdown is usually complete
            if not response_text.rstrip().endswith('}'):
                logger.warning("response appears truncated, attempting salvage")
                salvaged = self._salvage_truncated_response(response_text)
                if salvaged:
                    return salvaged

            raise
        except Exception as e:  # Intentionally broad: log validation error then propagate
            logger.error("error validating json response", error=str(e), error_type=type(e).__name__)
            logger.error("response that failed validation", response_text=response_text)
            raise

    def _salvage_truncated_response(self, response_text: str) -> tuple[str, list[str]] | None:
        """Attempt to extract usable content from a truncated JSON response.

        When Gemini truncates output mid-response (often on large documents),
        the summary_markdown field is typically complete since it comes first.
        This method uses regex to extract whatever fields are available.

        Args:
            response_text: The truncated JSON response

        Returns:
            (summary, topics) tuple if salvageable, None if not enough content
        """
        summary_parts = []
        topics: list[str] = []

        # Extract summary_markdown - usually complete even in truncated responses
        # Terminator accepts a closing quote, end-of-text, or end-of-text with
        # a dangling backslash: truncation mid-escape leaves a lone trailing
        # backslash that neither char class consumes, which used to fail the
        # whole match and lose an otherwise complete summary.
        summary_match = re.search(
            r'"summary_markdown"\s*:\s*"((?:[^"\\]|\\.)*)(?:"|\\?$)',
            response_text,
            re.DOTALL
        )
        if summary_match:
            summary_md = summary_match.group(1)
            # Unescape JSON string escapes
            summary_md = summary_md.replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
            if summary_md.strip():
                summary_parts.append(summary_md)

        # Try to extract topics array
        topics_match = re.search(
            r'"topics"\s*:\s*\[(.*?)\]',
            response_text,
            re.DOTALL
        )
        if topics_match:
            topics_str = topics_match.group(1)
            # Extract quoted strings from the array
            topic_items = re.findall(r'"([^"]+)"', topics_str)
            topics = [t.strip() for t in topic_items if t.strip()]

        # Only return if we got meaningful content
        if not summary_parts:
            logger.warning("salvage failed: no summary content found in truncated response")
            return None

        # Add truncation notice
        summary_parts.append("\n---\n*Note: This summary was recovered from a truncated response.*")

        summary = "\n".join(summary_parts)
        logger.info(
            "salvaged truncated response",
            summary_len=len(summary),
            topics_count=len(topics)
        )

        return summary, topics

    def _estimate_page_count(self, text: str) -> int:
        """Estimate page count from text

        Args:
            text: Extracted text

        Returns:
            Estimated page count
        """
        # Rough estimate: ~2000 chars per page
        return max(1, len(text) // 2000)


# Import alias for callers that predate the provider-neutral rename.
GeminiSummarizer = Summarizer
