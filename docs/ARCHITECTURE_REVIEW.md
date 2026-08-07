# Architecture Review

> Historical snapshot (2026-04-12). Pipeline paths, line numbers, performance
> findings, and open items below describe the pre-remediation tree. Use
> [`PIPELINE_REMEDIATION.md`](PIPELINE_REMEDIATION.md) for the settled invariants
> and [`PIPELINE_PERFORMANCE.md`](PIPELINE_PERFORMANCE.md) for the current map and
> bounded residuals; do not treat this review as the live pipeline runbook.

**Date:** 2026-04-11 (updated 2026-04-12 with round 2 corrections)
**Prior review:** 2025-12-02 (140 lines, high-level priorities)
**Scope:** ~58,000 lines Python backend, ~17K lines vendor adapters
**Method:** End-to-end flow tracing through actual code paths, then deep dives into adapter internals, database/queue, and security

---

## Verdict

**Not spaghetti. More like lasagna -- well-layered but some layers are too thick and a few are fused together.**

The architecture is sound: clear service boundaries, async throughout, proper repository pattern, structured logging, a thoughtful exception hierarchy. The debt is concentrated in two places: processor + summarizer (2,308 combined lines in the hottest code path) and testing (4% coverage, zero tests on the most complex code). The vendor adapter layer, initially assessed as "copy-paste heavy," is actually healthier than it appears -- see Axis 5 for details.

Maintainability: **7/10** -- you can find things and reason about flow, but touching the processor or adding a vendor adapter requires holding a lot of state in your head.

Extensibility: **6/10** -- adding a new city/vendor is mechanical but tedious. Adding a new processing stage or changing the snapshot model would be surgery.

---

## Major Axes Traced End-to-End

### Axis 1: Data Ingestion

```
Conductor.sync_all()
  -> Fetcher.sync_all()           # pipeline/fetcher.py:78
     -> group cities by vendor
     -> for each vendor (sequential, 30-40s sleep between):
        -> semaphore(3) per vendor
        -> _sync_city_with_retry()  # pipeline/fetcher.py:294
           -> _sync_city()          # pipeline/fetcher.py:184
              -> get_async_adapter() # vendors/factory.py:58
              -> adapter.fetch_meetings() -> FetchResult
              -> for each meeting:
                 -> MeetingSyncOrchestrator.sync_meeting()  # pipeline/orchestrators/meeting_sync.py:46
                    -> parse date, generate IDs
                    -> build Meeting object
                    -> preserve existing processing state if resync
                    -> transaction:
                       store meeting
                       track matters
                       store agenda items
                       copy prior-appearance summaries (temporal snapshot)
                       create matter appearances
                    -> enqueue jobs (outside transaction)
```

**Strengths:**
- `FetchResult` dataclass distinguishes "0 meetings" from "adapter failed" (implemented since Dec 2025 review)
- Adaptive sync scheduling: high-activity cities every 12h, low-activity every 7d (`fetcher.py:328-349`)
- Rate limiter with per-vendor politeness delays and jitter
- Shutdown signaling via `asyncio.Event` -- clean cancellation throughout

**Weaknesses:**
- `_sync_city_with_retry` defaults to `max_retries=1`, meaning zero retries -- the retry wait_times array `[5, 20]` is dead code at default config (`fetcher.py:294-326`)
- `sync_meeting()` is 173 lines inside a single `try` block (`meeting_sync.py:46-219`). Correct, well-commented, extremely difficult to modify safely. This is the densest method in the codebase.
- Factory special-cases Legistar for `api_token` via `**kwargs` (`factory.py:75-76`); other vendors silently ignore unknown kwargs.

**Performance waste in the sync cycle:**
- **Unconditional vendor sleep** (`fetcher.py:141-144`): 30-40s sleep after every vendor regardless of whether rate limiting was engaged. With 21 vendors, 10-14 minutes of pure sleeping per sync.
- **Sequential city prioritization** (`fetcher.py:365`): `[(await get_priority(city), city) for city in cities]` awaits each city one at a time. Each call makes 2 DB queries. With 100 cities at ~300ms/pair, this takes 30 seconds when `asyncio.gather()` could do it in 300ms.
- **Double DB calls per city** (`fetcher.py:328-367`): `_prioritize_cities()` fetches `meeting_frequency` + `last_sync` for every city, then `_should_sync_city()` fetches the same two values again. 4 DB round-trips per city where 2 would do.

### Axis 2: Processing

