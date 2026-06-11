# Analysis Module - LLM Intelligence & Topic Extraction

**Transform raw meeting documents into actionable civic intelligence.** Async orchestration, Gemini API integration, adaptive prompting, topic normalization.

---

## Overview

The analysis module provides LLM-powered intelligence for civic meeting documents. Orchestrates Google's Gemini API to generate summaries, extract topics, and assess citizen impact from agenda items and meeting packets.

**Core Capabilities:**
- **Async orchestration:** Concurrent PDF downloads, semaphore-limited LLM calls
- **Reactive rate limiting:** Respects Gemini's `retryDelay` on 429 errors with exponential backoff
- **Unified adaptive prompting:** Single prompt with four classification levels lets the LLM scale output depth to consequence (2-20 sentences)
- **Topic extraction:** 16 canonical civic topics (housing, zoning, transportation, etc.)
- **Citizen impact assessment:** "Why should residents care?" analysis
- **Batch processing:** 50% cost savings via Gemini Batch API (JSONL file method)
- **JSON structured output:** Schema-validated responses (no parsing failures)

**Architecture Pattern:** AsyncAnalyzer (orchestration) → GeminiSummarizer (LLM + rate limiting) → TopicNormalizer (mapping)

```
analysis/
├── analyzer_async.py       # 428 lines - Async orchestration
├── llm/
│   ├── summarizer.py       # 1,309 lines - Gemini API + reactive rate limiting
│   ├── prompts_v2.json     # 82 lines - Legacy prompt template (retained for reference)
│   └── prompts_v3.json     # 61 lines - Active prompt with 4 classification levels + 7 worked examples
└── topics/
    ├── normalizer.py       # 230 lines - Topic normalization
    └── taxonomy.json       # 242 lines - 16 canonical topics

**Total:** 1,967 lines Python + 324 lines JSON = 2,291 lines
```

---

## Architecture

### AsyncAnalyzer (analysis/analyzer_async.py)

**Main orchestration layer** - coordinates async PDF downloads and LLM calls. Rate limiting is handled reactively by the summarizer.

```python
class AsyncAnalyzer:
    """
    Async LLM analysis orchestrator.

    Key Features:
    - Async PDF downloads (aiohttp, concurrent)
    - CPU-bound extraction in thread pool (non-blocking)
    - Concurrent batch processing with configurable semaphore

    Rate limiting handled reactively by summarizer via Gemini's retry instructions.
    """

    def __init__(self, api_key: Optional[str] = None, metrics: Optional[MetricsCollector] = None):
        self.metrics = metrics or NullMetrics()
        self.pdf_extractor = PdfExtractor()  # Sync extractor, wrap in asyncio.to_thread()
        self.summarizer = GeminiSummarizer(api_key=api_key, metrics=self.metrics)
        self.http_session: Optional[aiohttp.ClientSession] = None
```

**Key Methods:**

```python
async def process_agenda_with_cache_async(meeting_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main entry point - process agenda with caching support.

    Args:
        meeting_data: Dict with packet_url, city_banana, meeting_id, etc.

    Returns: {
        success: bool,
        summary: str,
        processing_time: float,
        processing_method: str,  # "pymupdf_gemini"
        participation: Optional[Dict],
        cached: bool,
        meeting_id: str,
        error: str (if failed)
    }
    """

async def download_pdf_async(url: str) -> bytes:
    """Download PDF asynchronously with aiohttp"""

async def extract_pdf_async(url: str) -> Dict[str, Any]:
    """
    Extract text from PDF asynchronously.
    Downloads with async HTTP, extracts in thread pool (CPU-bound).
    Returns: {success, text, page_count, ...}
    """

async def process_agenda_async(url: str) -> Tuple[str, str, Optional[Dict]]:
    """
    Process agenda using PyMuPDF + Gemini (fail-fast approach).
    Returns: (summary, method_used, participation_info)
    """

async def process_batch_items_async(
    item_requests: List[Dict[str, Any]],
    shared_context: Optional[str] = None,
    meeting_id: Optional[str] = None
) -> List[List[Dict[str, Any]]]:
    """
    Process multiple agenda items concurrently (semaphore-limited).
    Concurrency controlled by config.LLM_CONCURRENCY (default 3).
    Returns: [[{item_id, success, summary, topics, error?}, ...]]
    """
```

**Concurrency Model:**

Items are processed concurrently via `asyncio.gather` with a semaphore limiting parallel LLM calls:

