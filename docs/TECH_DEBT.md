# Technical Debt Register

> Historical snapshot (2026-04-12). Several pipeline entries below were resolved
> or replaced by the 2026-08-07 remediation. Current pipeline residuals are
> maintained in [`PIPELINE_PERFORMANCE.md`](PIPELINE_PERFORMANCE.md); settled
> correctness and rollout state live in
> [`PIPELINE_REMEDIATION.md`](PIPELINE_REMEDIATION.md).

Architectural opportunities identified but deferred. Review when modifying related code.

Last audit: 2026-04-12

---

## High Priority (Address When Touching)

### Sync cycle wastes 10-14 minutes sleeping unconditionally
- **File:** `pipeline/fetcher.py:141-144`
- **Issue:** After every vendor's cities finish, the fetcher sleeps 30-40 seconds unconditionally (`30 + random.uniform(0, 10)`), regardless of whether rate limiting was actually engaged. With 21 vendors, this is 10-14 minutes of pure sleeping per sync cycle.
- **Fix:** Only sleep if the rate limiter actually delayed during that vendor's sync. Track whether `wait_if_needed()` fired, and skip the inter-vendor sleep if it didn't.
- **Effort:** Low (10 min)
- **Trigger:** Immediate -- free performance win

### City prioritization is sequential and double-queries the DB
- **File:** `pipeline/fetcher.py:328-367`
- **Issue:** Two problems in one:
  1. `_prioritize_cities()` at line 365 uses `[(await get_priority(city), city) for city in cities]` -- sequential awaits. With 100 cities at ~300ms per DB round-trip pair, this takes 30s when `asyncio.gather()` could do it in 300ms.
  2. `_prioritize_cities()` calls `get_city_meeting_frequency()` + `get_city_last_sync()` per city, then `_should_sync_city()` calls the exact same two queries again. 4 DB round-trips per city where 2 would do (400 wasted queries per sync with 200 cities).
- **Fix:** (a) Replace sequential comprehension with `asyncio.gather()`. (b) Compute priority and sync eligibility in one pass -- return `(priority, should_sync)` from a single function that queries once.
- **Effort:** Low-Medium (30 min)
- **Trigger:** Immediate -- 30-120 seconds of sync latency eliminated

### N+1 item fetches in process_matter
- **File:** `pipeline/processor.py:480-486`
- **Issue:** When `metadata["item_ids"]` contains N item IDs, makes N separate `get_agenda_item()` database calls instead of a single batch query. Typical matters have 3-20 items.
- **Fix:** Add `get_agenda_items_by_ids(item_ids: List[str])` method using `WHERE id = ANY($1::text[])`. Call it once instead of N times.
- **Effort:** Low (20 min)
- **Trigger:** Immediate -- eliminates 3-20 queries per matter processing job

### Legistar fetches matter details and sponsors sequentially
- **File:** `vendors/adapters/legistar_adapter_async.py:478-523`
- **Issue:** `_fetch_matter_metadata_async()` fetches matter details (HTTP call), waits for response, then fetches sponsors (second HTTP call). These are independent requests to different endpoints.
- **Fix:** `asyncio.gather(matter_task, sponsors_task)` instead of sequential awaits.
- **Effort:** Low (10 min)
- **Trigger:** When touching Legistar adapter -- saves ~500ms per item for Legistar cities

### ~~parsing/participation.py returns dicts, not models~~ RESOLVED
Now returns `ParticipationInfo` models directly (`parsing/participation.py:20,155`).

### Temporal snapshot freeze has no DB-level enforcement
- **Files:** `database/repositories_async/items.py` (UPSERT CASE), `pipeline/orchestrators/meeting_sync.py:148-163`
- **Issue:** The invariant "once an item has a summary, it is frozen" is enforced only by a SQL `CASE WHEN items.summary IS NOT NULL` in the UPSERT. No trigger, no CHECK constraint. Any code path that writes `summary = NULL` to an item silently breaks the freeze -- the item gets re-processed on next sync, potentially with different LLM output.
- **Fix:** Add a trigger or partial index/constraint that prevents clearing a non-NULL summary, or add a `frozen_at` timestamp column.
- **Effort:** Low (trigger) to Medium (schema change + backfill)
- **Trigger:** Any change to item update paths or the temporal snapshot model