```
Processor.process_queue()           # pipeline/processor.py:219
  -> poll every 5s, claim up to JOB_CONCURRENCY (3) jobs
  -> asyncio.gather(*jobs)
     -> _dispatch_and_process_job() # pipeline/processor.py:162
        -> if "meeting": process_meeting()
        -> if "matter": process_matter()

process_meeting()                   # pipeline/processor.py:577
  -> get agenda items
  -> if items have content:
     -> _process_meeting_with_items()  # pipeline/processor.py:923
        -> _extract_participation_info()
        -> _filter_processed_items()   # temporal snapshot check
        -> _build_document_cache()     # concurrent PDF extraction, semaphore(6)
        -> _build_batch_requests()     # assemble LLM prompts
        -> _process_batch_incrementally() -> GeminiSummarizer
        -> _store_canonical_summary()  # matter-level dedup
        -> _aggregate_meeting_topics()
        -> update meeting metadata
  -> elif packet_url:
     -> analyzer.process_agenda_with_cache_async()  # monolithic fallback
  -> else: empty stats

process_matter()                    # pipeline/processor.py:464
  -> gather all items for matter
  -> summarize via LLM
  -> bulk_fill_null_item_summaries()  # temporal snapshot fill
  -> store canonical summary on city_matters
```

**Strengths:**
- Clean fallback chain: items-with-content -> packet-as-monolith -> empty
- Incremental batch processing: results saved per-chunk, crash-safe
- Document cache with version filtering (`Ver2 > Ver1`), shared context extraction, public comment detection
- Memory management: `malloc_trim()` after large meetings, `document_cache.clear()` explicit, `MAX_EXTRACTED_TEXT_CHARS` cap at 500K

**Weaknesses:**
- `Processor` is a 1009-line class with all processing logic as methods. The Dec 2025 refactoring plan targeted "under 150 lines" -- never pursued.
- `_dispatch_and_process_job` (`processor.py:162-205`) catches `Exception` and domain errors identically -- both mark failed, log, return True. Bugs become silent "failed jobs" indistinguishable from expected failures.
- `is_running` property pattern copy-pasted identically across Conductor, Fetcher, and Processor (3 copies of same 12 lines).

**Performance waste in the processing path:**
- **N+1 item fetches in `process_matter()`** (`processor.py:480-486`): When `metadata["item_ids"]` contains 5 items, makes 5 separate `get_agenda_item()` calls. A single `WHERE id = ANY($1)` would replace all of them.
- **Legistar sequential matter+sponsors fetch** (`legistar_adapter_async.py:478-523`): `_fetch_matter_metadata_async()` fetches matter details, waits, then fetches sponsors. These are independent HTTP calls -- `asyncio.gather()` would halve per-item time for Legistar cities.

### Axis 3: Temporal Snapshots

The most architecturally interesting and most fragile design in the codebase.

**Model:** An agenda item that reappears across meetings gets its summary "frozen" once written. If attachments haven't changed (hash match), the prior summary is copied at sync time. If attachments changed, the matter gets re-processed and fills nulls.

**Implementation across three layers:**

1. **Sync time** (`meeting_sync.py:148-163`): `copy_summary_from_prior_appearance()` runs inside the transaction, after `store_agenda_items` so the target row exists.

2. **Store time** (`database/repositories_async/items.py`): UPSERT uses `CASE WHEN items.summary IS NOT NULL THEN items.title ELSE EXCLUDED.title END` -- the freeze is enforced in SQL. Five columns get this treatment.

3. **Process time** (`processor.py:662-694`): `_filter_processed_items()` checks `item.summary` to skip already-processed items. Comment at line 662-672 explains the model clearly.

4. **Matter process time** (`processor.py:561-563`): `bulk_fill_null_item_summaries()` fills remaining nulls after matter LLM call.

**Risk:** The freeze invariant lives in a SQL CASE statement. No DB-level constraint or trigger enforces immutability. If any code path writes `summary = NULL` to an item, the freeze breaks silently and the item gets re-processed on next sync. The design is correct but depends on every writer respecting the convention.

### Axis 4: API Layer

```
FastAPI app                         # server/main.py
  -> 16 route modules mounted
  -> middleware: rate limiting, logging, CORS
  -> dependencies: get_db(), get_current_user()
  -> routes -> services (search, flyer) -> repositories -> PostgreSQL
```

**Strengths:**
- Clean route separation, proper FastAPI dependency injection
- JWT auth with timing-safe comparison (`secrets.compare_digest`)
- Rate limiting across per-minute, per-hour, per-day with per-route overrides
- TypedDict response types (e.g., `SearchSuccessResponse`, `SearchAmbiguousResponse`)