```python
concurrency = config.LLM_CONCURRENCY  # Default 3 (configurable via ENGAGIC_LLM_CONCURRENCY)
semaphore = asyncio.Semaphore(concurrency)

results = await asyncio.gather(
    *[process_with_limit(item, i) for i, item in enumerate(item_requests)],
    return_exceptions=True
)
```

**Session Management:**

HTTP sessions are automatically recycled after 100 requests to prevent memory accumulation. Recycling is serialized via `asyncio.Lock` and skipped when downloads are in-flight:

```python
async with self._recycle_lock:
    self._request_count += 1
    if self._request_count >= self._recycle_after and self._in_flight == 0:
        await self.recycle_session()
```

**Timeout Structure:**

Defense-in-depth timeout hierarchy prevents hangs:
- PDF extraction: 10 minutes (includes OCR budget)
- LLM summarization: 5 minutes per call
- Retry budget: 3 minutes total (within LLM timeout)

**Exceptions:**

- `AnalysisError`: Raised when document analysis fails (scanned/complex PDF)
- Wraps `ExtractionError` (PDF failures) and `LLMError` (Gemini failures)

**Usage:**
```python
# Recommended: Use as async context manager for automatic cleanup
async with AsyncAnalyzer() as analyzer:
    results = await analyzer.process_batch_items_async(items)
# Session automatically closed on exit

# Alternative: Manual lifecycle management
analyzer = AsyncAnalyzer()
results = await analyzer.process_batch_items_async(items)
await analyzer.close()  # Cleanup HTTP session
```

---

### GeminiSummarizer (analysis/llm/summarizer.py)

**Gemini API orchestration** with config-driven model selection, unified adaptive prompting, reactive rate limiting, and batch processing.

```python
class GeminiSummarizer:
    """Smart LLM orchestrator - picks model, picks prompt, formats response"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        prompts_path: Optional[str] = None,
        metrics: Optional[MetricsCollector] = None
    ):
        self.metrics = metrics or NullMetrics()
        # API key: api_key param > GEMINI_API_KEY env > LLM_API_KEY env
        self.client = genai.Client(api_key=self.api_key)
        self.flash_model_name = "gemini-2.5-flash"
        self.flash_lite_model_name = "gemini-2.5-flash-lite"

        # Load prompts from JSON (v3, via importlib.resources)
        self.prompts = json.loads(files("analysis.llm").joinpath("prompts_v3.json").read_text())
```

**Reactive Rate Limiting:**

Instead of proactive token bucket limiting, we trust Gemini to tell us when to retry. The `_call_with_retry()` method parses `retryDelay` from 429 responses:

```python
def _call_with_retry(self, model_name: str, prompt: str, config, max_retries=3, max_retry_seconds=180):
    """
    Call Gemini API with automatic retry on 429 rate limits.

    - Parses retryDelay from Gemini's 429 error response (handles multiple quote/format styles)
    - Fallback: exponential backoff (30s, 60s, 90s) if no retryDelay provided
    - Total retry time capped at max_retry_seconds (default 3 minutes)
    - Non-rate-limit errors raise immediately
    """
```

**Model Selection (config-driven):**

| Model | Use Case | Controlled By |
|-------|----------|---------------|
| Flash-2.5 (default) | All items by default | Always selected unless USE_FLASH_LITE is enabled |
| Flash-2.5-Lite | Small docs (<50 pages AND <200K chars) | Only when `config.USE_FLASH_LITE = True` |

```python
def _select_model(self, page_count: int, text_size: int) -> tuple[str, str]:
    # Default: Flash for everything (consistent quality)
    # If USE_FLASH_LITE enabled: use Flash-Lite for small docs (cost savings)
    if config.USE_FLASH_LITE:
        if text_size < 200_000 and page_count <= 50:
            return self.flash_lite_model_name, "flash-lite"
    return self.flash_model_name, "flash"
```

**Unified Prompt (replaces standard/large split):**

A single `"unified"` prompt handles all item sizes. The LLM decides output depth based on content complexity:

```python
def _select_prompt_type(self) -> str:
    return "unified"  # Always unified - LLM scales output to complexity
```

The unified prompt provides four classification levels controlling output depth:
- **ROUTINE** (appointments, routine renewals, procedural filings): 2-3 sentences
- **STANDARD** (budget amendments, permits, contract awards): 4-6 sentences
- **CONSEQUENTIAL** (ordinances, developments, zoning changes): 7-12 sentences with sections
- **MAJOR** (fiscal reports, multi-document council files, credit analyses): 12-20 sentences, narrative prose with sections