### Adapter output has no schema validation
- **Files:** `vendors/adapters/*.py` -> `pipeline/orchestrators/meeting_sync.py:46`
- **Issue:** All adapters return `List[Dict[str, Any]]`. The orchestrator accesses keys by convention (`meeting_dict.get("title")`, `.get("items")`, `.get("vendor_id")`). If an adapter returns the wrong shape, it fails deep in `sync_meeting()`, not at the adapter boundary. Dec 2025 review proposed `MeetingSchema` -- never implemented.
- **Fix:** TypedDict or Pydantic model for adapter output, validated at the `fetch_meetings()` return boundary.
- **Effort:** Medium (define schema + update 21 adapters)
- **Trigger:** Next time an adapter is added or an orchestrator crash is debugged

### Fetcher retry logic is effectively dead code
- **File:** `pipeline/fetcher.py:294-326`
- **Issue:** `_sync_city_with_retry(max_retries=1)` -- the loop runs once, meaning zero retries. The `wait_times = [5, 20]` array is never indexed. Transient network failures cause a city to be marked failed until the next 24h sync cycle.
- **Fix:** Change default to `max_retries=2` or `3`. Or remove the misleading retry wrapper and add proper exponential backoff.
- **Effort:** Low
- **Trigger:** Any investigation into city sync reliability

### SQL Query Duplication in SearchRepository
- **File:** `database/repositories_async/search.py:48-81, 117-134`
- **Issue:** `search_meetings_fulltext()` and `search_meetings_by_topic()` duplicate nearly identical SQL queries for banana filter vs no filter. Only difference is `AND banana = $N` clause.
- **Fix:** Extract query builder with conditional WHERE clause construction
- **Effort:** Low-Medium
- **Trigger:** Next time search queries are modified

### Chained .replace() Anti-pattern
- **File:** `server/services/flyer.py:253-264`
- **Issue:** 12 consecutive `html.replace()` calls, each allocates new string
- **Fix:** Use dict-based substitution loop
- **Effort:** Low
- **Trigger:** Next time flyer template logic is touched

### SVG Literal Duplication
- **File:** `server/services/flyer.py:298-303, 308-313`
- **Issue:** Identical fallback SVG placeholder appears twice (in try block and exception handler)
- **Fix:** Extract to module-level constant `DEFAULT_LOGO_SVG`
- **Effort:** Trivial
- **Trigger:** Any modification to `_generate_logo_data_url()`

### Server routes return raw dicts
- **Files:** `server/routes/engagement.py`, `server/routes/feedback.py`, `server/routes/matters.py`
- **Issue:** Endpoints return `{"success": True, ...}` dicts instead of Pydantic response models
- **Fix:** Add response models in `server/models/responses.py`, use in route return types
- **Effort:** Medium (5-10 new model classes)
- **Trigger:** Next API contract change or frontend type generation work

### Scanned PDFs lose legislative formatting (strikethrough/underline)
- **File:** `parsing/pdf.py`
- **Issue:** Legislative formatting detection relies on PyMuPDF detecting vector line objects. Scanned PDFs are pure images - strikethrough/underline are pixels, not vectors. OCR extracts text but loses ALL formatting, meaning deleted text appears as valid law.
- **Example:** Mount Airy NC agenda (Konica copier scan, 25 pages) - Section 7 amendments have struck-through text that OCR outputs as regular text with no `[DELETED:]` tags.
- **Current behavior:** `_detect_horizontal_lines()` finds nothing, `_has_legislative_legend()` fails (no text layer to search), OCR runs but formatting is lost.
- **Impact:** Legally incorrect extraction - deleted provisions appear as current law.

**Detection logic needed:**
```python
# In extract_from_bytes/extract_from_url:
is_fully_scanned = (ocr_pages == total_pages)  # Every page triggered OCR
has_legislative_content = any(kw in text.lower() for kw in ['whereas', 'ordinance', 'resolution', 'hereby'])

if is_fully_scanned and has_legislative_content:
    # Can't trust formatting - need vision model or flag as uncertain
```

**Proposed fix:** Use Gemini Vision for scanned legislative pages
```python
from google import genai
from google.genai import types

def extract_legislative_with_vision(image_bytes: bytes) -> str:
    """Use Gemini Vision to extract text with formatting from scanned page."""
    prompt = """Extract all text from this scanned legislative document.

    CRITICAL: Identify visual formatting:
    - Text with a line THROUGH it = strikethrough = output as [DELETED: text]
    - Text with a line UNDER it = underline = output as [ADDED: text]
    - Regular text = output normally

    Preserve paragraph structure. Be precise about which text has lines through/under it."""

    response = client.models.generate_content(
        model="gemini-2.0-flash-exp",
        contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt]
    )
    return response.text
```