**Weaknesses:**
- Every route has identical try/except/HTTPException boilerplate -- functional but noisy
- Email signup rate limiting is in-memory (`server/routes/auth.py:44` -- `defaultdict(list)`) while IP rate limiting persists to SQLite. Server restart resets email limits.

### Axis 5: Vendor Adapters

**17,413 lines across 21 adapters + 3 parsers -- 30% of the codebase.**

```
vendors/
  factory.py              78 lines   # registry + special-case dispatch
  adapters/
    base_adapter_async.py 747 lines  # shared HTTP, dates, PDF, sessions
    legistar_*.py        1368 lines  # API-first + HTML fallback, XML parsing
    granicus_*.py         761 lines   # multi-format HTML, 5 parser dispatch
    civicplus_*.py        697 lines   # URL pattern extraction, site discovery
    municode_*.py         974 lines
    ... 17 more adapters (255-870 lines each)
    custom/
      berkeley_*.py       317 lines   # simple HTML table scraper
      chicago_*.py        785 lines
    parsers/
      agenda_chunker.py  1780 lines  # v1: root-to-leaf (regex per page)
      agenda_chunker_v2.py 1061 lines # v2: leaf-to-root (hyperlinks + position)
      granicus_parser.py 1083 lines
      legistar_parser.py  535 lines
```

**Initial assessment (round 1) called this a "copy-paste kingdom." That was too harsh.**

After reading Legistar, Granicus, CivicPlus, and Berkeley end-to-end, adapters are 50-70% unique code -- but that code is *legitimately* vendor-specific. Legistar needs XML/OData parsing and garbage detection. CivicPlus needs ViewFile URL extraction and site discovery. Granicus needs multi-format HTML parser dispatch across 5 parsers. Berkeley needs Drupal table scraping. This isn't extractable duplication; it's domain variation.