**Adaptive Thinking Configuration:**

Thinking budget scales with document complexity:

```python
def _get_thinking_config(page_count, text_size, model_name):
    """
    - Simple (≤10 pages, ≤30K chars): thinking_budget=0 (disabled for speed)
    - Medium (≤50 pages, ≤150K chars):
        - Flash-Lite: thinking_budget=2048 (explicit, doesn't think by default)
        - Flash: no thinking config (model decides dynamically)
    - Complex (>50 pages or >150K chars): thinking_budget=-1 (dynamic/unlimited)
    """
```

**Item Summarization:**

```python
def summarize_item(item_title: str, text: str, page_count: Optional[int] = None) -> Tuple[str, List[str]]:
    """
    Summarize agenda item with unified prompt.

    Always uses max_output_tokens=8192 and response_mime_type="application/json".
    Response is parsed into summary markdown with a validated topic list.

    Returns: (summary_markdown, canonical_topics_list)
    """
```

**Meeting Summarization (fallback):**

```python
def summarize_meeting(text: str) -> str:
    """
    Summarize full meeting agenda (plain markdown, no JSON).

    Prompt selection by document size:
    - ≤30 pages: "short_agenda" prompt
    - >30 pages: "comprehensive" prompt

    Used as fallback when item-level processing is unavailable.
    """
```

**Batch Processing (50% Cost Savings):**

`summarize_batch` is the Gemini Batch API path — 50% token discount and a
**separate quota pool** from interactive calls. Invoked by the processor's
batch lane for meetings outside the urgent window (see pipeline/README,
Processing Lanes); the streaming path (`process_batch_items_async`) keeps
handling urgent meetings.

```python
async def summarize_batch(
    item_requests: List[Dict[str, Any]],
    shared_context: Optional[str] = None,
    meeting_id: Optional[str] = None
):
    """
    Process multiple items using Gemini Batch API (async generator).

    Yields results per chunk immediately for incremental saving:
    - 30 items per chunk, 10s delays between chunks
    - JSONL file upload (client.batches.create), poll until done; 1h
      per-chunk wait, and timed-out jobs are CANCELLED server-side so a
      retry never pays for the same tokens twice
    - Same responseSchema and adaptive thinkingConfig tiers as the
      streaming path (parity restored 2026-06-11)
    - Orphaned JSONL uploads deleted if job creation fails

    Yields: List of results per chunk
    """
    # Shared-context cache (if token count >= 1024): ttl="14400s" — 4h,
    # so a queued multi-chunk batch can't outlive its own cache
```

**Model note:** batch always uses `PRIMARY_MODEL`; streaming may downgrade
small docs to `SMALL_DOC_MODEL` (USE_FLASH_LITE). At 50% off, batch runs
the newer model for roughly the streaming small-doc price.

**Truncated Response Recovery:**

```python
def _salvage_truncated_response(response_text: str) -> tuple[str, list[str]] | None:
    """
    Extract content from truncated JSON using regex.

    Truncation typically happens mid-field, but summary_markdown is usually
    complete since it comes first. Uses regex to extract whatever fields
    are available. Adds truncation notice to recovered summary.

    Returns (summary, topics) if salvageable, None if insufficient content.
    """
```

**Response Parsing:**

Item responses are parsed from JSON into a combined markdown document:

```python
def _parse_item_response(response_text: str) -> Tuple[str, List[str]]:
    """
    Parse JSON response into (summary, topics).

    Output format: summary_markdown content directly (v3 removed citizen_impact
    and confidence fields).

    Topics are validated against canonical taxonomy via TopicNormalizer.
    Invalid topics are rejected (logged); falls back to ["other"] if all invalid.
    """
```

---

## Prompts Architecture (prompts_v3.json)

**JSON-structured prompts** with schema-validated responses. v3 replaces v2 with four classification levels and richer extraction rules.

**Structure:**
```json
{
  "item": {
    "unified": {
      "description": "v3 unified prompt - editorial judgment over exhaustive extraction",
      "variables": ["title", "text"],
      "output_format": "json",
      "response_schema": { ... },
      "system_instruction": "You are an expert at analyzing city council agenda items...",
      "template": "Analyze this city council agenda item...\n\n# Item Title\n\"{title}\"\n..."
    }
  },
  "meeting": {
    "fallback": {
      "description": "FALLBACK ONLY: Used when item-level processing unavailable (3.4% of meetings)",
      "variables": ["text"],
      "template": "This is a city council meeting agenda that could not be broken into individual items..."
    }
  }
}
```