**Cost consideration:** ~$0.01-0.02 per page for vision. A 25-page scanned packet = ~$0.25-0.50. Only triggers for fully-scanned PDFs with legislative content.

**Alternative (cheaper, less accurate):** OpenCV line detection on rendered page images, correlate line positions with OCR bounding boxes. High effort, medium accuracy.

**Effort:** Medium (vision integration) or High (OpenCV approach)
**Trigger:** When onboarding cities that use copier scans for agenda packets
**Note:** Most cities use native PDFs. This affects smaller municipalities with older document workflows.

---

## Medium Priority (Note for Future)

### Rate limiter opens new SQLite connection per request
- **File:** `server/rate_limiter.py:214-663`
- **Issue:** `check_rate_limit()` opens 4-6 separate `sqlite3.connect()` calls per API request (ban check, tier lookup, hourly/daily/minute tracking). At moderate traffic (500+ req/min) this becomes the bottleneck -- synchronous file system lock negotiation on every request.
- **Fix:** Keep a persistent in-process connection or use an in-memory cache with periodic SQLite flush (e.g., write every 100 requests or 60s). Alternative: replace with Redis if horizontal scaling is needed.
- **Effort:** Medium (2-3 hours)
- **Trigger:** When API traffic exceeds ~500 req/min, or when profiling shows rate limiter latency

### Dashboard makes N+1 queries per watched city
- **File:** `server/routes/dashboard.py:85-123`
- **Issue:** For each watched city (up to 5), makes separate `get_happening_items()` + `get_upcoming_meetings()` calls. 10 DB queries where 2 batch queries (`WHERE banana = ANY($1)`) would work.
- **Fix:** Add `get_happening_items_batch(bananas)` and `get_upcoming_meetings_batch(bananas)` methods, group results by banana in Python.
- **Effort:** Low (30 min)
- **Trigger:** When touching dashboard or optimizing user-facing latency

### Engagement stats make 2 queries per entity
- **File:** `server/routes/engagement.py:85-134`
- **Issue:** `get_watch_count()` + `is_watching()` are separate DB calls for the same entity. Could be one query: `SELECT COUNT(*), SUM(CASE WHEN user_id = $1 THEN 1 ELSE 0 END) FROM watches WHERE entity_type = $2 AND entity_id = $3`.
- **Effort:** Low (15 min)
- **Trigger:** When touching engagement routes

### Raw console.* Calls in Frontend (worsened)
- **Files:** 17 files with 22 instances (was 9 files / 11 instances in Dec 2025)
- **Issue:** Using raw `console.error/warn/debug` instead of structured logger service. Count has doubled since last audit -- new server-side routes added without using the logger service.
- **Fix:** Replace with `logger.error()`, `logger.warn()`, `logger.debug()` from `$lib/services/logger`
- **Effort:** Low (mechanical replacement)
- **Trigger:** When touching any of these files

### Type-unsafe Admin State
- **File:** `frontend/src/routes/admin/+page.svelte:14-15`
- **Issue:** Uses `any` type for metrics and activities state
- **Fix:** Define proper TypeScript interfaces for admin dashboard data
- **Effort:** Low
- **Trigger:** When extending admin dashboard functionality

### analysis/ lacks intermediate result types
- **Files:** `analysis/analyzer_async.py`, `analysis/topics/normalizer.py`
- **Issue:** Analysis pipeline passes dicts between stages; no `ExtractionResult`, `SummaryResult`, `TopicsResult` types
- **Fix:** Create typed result objects for pipeline stages
- **Effort:** Medium
- **Trigger:** When adding caching, retry logic, or new analysis stages

### Deliberation module domain modeling
- **Files:** `database/repositories_async/deliberation.py`, `scripts/moderate.py`
- **Issue:** New module, currently dict-heavy. No `Comment`, `Deliberation`, `ModerationDecision` domain objects with behavior
- **Fix:** Define domain types with methods (e.g., `PendingComment.approve()`)
- **Effort:** Medium
- **Trigger:** Before adding features like moderator notes, comment threading, or vote aggregation

### userland/matching returns dicts
- **Files:** `userland/matching/*.py`
- **Issue:** Matching logic returns raw dicts instead of `MatchResult` objects
- **Fix:** Create `MatchResult` type, return from matchers
- **Effort:** Low-Medium
- **Trigger:** When adding match explanation or debugging features

---

## Low Priority (Long-Term)