**Base adapter provides real shared value.** All 21 adapters inherit:
- `_get_json()`, `_get()`, `_post()` with retry, timeout, SSL workarounds
- `_parse_date()` handling 14+ date formats (adapters don't reimplement this)
- `_chunk_agenda_then_packet()` for 2-pass PDF extraction (agenda URL -> packet TOC)
- `_resolve_sub_attachments()` for staff report link extraction
- `_validate_meeting()` enforcing required `{vendor_id, title}` and an optional
  ISO `start` (authoritative undated meetings remain undated)
- Session management via `AsyncSessionManager` (per-vendor connection pooling, correct)

**Both agenda chunkers are active by design.** v2 is preferred, v1 is fallback:
```
v2 (auto) -> v2 (force toc) -> v2 (force url) -> v1 (fallback)
```
v2 does leaf-to-root (find hyperlinks first, cluster by position). v1 does root-to-leaf (regex item detection per page). Different PDFs need different approaches.

**The real structural problem is the untyped item schema.** Meeting-level dicts are mostly consistent (`vendor_id`, `title`, optional `start` -- validated by base). But item dicts diverge completely: Legistar items have `matter_id`, `votes`, `sponsors`; Berkeley items have `sponsor`, `recommendation`; PDF-chunked items have `body_text`, `agenda_number`. The orchestrator accesses all of these by convention with no shared contract.

**Error handling varies wildly across adapters:**

| Adapter | Exception handlers | Fallback strategy |
|---------|-------------------|-------------------|
| Legistar | 55 | API -> garbage detection -> HTML (sophisticated) |
| Granicus | 21 | Returns None silently |
| CivicPlus | ~10 | PDF -> HTML (exception-driven) |
| Berkeley | 0 | None (relies on base wrapper) |

**Code smells in base adapter:** `base_adapter_async.py` contains vendor-specific logic that should be in subclasses -- Legistar JSON Accept header in `_request()`, and Granicus SSL bypass via `if self.vendor == "granicus"`. The base class should not know about specific vendors.

**Adding adapter #22 (realistic estimate):**
- Simple HTML scraper (like Berkeley): ~300 lines, 60% new code, 4-6 hours
- API with HTML fallback (like Legistar): ~1000+ lines, 70% new code, 2-3 days
- Multi-format scraper (like Granicus): ~760 lines, 75% new code, 3-4 days

### Axis 6: Queue System

```
Queue table (PostgreSQL)
  -> Processor.process_queue()      # pipeline/processor.py:219
     -> poll every 5s
     -> claim up to JOB_CONCURRENCY jobs via FOR UPDATE SKIP LOCKED
     -> asyncio.gather(*jobs)
     -> on success: mark_processing_complete()
     -> on failure: mark_processing_failed() with retry logic
     -> after 3 attempts: move to dead_letter status
     -> stale jobs (>10 min in processing): reset to pending
```

**This is well-built.** `FOR UPDATE SKIP LOCKED` is the correct pattern for concurrent job claiming. Dead letter queue exists with error messages preserved. Admin endpoint exposes DLQ. Stale job recovery handles worker crashes (jobs stuck `processing` >10 minutes reset to `pending`). Priority-based ordering with composite index `(status, priority DESC, created_at)`.

**One gap:** Retries have no time-based backoff. Failed jobs go right back to `pending` with lower priority (`-(20 * retry_count)`) but no delay. A transient network issue that resolves in 30 seconds can cause a job to fail 3 times instantly and land in dead letter before the issue clears.

**Dead letter accumulation:** No TTL on DLQ jobs. They accumulate indefinitely. Not urgent but eventually grows.

### Security Surface

**Two real findings:**

**1. MCP SQL validator allows dangerous function calls** (`mcp_server.py:49-61`)

Validates queries by checking for write keywords and requiring `SELECT` prefix. Blocks `INSERT`, `UPDATE`, `DELETE`, `DROP`, `COPY`, multi-statement. But doesn't filter function calls:
```sql
SELECT pg_sleep(3600)              -- DoS
SELECT pg_read_file('/etc/passwd') -- file read (if superuser)
```
Protected by bearer token auth. Low risk at current exposure level, but worth hardening if MCP is ever exposed to less-trusted clients.

**2. X-Forwarded-For accepted without proxy validation** (`server/middleware/rate_limiting.py:58-60`)

Falls back to first IP from `X-Forwarded-For` if `CF-Connecting-IP` isn't present. If the API is reachable directly (bypassing Cloudflare), rate limiting can be spoofed via custom headers. Moot if UFW restricts to Cloudflare IPs.

**Minor findings (not urgent):**
- MCP bearer token uses string equality, not `secrets.compare_digest()` (theoretical timing attack)
- Magic links have no per-token `jti` nonce -- JWT secret leak = forge links for any user_id
- Unsubscribe tokens live 365 days with no revocation
- `userland.used_magic_links` has no cleanup job (tokens accumulate indefinitely)
- Admin Prometheus proxy passes user-supplied PromQL directly (requires admin token)

---

## Cross-Cutting Concerns

### Configuration

`config.py` (321 lines) is a global singleton imported by 97 files. Every secret (Postgres password, 6 API keys, JWT secret, Stripe key) lives as a plain attribute. `config.summary()` excludes secrets, but the object itself is one accidental `vars(config)` log away from a leak.

The `JOB_CONCURRENCY` comment at line 90-93 documents real production knowledge:
```python
# Lowered from 4 -> 3 after 2026-04-10 OOM. Combined with city_concurrency=3,
# peak goes from 12 concurrent meetings to 9
```

### Exception Hierarchy

`exceptions.py` (444 lines) is genuinely well-designed:
- `is_retryable` property on every exception -- `DatabaseConnectionError` is retryable, `ValidationError` is not
- Rich context dicts (`vendor`, `city_slug`, `table`, `constraint`)
- Proper inheritance tree: `EngagicError` -> `DatabaseError` -> `DataIntegrityError` -> `DuplicateEntityError`

This is better than most production codebases.

### Testing

**2,348 lines across 5 test files. 4% test-to-code ratio.**

| Test file | Lines | What it covers |
|-----------|-------|----------------|
| `test_postgres_edge_cases.py` | 837 | NULL handling, JSONB, dedup |
| `test_matter_tracking_integration.py` | 499 | Matter lifecycle |
| `test_id_generation.py` | 407 | 3-tier ID fallback hierarchy |
| `test_postgres_load.py` | 355 | Load testing |
| `test_validator.py` | 244 | URL validation |

Tests that exist are meaningful. But **no tests for:** API endpoints, vendor adapters, the processor pipeline, the summarizer, auth, or rate limiting. The most complex code has zero test coverage.

### Dependencies

`pyproject.toml` has no upper bounds on fast-moving deps (`anthropic>=0.64.0`, `pydantic>=2.11.7`). Dev tools (`pyright`, `ruff`, `maturin`) are in runtime deps. Two PDF libraries (`pymupdf`, `pypdf2`) serve unclear separate purposes.

---

## What's Actually Good

1. **Exception hierarchy** -- `is_retryable`, context dicts, proper inheritance
2. **Queue system** -- `FOR UPDATE SKIP LOCKED`, dead letter queue, stale job recovery, priority ordering. Best-engineered subsystem after exceptions.
3. **Async discipline** -- no blocking I/O, proper semaphores, `asyncio.Event` shutdown
4. **Base adapter inheritance** -- genuinely useful shared infrastructure (HTTP, dates, PDF chunking, sessions). Not copy-paste overhead.
5. **Database layer** -- clean repository pattern, connection pooling, JSONB codec, `command_timeout=60`. Indexes well-aligned with query patterns.
6. **Memory management** -- `malloc_trim`, text caps, explicit cache clearing. Battle-tested.
7. **Adaptive sync scheduling** -- high-activity cities sync more often. Simple heuristic, real impact.
8. **Parameterized SQL everywhere** -- no string concatenation for values
9. **Incremental batch saves** -- crash mid-batch doesn't lose completed items
10. **FetchResult** -- distinguishes "0 meetings" from "adapter failed" (fixed since Dec 2025 review)

---

## What Would Break First Under Growth

| Scenario | What breaks | Why |
|----------|------------|-----|
| 10x cities | Fetcher vendor loop | Outer loop is sequential with 30-40s sleep between vendors |
| New processing stage | Processor class | 1009-line monolith with no plugin mechanism |
| Vendor API change | Orchestrator at runtime | No schema validation on adapter output |
| Team grows to 2+ | processor.py, summarizer.py | God files -- two people can't work on processing simultaneously |
| Audit/compliance | Config singleton | Secrets mixed with non-secrets, no access logging |

---

## Status of Dec 2025 Review Items

| Item | Status | Notes |
|------|--------|-------|
| Adapter result types (FetchResult) | **Done** | `vendors/adapters/base_adapter_async.py:50` |
| Single topic storage | **Not done** | Dual JSONB + normalized tables still present |
| Abstract adapter interface | **Not done** | No abc/abstractmethod in vendors/ |
| Alembic migrations | **Not done** | Still manual SQL in `/database/migrations/` |
| Move metrics to shared module | **Done** | `pipeline/protocols/metrics.py` |
| Processor decomposition (target: <150 lines) | **Not done** | Currently 1009 lines |
| Database decomposition (target: <400 lines) | **Partial** | `MeetingSyncOrchestrator` extracted, `db_postgres.py` is 486 lines |

---

## Concentrated Debt

The debt is not spread evenly. Two areas hold most of it:

1. **processor.py + summarizer.py** (2,308 lines combined) -- the hottest code path is the least decomposed. The Dec 2025 refactoring plan proposed 5 extracted workers; only `meeting_metadata.py` was created.

2. **Testing** (4% coverage) -- the most complex code (processor, summarizer, orchestrator) has zero tests. The tests that exist are good but sparse.

**Previously listed but corrected:** The vendor adapter layer (17K lines) was initially assessed as copy-paste heavy. After deep-diving into 4 adapters, the code is 50-70% legitimately vendor-specific. The base adapter provides real shared value. The actual problem is narrower: the untyped item schema (each vendor returns different dict shapes with no contract) and vendor-specific logic leaking into `base_adapter_async.py`.

None of this is sloppy. It's the rational output of shipping fast solo on a constrained box. But the next major feature will cost more than it should because of the processor concentration.

---

## Performance: Time Left on the Table

The sync cycle spends more time sleeping and doing redundant DB lookups than actually fetching data. Fixing the top 3 items would cut sync wall-clock time by 30-50%.

| # | What | Where | Waste | Fix time |
|---|------|-------|-------|----------|
| 1 | Sequential city prioritization | `fetcher.py:365` | 30-120s per sync | 5 min |
| 2 | Double DB calls for city priority+sync check | `fetcher.py:328-367` | 400 queries/sync | 30 min |
| 3 | Unconditional 30-40s vendor sleep | `fetcher.py:141-144` | 10-14 min/sync | 10 min |
| 4 | N+1 item fetches in process_matter | `processor.py:480-486` | 3-20 queries/matter | 20 min |
| 5 | Legistar sequential matter+sponsors HTTP | `legistar_adapter_async.py:478-523` | 500ms/item | 10 min |
| 6 | Rate limiter: new SQLite conn per request | `server/rate_limiter.py` | 50-100ms/req at scale | 2-3 hrs |
| 7 | Dashboard N+1 per watched city | `server/routes/dashboard.py:85-123` | 50-200ms/load | 30 min |

Items 1-3 are the sync cycle. Items 4-5 are the processing hot path. Item 6 only matters at real traffic. Item 7 is user-facing latency.