**Response Schema (item.unified - v3):**
```json
{
  "type": "object",
  "properties": {
    "summary_markdown": {
      "type": "string",
      "description": "Summary in markdown format. Length scales with consequence, not document length."
    },
    "topics": {
      "type": "array",
      "items": {
        "type": "string",
        "enum": ["housing", "zoning", "transportation", "budget", "public_safety",
                 "environment", "parks", "utilities", "economic_development",
                 "education", "health", "planning", "permits", "contracts",
                 "appointments", "other"]
      },
      "description": "1-3 canonical topics from the allowed list"
    }
  },
  "required": ["summary_markdown", "topics"]
}
```

**v3 Changes from v2:**
- Removed `citizen_impact_markdown` and `confidence` fields (simplified schema)
- Added four classification levels controlling output depth:
  - **ROUTINE** (2-3 sentences): Appointments, routine renewals, procedural filings
  - **STANDARD** (4-6 sentences): Budget amendments, permits, contract awards
  - **CONSEQUENTIAL** (7-12 sentences, with sections): Ordinances, development projects, zoning changes
  - **MAJOR** (12-20 sentences, narrative prose): Fiscal reports, multi-document council files, credit analyses
- Added editorial filter step: "What is actually changing?" vs. carried-forward provisions
- Expanded extraction rules for enforcement mechanisms, procurement methods, upstream legislative references
- 7 worked examples covering the full spectrum from triennial code adoptions to fiscal status reports
- Meeting prompts consolidated to single `fallback` key (removed short_agenda/comprehensive split)

**Unified Prompt Design:**

The v3 prompt uses a four-step analysis pipeline:
1. **Read and Classify** - Determine item category (ROUTINE/STANDARD/CONSEQUENTIAL/MAJOR)
2. **Editorial Filter** - What is changing? What's the procedural arc? Who is accountable?
3. **Extract Details** - Concrete details filtered by editorial judgment
4. **Build Summary** - Write summary matching the depth category

Extraction rules cover:
- **Legislative documents:** Each new/modified provision, penalties, enforcement mechanisms
- **Data presentations:** Key metrics, before/after comparisons, trends, frameworks
- **Staff memos:** Recommendations, alternatives, cost-benefit with numbers
- **Appeals/variances:** Backstory, procedural history, conflict, outcome reasoning
- **Fiscal reports:** Central fiscal tension, root causes (not just amounts), reserve status, services at risk
- **Multi-document council files:** Decision arc, split votes with names, counter-proposals

**Meeting prompts (fallback only):**
- Plain markdown output (no JSON, no topics)
- Used when item-level processing is unavailable (vendor limitations, parsing failures)

---

## Topic Normalization (analysis/topics/normalizer.py)

**Maps raw LLM topics → 16 canonical topics** for consistent frontend display and filtering.

### Canonical Topics (taxonomy.json)

16 categories covering civic government:

```python
CANONICAL_TOPICS = [
    "housing",              # Housing & Development
    "zoning",               # Zoning & Land Use
    "transportation",       # Transportation & Traffic
    "budget",               # Budget & Finance
    "public_safety",        # Public Safety
    "environment",          # Environment & Sustainability
    "parks",                # Parks & Recreation
    "utilities",            # Utilities & Infrastructure
    "economic_development", # Economic Development
    "education",            # Education & Schools
    "health",               # Public Health
    "planning",             # City Planning
    "permits",              # Permits & Licensing
    "contracts",            # Contracts & Procurement
    "appointments",         # Appointments & Personnel
    "other"                 # Other
]
```

### Normalizer Logic

```python
class TopicNormalizer:
    """Normalizes extracted topics to canonical taxonomy"""

    def normalize(topics: List[str]) -> List[str]:
        """
        Normalize raw topics to canonical forms.

        Process:
        1. Direct match: "housing" → "housing"
        2. Synonym match: "affordable housing" → "housing"
        3. Word-boundary partial match: "affordable housing plan" → "housing"
        4. No match: log to unknown_topics.log for taxonomy expansion

        Returns: Sorted list of canonical topics (deduplicated)
        """

    def normalize_single(topic: str) -> str:
        """Normalize a single topic. Returns canonical form or lowercased original if no match."""

    def get_display_name(canonical_topic: str) -> str:
        """Get human-friendly display name (e.g., 'public_safety' → 'Public Safety')."""

    def get_all_canonical_topics() -> List[str]:
        """Get list of all canonical topic strings for validation."""

    def get_prompt_examples() -> str:
        """Get comma-separated topic list for embedding in LLM prompts."""
```