### ~~Two agenda chunkers with unclear canonical status~~ RESOLVED (documented)
Both are active by design. v2 (leaf-to-root: hyperlinks + position clustering) is preferred; v1 (root-to-leaf: regex per page) is fallback. Base adapter dispatches: `v2 auto -> v2 toc -> v2 url -> v1 fallback`. Different PDFs need different approaches. Not dead code; not consolidation candidates.

### MCP SQL validator allows dangerous function calls
- **File:** `mcp_server.py:49-61`
- **Issue:** `validate_readonly_sql()` blocks write keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`) and requires `SELECT` prefix. But it doesn't filter function calls. `SELECT pg_sleep(3600)` (DoS) and `SELECT pg_read_file('/etc/passwd')` (file read, requires superuser) pass validation.
- **Fix:** Add function call detection (blocklist `pg_sleep`, `pg_read_file`, `pg_write_file`, `lo_import`, etc.) or parse the SQL AST with a lightweight parser.
- **Effort:** Low (blocklist) to Medium (AST parsing)
- **Trigger:** If MCP is ever exposed to less-trusted clients. Currently behind bearer token auth.

### Queue retries lack time-based backoff
- **File:** `database/repositories_async/queue.py:333-335`
- **Issue:** Failed jobs move back to `pending` immediately with lower priority (`-(20 * retry_count)`) but no delay. A transient issue that resolves in 30s can cause a job to exhaust all 3 retries in 3 seconds and land in dead letter.
- **Fix:** Add `retry_at TIMESTAMP` column. On failure, set `retry_at = NOW() + interval * 2^retry_count`. Modify `get_next_for_processing()` to add `AND (retry_at IS NULL OR retry_at <= NOW())`.
- **Effort:** Low-Medium (schema change + query update)
- **Trigger:** Any investigation into premature dead letter arrivals

### Vendor-specific logic in base adapter
- **File:** `vendors/adapters/base_adapter_async.py`
- **Issue:** Base class contains Legistar JSON Accept header preference in `_request()` and Granicus SSL bypass (`if self.vendor == "granicus" or "granicus.com" in url`). The base class should not know about specific vendors.
- **Fix:** Move vendor-specific behavior to subclass overrides (e.g., `_request_headers()` hook, `_ssl_context()` hook).
- **Effort:** Low
- **Trigger:** When adding a new vendor that needs different HTTP behavior

### is_running property pattern duplicated 3x
- **Files:** `pipeline/conductor.py:63-75`, `pipeline/fetcher.py:64-76`, `pipeline/processor.py:124-136`
- **Issue:** Identical 12-line `is_running` property + setter + `_shutdown_event` + `_running` pattern copy-pasted across three classes.
- **Fix:** Extract to a `ShutdownMixin` or shared base class.
- **Effort:** Low
- **Trigger:** Any change to shutdown signaling or lifecycle management

### Gemini pricing hardcoded (stale)
- **File:** `analysis/llm/summarizer.py:83-99`
- **Issue:** Cost calculation hardcodes Gemini pricing with comment "as of Nov 2025". Model names also hardcoded at lines 67-68. Both silently drift as Google updates pricing/models.
- **Fix:** Move pricing to config or a constants file with a "last verified" date. Or just delete the cost calculation -- it's only used for logging.
- **Effort:** Low
- **Trigger:** Any model upgrade or cost anomaly investigation

### Documentation Drift: README.md and module READMEs
- **Files:** `README.md`, `vendors/README.md`, `server/README.md`
- **Issue:** README claims "~41,000 lines" (actual: 58K), "19 adapters" (actual: 21). vendors/README adapter count and server/README route module count are stale.
- **Fix:** Update counts. Consider generating from code.
- **Effort:** Trivial
- **Trigger:** When updating any project documentation

### ~~parsing/pdf.py monolith (28K lines)~~ OUTDATED
File is now 841 lines (not 28K as previously recorded). Either the original measurement was wrong or the file was significantly refactored. At 841 lines it's manageable; this is no longer a high-priority decomposition target.

### MCP bearer token not constant-time
- **File:** `mcp_server.py:184`
- **Issue:** Bearer token comparison uses string equality, not `secrets.compare_digest()`. Theoretically vulnerable to timing attacks that leak token length/characters.
- **Fix:** Replace `auth_value != f"Bearer {self.token}"` with `secrets.compare_digest()`.
- **Effort:** Trivial
- **Trigger:** Any MCP auth change

### Magic link tokens accumulate without cleanup
- **File:** `database/schema_userland.sql:78-86`
- **Issue:** `userland.used_magic_links` stores hashed tokens for replay protection but has no TTL or cleanup. Rows accumulate indefinitely after expiry.
- **Fix:** Add periodic cleanup job: `DELETE FROM userland.used_magic_links WHERE expires_at < NOW()`.
- **Effort:** Trivial
- **Trigger:** When userland table sizes become noticeable

### Dead letter queue accumulates without TTL
- **File:** `database/repositories_async/queue.py:273-286`
- **Issue:** Permanently failed jobs moved to `dead_letter` status with error messages preserved. No cleanup mechanism -- rows grow indefinitely.
- **Fix:** Add periodic cleanup: `DELETE FROM queue WHERE status = 'dead_letter' AND failed_at < NOW() - INTERVAL '30 days'`.
- **Effort:** Trivial
- **Trigger:** When queue table size becomes noticeable

---

## Future Architecture (Product Evolution)

### Governance model too council-centric
- **Tables:** `council_members`, `votes`, `committee_members`, `committees`
- **Issue:** Current model assumes council members are the key actors. But CA city-manager cities work differently:
  - Commissions (citizen appointees) make substantive recommendations
  - City Manager has real executive authority
  - Council ratifies (often ceremonially, unanimous consent)
- **Reality discovered:** Sunnyvale has 20+ commissions with 131 office records, but our model only captures 7 council members. Planning Commission, Arts Commission, etc. have voting members we ignore.
- **API limitation:** Many Legistar cities (Sunnyvale, San Jose) don't expose `/Votes` or `/EventItems` endpoints - vote data unavailable via API.

**What already exists:**
- `matter_appearances` tracks matter across meetings with `committee_id`, `action` (free text), `vote_outcome` (enum), `vote_tally` (jsonb)
- `committees` table with basic info
- `votes` table links individual votes to matter + meeting + council_member

**Proposed schema evolution:**

```sql
-- Generalize council_members
officials
├── id
├── city_id (banana)
├── name
├── role_type: enum('elected', 'appointed', 'staff')
├── position: text ("Council Member", "Commissioner", "City Manager", "Director")
└── term_start, term_end (nullable for staff)