**Usage:**
```python
from analysis.topics.normalizer import get_normalizer

normalizer = get_normalizer()  # Global singleton
raw_topics = ["Affordable Housing", "bike lanes", "budget"]
canonical = normalizer.normalize(raw_topics)
# Returns: ["budget", "housing", "transportation"]
```

**Why normalize topics?**
- **Consistent filtering:** Frontend can filter by "housing" reliably across all cities
- **User-friendly labels:** "Housing & Development" via `get_display_name()`
- **Analytics:** Aggregate topic frequency across all cities
- **Taxonomy evolution:** Unknown topics logged to `{DB_DIR}/unknown_topics.log` for review

---

## Error Handling

**Broad exception catches are intentional** at API boundaries - comments in code explain each one. All exceptions are converted to typed errors (`LLMError`, `ExtractionError`, `AnalysisError`) with context.

### 1. Rate Limiting (429 / RESOURCE_EXHAUSTED)

Handled reactively via `_call_with_retry()`:
- Parses `retryDelay` from Gemini's 429 error response (handles multiple format styles)
- Fallback: exponential backoff (30s, 60s, 90s)
- Total retry time capped at 180s (3 minutes)
- Batch processing: 5 items per chunk with 120s delays between chunks

### 2. Truncated Responses (MAX_TOKENS)

When Gemini truncates output mid-JSON:
- `_salvage_truncated_response()` extracts summary/topics via regex
- Adds truncation notice to recovered summary
- Falls back to error if no summary content salvageable

### 3. Empty Responses

When `response.text` is None:
- `_extract_text_from_response()` navigates the candidates/parts structure
- Skips thinking blocks, extracts text from content parts
- Logs prompt_feedback for debugging safety blocks

### 4. Invalid Topics

When Gemini returns topics not in the canonical taxonomy:
- Invalid topics are rejected and logged as warnings
- Falls back to `["other"]` if all topics are invalid

### 5. PDF Extraction Failures

```python
async def extract_pdf_async(url: str) -> Dict[str, Any]:
    """
    Extract text from PDF with error handling.
    Downloads async, extracts in thread pool, raises ExtractionError on failure.
    """
```

---

## Cost Optimization Strategies

### 1. Model Selection

- **Flash (default):** Consistent quality for all items
- **Flash-Lite (opt-in via `USE_FLASH_LITE` config):** 50% cost savings for simple items (<50 pages, <200K chars)

### 2. Unified Prompt

Single prompt with four classification levels (ROUTINE/STANDARD/CONSEQUENTIAL/MAJOR) replaces separate standard/large prompts. The LLM scales output depth to consequence, not document length, reducing over-generation on simple items.

### 3. Thinking Budget

Disabled (`budget=0`) for simple documents (≤10 pages), saving thinking tokens. Dynamic for complex documents.

### 4. Batch Processing

- **50% cost reduction** via Gemini Batch API
- JSONL file upload method with async polling
- Trade-off: minutes of latency (acceptable for background processing)

### 5. Context Caching

```python
# Create cache for shared meeting context (≥1024 estimated tokens)
if len(shared_context) // 4 >= 1024:
    cache = client.caches.create(
        model=flash_model_name,
        config=CreateCachedContentConfig(
            contents=[shared_context],
            ttl="3600s"  # 1 hour
        )
    )
    # Reuse cache across all items in meeting, cleanup in finally block
```

---

## Metrics

All LLM calls record metrics via the `MetricsCollector` protocol:

```python
self.metrics.record_llm_call(
    model="flash",
    prompt_type="item_unified",
    duration_seconds=duration,
    input_tokens=input_tokens,
    output_tokens=output_tokens,
    cost_dollars=self._calculate_cost(model_name, input_tokens, output_tokens),
    success=True
)
```

Cost calculation uses per-model pricing:
- Flash: $0.075/1M input, $0.30/1M output
- Flash-Lite: $0.0375/1M input, $0.15/1M output

---

**See Also:**
- [pipeline/README.md](../pipeline/README.md) - How analysis integrates with processing
- [database/README.md](../database/README.md) - How summaries are stored