-- Membership table for multi-body relationships
official_memberships
├── official_id
├── body_id
├── role: text ("Chair", "Vice Chair", "Member")
├── start_date, end_date

-- Generalize committees
bodies
├── id
├── city_id (banana)
├── name
├── body_type: enum('council', 'committee', 'commission', 'board', 'authority')
├── authority_level: enum('legislative', 'advisory', 'administrative', 'executive')
└── parent_body_id (nullable, for subcommittees)

-- Extend matter_appearances
matter_appearances (add columns)
├── action_type: enum('approval', 'recommendation', 'motion', 'consent', 'referral')
└── (migrate `action` free text to action_type enum)

-- votes unchanged, but council_member_id -> official_id
```

**Key insight:** `authority_level` distinguishes advisory (commissions recommend) from legislative (council approves). This enables tracking the governance flow:
```
Matter originates
    ↓
Commission reviews (authority_level='advisory')
    - action_type='recommendation'
    ↓
Council ratifies (authority_level='legislative')
    - action_type='approval' or 'consent'
```

**Migration path:**
1. Rename `committees` → `bodies`, add `body_type`, `authority_level`, `parent_body_id`
2. Rename `council_members` → `officials`, add `role_type`, `position`
3. Rename `committee_members` → `official_memberships`
4. Add `action_type` enum to `matter_appearances`
5. Update adapters to populate new fields
6. Update frontend to show governance flow

**Effort:** High (schema migration, adapter updates, frontend rework)
**Trigger:** When building features that need "who actually decided this" or commission-level tracking
**Note:** Core value for CA cities is summaries + matter tracking. Full governance model matters more for cities with real political dynamics. Prioritize accordingly.

---

## Architecture Strengths (Preserve These)

- **Repository pattern** in `database/` - clear separation, async pooling, typed models
- **Discriminated union jobs** in `pipeline/models.py` - type-safe job dispatch
- **Adapter pattern** in `vendors/` - clean interface, shared HTTP/rate-limit logic, genuinely useful base class
- **Queue system** - `FOR UPDATE SKIP LOCKED`, dead letter queue, stale recovery, priority ordering
- **Pydantic validation at boundaries** - `vendors/schemas.py`, `server/models/requests.py`
- **`city_banana` canonical identifier** - vendor-agnostic, well-documented

---

## Process

When you encounter friction modifying a module:
1. Check if it's listed here
2. If yes: consider whether now is the time to address it
3. If no: add it with context for future reference

Update "Last audit" date when reviewing this document.
