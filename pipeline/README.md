# Pipeline - Orchestration & Processing

Orchestrates data flow from vendor fetching to LLM analysis to database storage.

---

## Structure

```
pipeline/
  conductor.py      # Daemon lifecycle, CLI entry point
  fetcher.py        # City sync, vendor routing
  processor.py      # Queue processing, item assembly
  models.py         # Job type definitions (Pydantic dataclasses)
  utils.py          # Matter-first utilities
  admin.py          # Debug commands (standalone)
  click_types.py    # CLI validation

  protocols/        # Dependency injection interfaces
  filters/          # Multi-tier filtering logic
  orchestrators/    # Business logic coordinators
  workers/          # (reserved, currently empty)
```

---

## Architecture

```
Conductor
  ├─> Fetcher    (city sync, rate limiting)
  │       └─> MeetingSyncOrchestrator (store + enqueue)
  └─> Processor  (queue processing)
           ├─> orchestrators/  (business logic)
           └─> filters/        (skip decisions)
```

**Key patterns:**
- Conductor delegates to Fetcher and Processor
- Processor uses orchestrators for business logic
- MeetingSyncOrchestrator coordinates all sync-side decisions
- Metrics injected via Protocol (no server dependency)

---

## Module Reference

### 1. `conductor.py` - Orchestration (~940 lines)

**Entry point for all pipeline operations.** Coordinates sync and processing loops.

#### Responsibilities
- Start/stop background daemon (async tasks)
- Sync loop (runs every 24 hours)
- Processing loop (continuously processes queue)
- Admin commands (force sync, status, preview)
- Watchlist operations (user-demanded cities)
- Global state management (graceful shutdown via `asyncio.Event`)

#### Key Methods

```python
conductor = Conductor(db=database_instance, metrics=optional_metrics)

# Lifecycle (context manager for cleanup)
async with Conductor(db) as conductor:
    await conductor.force_sync_city("paloaltoCA")
    await conductor.sync_and_process_city("paloaltoCA")
    await conductor.preview_queue(city_banana="paloaltoCA")
    status = await conductor.get_sync_status()

# Multi-city (sync_cities returns list, process_cities yields per-city)
results = await conductor.sync_cities(["paloaltoCA", "oaklandCA"])
async for result in conductor.process_cities(["paloaltoCA", "oaklandCA"]):
    print(result)

# Combined sync+process (async generator)
async for result in conductor.sync_and_process_cities(["paloaltoCA"]):
    print(result)
```

#### CLI Usage

```bash
# Background services
engagic-conductor daemon       # Combined sync + processing (two async tasks)
engagic-conductor fetcher      # Sync only, no processing
engagic-conductor processor    # Processing only, no sync (stale job recovery on start)

# Inspection
engagic-conductor preview-queue paloaltoCA
engagic-conductor status

# Sync/process operations - TARGETS may be city bananas, county bananas, state codes, or @file
# (comma-separated, mixed forms allowed)
engagic-conductor sync paloaltoCA                          # one city
engagic-conductor sync paloaltoCA,oaklandCA                # multiple cities
engagic-conductor sync montereycountyCA                    # county + linked cities
engagic-conductor sync GA                                  # every jurisdiction in Georgia
engagic-conductor sync @cities.txt                         # from file
engagic-conductor process GA
engagic-conductor sync-and-process paloaltoCA

# Watchlist operations (user-demanded cities)
engagic-conductor preview-watchlist
engagic-conductor sync-watchlist
engagic-conductor process-watchlist

# Admin
engagic-conductor full-sync           # Sync all active cities
engagic-conductor city-requests       # Show pending user city requests
engagic-conductor extract-text MEETING_ID --output-file text.txt
engagic-conductor preview-items MEETING_ID --extract-text
```

#### Async Architecture
- **Single event loop:** Uses `asyncio.create_task()` for concurrent loops
- **Sync task:** Runs every 24 hours (calls `fetcher.sync_all()`)
- **Processing task:** Runs continuously (calls `processor.process_queue()`)
- **Graceful shutdown:** `asyncio.Event`-based, checked via `is_running` property; SIGTERM/SIGINT handlers
- **Interruptible sleep:** 1-second poll interval during 72-hour waits (immediate shutdown response)

---

### 2. `fetcher.py` - City Sync & Vendor Routing (~370 lines)

**Fetches meetings from vendor platforms.** Handles rate limiting and database storage via `MeetingSyncOrchestrator`.

#### Responsibilities
- Sync all cities (vendor-grouped, rate-limited)
- Adaptive sync scheduling (high activity = more frequent)
- Vendor-aware rate limiting (`AsyncRateLimiter`)
- Parallel city sync within vendor groups (semaphore-controlled concurrency)
- Meeting + item storage via `MeetingSyncOrchestrator.sync_meeting()`
- Failed city tracking
- Chunker hint seeding: once per Fetcher lifetime, loads each city's last
  winning chunker rung from persisted audits (`queue.get_chunker_hints()`)
  into the router's sticky-routing registry (see vendors/README, PDF
  Chunking Cascade)

#### Key Methods

```python
fetcher = Fetcher(db=db, metrics=optional_metrics)

# Sync all active cities (vendor-grouped, rate-limited)
results: List[SyncResult] = await fetcher.sync_all()

# Sync specific cities
results = await fetcher.sync_cities(["paloaltoCA", "oaklandCA"])

# Single city sync
result: SyncResult = await fetcher.sync_city("paloaltoCA")
```

#### SyncResult Object

```python
@dataclass
class SyncResult:
    city_banana: str
    status: SyncStatus  # COMPLETED, FAILED, SKIPPED
    meetings_found: int = 0
    meetings_processed: int = 0
    meetings_skipped: int = 0
    items_stored: int = 0
    duration_seconds: float = 0.0
    error_message: Optional[str] = None
```

#### Sync Flow

1. **Group cities by vendor** (primegov, legistar, novusagenda, iqm2, etc.)
2. **Prioritize by activity** (high activity cities first via `_prioritize_cities()`)
3. **Check sync schedule** (`_should_sync_city()` - adaptive intervals)
4. **Parallel sync** with semaphore (`CITY_SYNC_CONCURRENCY = 2` per vendor)
5. **Apply rate limiting** (`AsyncRateLimiter.wait_if_needed(vendor)`)
6. **Fetch meetings** (`adapter.fetch_meetings()`)
7. **Store via orchestrator** (`MeetingSyncOrchestrator.sync_meeting()`)
   - Creates Meeting + AgendaItem objects
   - Tracks matters (city_matters + matter_appearances)
   - Looks up or creates committees
   - Enqueues for processing (meetings and matters separately)
8. **Track failures** (`failed_cities` set)
9. **Vendor break** (30-40s between vendor groups)

#### Adaptive Sync Scheduling

Based on meeting frequency in the last 30 days:

```python
# High activity (8+ meetings/month): Sync every 12 hours
# Medium activity (4-7 meetings/month): Sync every 24 hours
# Low activity (< 4 meetings/month): Sync every 7 days
# Never synced: Always sync
```

#### Vendor Rate Limiting

- **Per-vendor rate limiting** via `AsyncRateLimiter.wait_if_needed(vendor)`
- **30-40 second break** between vendor groups
- **Polite crawling** to avoid overloading civic tech platforms

---

### 3. `processor.py` - Queue Processing & Item Assembly (~930 lines)

**Processes jobs from the queue.** Extracts text from PDFs, assembles items, orchestrates LLM analysis.

#### Responsibilities
- Process queue continuously (`process_queue()`) — streaming lane
- Batch API lane (`_batch_lane_loop()`) — non-urgent meetings at 50% cost
- Extract text from PDFs (via `AsyncAnalyzer.extract_pdf_async()`)
- Multi-tier filtering (procedural items, public comments, EIRs, boilerplate)
- Document-level caching (deduplication within meeting)
- Document version filtering (keep latest version only)
- Batch item processing
- Topic normalization and aggregation
- Participation info extraction and merging
- Incremental saving (per-chunk)
- Public comment compilation detection (page count, OCR ratio, signature patterns)

#### Processing Lanes (urgency-routed)

Meeting jobs split into two lanes at dequeue time
(`queue.get_next_for_processing(lane=...)`, joined against `meetings.date`):

- **Streaming lane** (JOB_CONCURRENCY=6, JOB_TIMEOUT=1500s): meetings inside
  the urgent window `[now - BATCH_URGENT_PAST_DAYS, now + BATCH_URGENT_FUTURE_DAYS]`
  (default: next 24h), matter jobs, and undated meetings. Concurrent
  single-item Gemini calls — summaries in seconds-to-minutes. The window
  exists because special meetings only require ~24h posted notice and their
  summaries must not land after the meeting happened.
- **Batch lane** (BATCH_JOB_CONCURRENCY=3, BATCH_JOB_TIMEOUT=7200s):
  everything else, routed through `summarizer.summarize_batch` — Gemini
  Batch API at 50% token cost on a **separate quota pool** (bulk work never
  competes with fresh meetings for TPM). Jobs park on Gemini poll loops, so
  the lane heartbeats `queue.started_at` every 5min to stay clear of the
  stale sweep, and gets its own generous wall-clock ceiling.

Lanes are disjoint SQL predicates over `FOR UPDATE SKIP LOCKED` — no double
claims. A meeting whose date drifts into the urgent window between retries
switches lanes automatically. Kill switch: `ENGAGIC_BATCH_API_ENABLED=false`
restores single-lane behavior. Note: `process_city_jobs()` claims
streaming-only; its non-urgent leftovers are drained by the daemon's batch
lane.

#### Key Methods

```python
processor = Processor(db=db, analyzer=optional_analyzer, metrics=optional_metrics)

# Continuous queue processing (production)
await processor.process_queue()

# Process specific city (admin/testing)
stats = await processor.process_city_jobs("paloaltoCA")
# Returns: {"processed_count": 5, "failed_count": 1, "items_processed": 12, ...}

# Process single meeting (internal)
await processor.process_meeting(meeting)  # Agenda-first: items > packet

# Process matter across appearances (internal)
await processor.process_matter(matter_id, meeting_id, {"item_ids": [...]})

# Cleanup
await processor.close()  # Closes analyzer + vendor HTTP sessions
```

#### Processing Paths

**Path 1: Item-Level Processing (PRIMARY)**
```
Meeting has agenda_items
  ├─ Filter already-processed items (reuse canonical summaries from matters)
  ├─ Filter procedural items (minutes, roll call, etc.)
  ├─ Build document cache (meeting-level, shared URLs)
  │   ├─ Filter document versions (keep latest Ver2 over Ver1)
  │   ├─ Filter low-value attachments (public comments, EIRs, boilerplate)
  │   ├─ Extract each unique PDF once concurrently (semaphore-limited)
  │   └─ Detect public comment compilations (page count, OCR ratio, signatures)
  ├─ Separate shared vs item-specific documents
  ├─ Build batch requests (item-specific text, shared context separate)
  ├─ Extract participation info from first/last items
  ├─ Process via AsyncAnalyzer.process_batch_items_async()
  │   └─ Generator yields chunks as they complete
  ├─ Normalize topics (via TopicNormalizer)
  ├─ Save incrementally (per-chunk, not end-of-batch)
  ├─ Store canonical summaries for items with matter_ids
  ├─ Aggregate topics to meeting level
  ├─ Merge participation info to meeting
  └─ Free document_cache memory immediately
```

**Path 2: Monolithic Processing (FALLBACK)**
```
Meeting has packet_url (no items)
  ├─ Process via AsyncAnalyzer.process_agenda_with_cache_async()
  └─ Store meeting summary + participation
```

**Path 3: Matter Processing (via queue)**
```
MatterJob from queue (matter_id + item_ids)
  ├─ Validate matter_id format and extract banana
  ├─ Aggregate unique attachments from all item appearances
  ├─ Process representative item via _process_single_item()
  ├─ Store canonical_summary + attachment_hash in city_matters
  └─ Backfill all item appearances with canonical summary
```

#### Document Caching (Item-Level Path)

**Problem:** Multiple items reference the same PDF -> extract once, reuse many times.

**Solution:** Meeting-level document cache with shared/item-specific separation.

```python
# Document cache keyed by URL
document_cache = {
    "https://example.com/staff_report.pdf": {
        "text": "...",
        "page_count": 45,
        "name": "staff_report.pdf"
    }
}

# Shared documents (referenced by 2+ items) go in meeting-level context
shared_context = "=== staff_report.pdf ===\n{text}"

# Item-specific documents go in per-item request
batch_request = {
    "item_id": "item_123",
    "title": "Approve Contract",
    "text": "=== contract.pdf ===\n{text}",  # Item-specific only
    "page_count": 12
}

# Items with only shared attachments use title/description as anchor text
```

#### Multi-Tier Filtering

**Meeting level - skip entire meeting (test/demo):**

```python
MEETING_SKIP_PATTERNS = ["mock", "test", "demo", "training", "practice"]
```

**Processor level - save all items, skip LLM for low-value ones (with filter_reason):**

```python
# Three categories, merged into PROCESSOR_SKIP_PATTERNS:
PROCEDURAL_PATTERNS = ["roll call", "invocation", "pledge of allegiance", ...]
CEREMONIAL_PATTERNS = ["proclamation", "commendation", "recognition", ...]
ADMINISTRATIVE_PATTERNS = ["appointment", "liquor license", "signboard permit", ...]
```

**Attachment level - skip low-value documents:**

```python
# Public comments: "public comment", "correspondence received", ...
# Parcel tables: "parcel table", "property list", "assessor", ...
# Boilerplate contracts: "omnia partners", "sourcewell", "master agreement", ...
# SF procedural: "ceqa det", "myr memo", "hearing notice", ...
# Environmental reports: "feir", "deir", "environmental impact report", ...
```

**Matter type level - skip administrative matters:**

```python
SKIP_MATTER_TYPES = ["Minutes", "IRC", "Information Item", "Information Only", ...]
```

#### Public Comment Compilation Detection

Runtime detection of bulk scanned compilations:

```python
# Excessive page count (>1000 pages)
# High OCR ratio on large docs (>50 pages, >30% OCR)
# Repetitive signatures (>20 "sincerely," in text)
```

#### Incremental Saving

**Generator-based processing:** Save results immediately after each chunk completes.

```python
chunks = await analyzer.process_batch_items_async(batch_requests, ...)
for chunk_results in chunks:
    for result in chunk_results:
        await db.items.update_agenda_item(item_id, summary, topics)
    # If crash occurs, already-saved items are preserved
```

---

### 4. `models.py` - Job Type Definitions (~150 lines)

**Type-safe job payload definitions using Pydantic dataclasses.** Enables exhaustive type checking, runtime validation, and safe dispatch.

#### Responsibilities
- Define job types (MeetingJob, MatterJob)
- Serialize/deserialize payloads to/from JSON
- Helper functions for job creation
- Database row to job object conversion

#### Job Types

```python
from pydantic.dataclasses import dataclass  # Runtime validation

@dataclass
class MeetingJob:
    """Process a meeting (monolithic or item-level)"""
    meeting_id: str

@dataclass
class MatterJob:
    """Process a matter across all its appearances (matters-first)"""
    matter_id: str  # Composite ID: {banana}_{matter_key}
    meeting_id: str  # Representative meeting where matter appears
    item_ids: List[str]  # All agenda item IDs for this matter

@dataclass
class QueueJob:
    """Typed queue job with discriminated union payload"""
    id: int
    job_type: JobType  # "meeting" | "matter"
    payload: JobPayload  # MeetingJob | MatterJob
    banana: str
    priority: int
    status: str
    retry_count: int = 0
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
```

#### Helper Functions

```python
# Create meeting job
job_data = create_meeting_job(
    meeting_id="paloaltoCA_2025-11-10",
    banana="paloaltoCA",
    priority=150
)

# Create matter job
job_data = create_matter_job(
    matter_id="sanfranciscoCA_251041",
    meeting_id="sanfranciscoCA_2025-11-10",
    item_ids=["item_1", "item_2", "item_3"],
    banana="sanfranciscoCA",
    priority=150
)

# Deserialize from database row
job = QueueJob.from_db_row(db_row)
```

#### Type Safety

**Discriminated union pattern:** `job_type` field determines which payload type is present.

```python
# Type-safe dispatch
if job.job_type == "meeting":
    assert isinstance(job.payload, MeetingJob)
    process_meeting(job.payload.meeting_id)
elif job.job_type == "matter":
    assert isinstance(job.payload, MatterJob)
    process_matter(job.payload.matter_id, job.payload.item_ids)
```

---

### 5. `utils.py` - Matter-First Utilities (~220 lines)

**Utilities for matter-first processing.** Attachment hashing and matter key extraction.

#### Responsibilities
- Generate stable attachment hashes for deduplication
- Extract canonical matter keys from vendor data
- Combine date/time strings from vendor APIs
- Support URL-only or metadata-enhanced hashing modes

#### Key Functions

```python
from pipeline.utils import hash_attachments, hash_attachments_fast, get_matter_key, combine_date_time

# Hash attachments for deduplication (URL-only mode, fast)
attachments = [att1, att2]  # AttachmentInfo objects with url, name attrs
hash_value = hash_attachments_fast(attachments)  # SHA256 hex digest

# Hash with metadata (slower, better change detection)
hash_value = hash_attachments(attachments, include_metadata=True)
# Fetches Content-Length and Last-Modified headers via HEAD requests

# Extract canonical matter key (prefer semantic ID over UUID)
matter_key = get_matter_key(matter_file="25-1234", matter_id="uuid-abc")
# Returns: "25-1234" (semantic ID preferred)

# Combine date and time strings from vendor APIs
combined = combine_date_time("2025-11-18T00:00:00", "6:30 PM")
# Returns: "2025-11-18T18:30:00"
```

#### Attachment Hashing Strategy

**Two modes:**

1. **URL-only (default):** Fast, but misses CDN rotations
   ```python
   hash_attachments_fast(attachments)
   # Hashes: [(url, name), (url, name), ...] sorted, SHA256
   ```

2. **Metadata-enhanced:** Slower, better change detection
   ```python
   hash_attachments_with_metadata(attachments, timeout=3)
   # Hashes: [(url, name, content_length, last_modified), ...]
   # Makes HEAD requests to get metadata; falls back to URL-only on failure
   ```

**Use case:** Detect when matter attachments have changed across appearances.

#### Matter Key Strategy

**Problem:** Vendors use different identifiers for legislative matters.

- **Semantic IDs:** Public-facing (e.g., "BL2025-1098", "25-1234")
- **Backend UUIDs:** Internal tracking (e.g., "uuid-abc-123")

**Solution:** Prefer semantic ID over UUID.

```python
get_matter_key("BL2025-1098", "12345")  # Returns: "BL2025-1098"
get_matter_key("251041", "uuid-...")     # Returns: "251041"
get_matter_key(None, "uuid-abc")         # Returns: "uuid-abc"
```

---

### 6. `admin.py` - Admin & Debug Utilities (~200 lines)

**Standalone debug utilities for manual inspection.** Not used in production daemon, only via CLI commands. Each function creates its own Database connection.

#### Responsibilities
- Extract and preview text from meeting PDFs
- Preview agenda items with optional text extraction
- Save extracted text to files

#### Key Functions

```python
from pipeline.admin import extract_text_preview, preview_items

# Extract full text to file (standalone, creates own DB connection)
result = await extract_text_preview(
    meeting_id="paloaltoCA_2025-11-10",
    output_file="text.txt"
)

# Preview agenda items (with optional text extraction and output directory)
result = await preview_items(
    meeting_id="paloaltoCA_2025-11-10",
    extract_text=True,
    output_dir="./debug_output/"
)
```

**CLI Usage:**
```bash
engagic-conductor extract-text MEETING_ID --output-file debug.txt
engagic-conductor preview-items MEETING_ID --extract-text --output-dir ./debug/
```

---

### 7. `click_types.py` - CLI Parameter Types (~60 lines)

**Custom Click parameter types for CLI validation.**

#### Responsibilities
- Validate city_banana format
- Provide clear error messages for invalid inputs

#### BananaType Validator

```python
from pipeline.click_types import BananaType, BANANA

# Used in CLI commands (BANANA is the singleton instance)
@click.command()
@click.argument("banana", type=BANANA)
def sync_city(banana: str):
    """Sync a single city"""
    pass
```

**Validation:**
- Format: lowercase alphanumeric (with optional hyphens) + uppercase 2-letter state code (`^[a-z0-9-]+[A-Z]{2}$`)
- Covers all jurisdiction types: cities (`paloaltoCA`), counties (`alamedacountyCA`), transit authorities (`bartCA`), utilities (`ebmudCA`)
- Examples: `paloaltoCA`, `nashvilleTN`, `alamedacountyCA`, `ebmudCA`
- Invalid: `PaloAltoCA` (capital), `paloaltoca` (lowercase state), `paloalto` (missing state)

---

### 8. `orchestrators/` - Business Logic Coordinators

Four orchestrators coordinate complex workflows across repositories:

#### `MeetingSyncOrchestrator` (~580 lines)
Main coordinator for sync operations. Called by Fetcher.

```python
orchestrator = MeetingSyncOrchestrator(db)
meeting, stats = await orchestrator.sync_meeting(meeting_dict, city)
```

**Responsibilities:**
- Transform vendor meeting dict to Meeting + AgendaItem models
- Generate deterministic IDs (meeting, item, matter) via `database.id_generation`
- Look up or create committees (vendor_body_id preferred, title-parsing fallback)
- Track legislative matters (new vs duplicate appearances)
- Create matter_appearances with committee and sequence tracking
- Process votes and update outcomes (via VoteProcessor)
- Enqueue meetings and matters for LLM processing (separate priority tiers)
- Preserve existing summaries and processing state on resync
- Deduplicate items by matter_id before DB operations
- Detect first meeting for city and notify subscribed users (city activation emails)
- Record sponsor-to-matter links and vote records for council members
- Handle enqueue failures gracefully (meeting data committed, jobs recoverable via re-sync)

**Stats returned:**
```python
MeetingStoreStats = {
    'items_stored': int,
    'items_skipped_procedural': int,
    'matters_tracked': int,       # New matters created
    'matters_duplicate': int,     # Existing matters updated
    'meetings_skipped': int,
    'appearances_created': int,
    'skip_reason': Optional[str],
    'skipped_title': Optional[str],
    'enqueue_failures': int,
}
```

#### `EnqueueDecider` (~45 lines, in enqueue_decider.py)
Determines if meetings should be enqueued for processing.

```python
decider = EnqueueDecider()
should_enqueue, reason = decider.should_enqueue(meeting, items, has_items)
priority = decider.calculate_priority(meeting_date)
```

**Logic:**
- Skip if all items already have summaries
- Skip if meeting already has summary (monolithic)
- Priority based on date proximity (0-150 scale)

#### `MatterEnqueueDecider` (~35 lines, in enqueue_decider.py)
Determines if matters should be enqueued for processing. Lower priority than meetings.

```python
decider = MatterEnqueueDecider()
should_enqueue, reason = decider.should_enqueue_matter(
    existing_matter, attachment_hash, has_attachments
)
priority = decider.calculate_priority(meeting_date)  # Returns -100 to 50
```

**Logic:**
- Skip if no attachments
- Enqueue if new matter or no canonical_summary
- Enqueue if attachment_hash changed (re-process)
- Priority: 50 - days_distance (lower than meetings' 0-150)

#### `MatterFilter` (~12 lines)
Filters out administrative/procedural matter types. Delegates to `filters.should_skip_matter()`.

```python
filter = MatterFilter()
if filter.should_skip(matter_type):
    # Skip Minutes, IRC, Information Items, etc.
    # Still creates Matter record (for FK), just skips LLM queue
```

#### `VoteProcessor` (~23 lines)
Computes vote tallies and determines outcomes. Delegates to `database.vote_utils`.

```python
processor = VoteProcessor()
result = processor.process_votes(votes)
# Returns: {"tally": {"aye": 5, "nay": 2}, "outcome": "passed"}

# Also available separately:
tally = processor.compute_tally(votes)
outcome = processor.determine_outcome(tally)
```

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│ Conductor (Async Event Loop)                                    │
│  ├─ Sync Task (every 24 hours)                                  │
│  │   └─> Fetcher.sync_all()                                     │
│  │                                                               │
│  └─ Processing Task (continuous)                                │
│      └─> Processor.process_queue()                              │
└─────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ Fetcher (City Sync)                                             │
│  ├─ Group cities by vendor                                      │
│  ├─ Prioritize by activity                                      │
│  ├─ Parallel sync (semaphore concurrency=2 per vendor)          │
│  ├─ Rate limit per vendor (AsyncRateLimiter)                    │
│  ├─ Adapter.fetch_meetings()                                    │
│  └─ MeetingSyncOrchestrator.sync_meeting()                      │
│      ├─ Store Meeting + AgendaItem objects                      │
│      ├─ Track matters (city_matters + matter_appearances)       │
│      ├─ Look up / create committees                             │
│      ├─ Record votes and sponsor links                          │
│      ├─ EnqueueDecider → Enqueue meetings for processing        │
│      ├─ MatterEnqueueDecider → Enqueue matters for processing   │
│      └─ City activation notifications (first meeting detected)  │
└─────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ Processing Queue (PostgreSQL)                                   │
│  ├─ Priority-based (recent meetings first)                      │
│  ├─ Typed jobs (MeetingJob, MatterJob)                          │
│  ├─ Retry logic (retry_count tracked, DLQ on threshold)         │
│  └─ Status tracking (pending, processing, completed, failed)    │
└─────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ Processor (Queue Processing)                                    │
│  ├─ Get next job from queue                                     │
│  ├─ Dispatch by job type:                                       │
│  │   ├─ MeetingJob → process_meeting()                          │
│  │   └─ MatterJob → process_matter()                            │
│  │                                                               │
│  ├─ ITEM-LEVEL PATH (if meeting has items):                     │
│  │   ├─ Filter already-processed (reuse canonical summaries)    │
│  │   ├─ Build document cache (concurrent extraction)            │
│  │   ├─ Build batch requests (shared context + item text)       │
│  │   ├─ AsyncAnalyzer.process_batch_items_async() [generator]   │
│  │   ├─ Save incrementally (per-chunk)                          │
│  │   ├─ Store canonical summaries for matter items              │
│  │   └─ Aggregate topics + participation to meeting             │
│  │                                                               │
│  ├─ MONOLITHIC PATH (if packet_url only):                       │
│  │   ├─ AsyncAnalyzer.process_agenda_with_cache_async()         │
│  │   └─ Store meeting summary                                   │
│  │                                                               │
│  └─ MATTERS-FIRST PATH (MatterJob):                             │
│      ├─ Aggregate unique attachments across all appearances     │
│      ├─ Process representative item                             │
│      ├─ Store canonical_summary + attachment_hash               │
│      └─ Backfill all item appearances with canonical summary    │
└─────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ AsyncAnalyzer (LLM Analysis) - lives in analysis/              │
│  ├─ extract_pdf_async()                                         │
│  ├─ process_batch_items_async() [generator]                     │
│  └─ process_agenda_with_cache_async()                           │
└─────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│ Database (PostgreSQL via asyncpg)                               │
│  ├─ meetings.update_meeting_summary()                           │
│  ├─ items.update_agenda_item()                                  │
│  ├─ matters.store_matter()                                      │
│  └─ queue.mark_processing_complete()                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Processing Queue Architecture

**Location:** `database/repositories/queue.py` (managed by `database/`)

**Schema:**
```sql
CREATE TABLE queue (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL UNIQUE,  -- Deduplication key
    meeting_id TEXT,
    banana TEXT,
    job_type TEXT,              -- "meeting" or "matter"
    payload JSONB,              -- MeetingJob or MatterJob data
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter')),
    priority INTEGER DEFAULT 0,
    retry_count INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    failed_at TIMESTAMP
);
```

**Deduplication keys:**
```python
# Meetings: "meeting://{meeting_id}"
# Matters:  "matter://{matter_id}"
```

**Status Flow:**
```
pending → processing → completed
                    └──> failed (retry_count tracked)
                    └──> dead_letter (threshold exceeded)
```

**Priority Scoring:**
```python
# Meetings: max(0, 150 - days_distance)    → range 0-150
# Matters:  max(-100, 50 - days_distance)   → range -100 to 50
# Meetings always processed before matters
```

---

## Configuration

**Required Environment Variables:**
```bash
GEMINI_API_KEY=your_api_key_here
POSTGRES_HOST=localhost
POSTGRES_DB=engagic
POSTGRES_USER=engagic
POSTGRES_PASSWORD=***
```

**Optional:**
```bash
NYC_LEGISTAR_TOKEN=token_for_nyc_api  # NYC requires API token
ENGAGIC_LOG_LEVEL=INFO
```

---

## Common Operations

### Local Development (One-Off Sync)

```bash
# Sync single city (or county banana, or state code, or @file)
engagic-conductor sync paloaltoCA

# Sync and process immediately
engagic-conductor sync-and-process paloaltoCA

# Preview queue
engagic-conductor preview-queue paloaltoCA

# Extract text for debugging
engagic-conductor extract-text MEETING_ID --output-file text.txt
```

### Production Deployment (Background Services)

```bash
# Fetcher service (sync only, no processing)
engagic-conductor fetcher

# Processor service (processing only, no sync)
engagic-conductor processor

# Full daemon (sync + processing in one process)
engagic-conductor daemon
```

**Deployment:** VPS runs two systemd services:
1. **`engagic-fetcher.service`** - Syncs cities every 24 hours
2. **`engagic-processor.service`** - Processes queue continuously (recovers stale jobs on startup)

---

## Error Handling & Retry Logic

### Sync Errors (Fetcher)

**Single attempt per city** (default `max_retries=1` = one try, no retries):
```python
# Attempt: Fetch meetings, store via orchestrator
# If failed: Add to failed_cities set, log error
# Error delay: 2s + jitter before returning
```

### Processing Errors (Processor)

**Retry:** Handled at queue level via `mark_processing_failed()`
```python
# On failure: Error recorded, retry_count incremented
# Non-retryable: "Analyzer not available" (no API key) - no retry increment
# Queue poll: 5s interval; 10s backoff after fatal errors
# Interruptible waits: Shutdown signal breaks any wait immediately
```

### Enqueue Errors (MeetingSyncOrchestrator)

**Graceful degradation:** Meeting data is committed in a transaction first, then jobs are enqueued separately. If enqueue fails, meeting data is preserved and jobs can be recovered via re-sync.

---

## Performance Characteristics

- **Sync cycle:** ~2 hours for 500 cities (rate-limited)
- **Item processing:** 10-30s per item (Gemini latency)
- **Batch processing:** Cost savings over individual calls
- **Document caching:** Reduces extraction costs for shared attachments
- **Incremental saving:** Prevents data loss on crashes
- **Memory:** Document cache cleared immediately after processing each meeting

---

## Key Patterns

1. **Agenda-First:** Check for items before falling back to packet_url
2. **Matters-First:** Deduplicate summarization work across meetings
3. **Document Caching:** Extract shared PDFs once per meeting
4. **Incremental Saving:** Save results per-chunk, not end-of-batch
5. **Generator-Based:** Yield chunk results immediately (don't buffer)
6. **Multi-Tier Filtering:** Meeting > adapter > processor > attachment > matter-type
7. **Graceful Shutdown:** `asyncio.Event`-based, interruptible waits throughout
8. **Idempotent Sync:** Preserves existing summaries and processing state on resync
9. **Canonical Summaries:** Process matter once, backfill all appearances

---

## Related Modules

- **`vendors/`** - Adapter implementations for civic tech platforms
- **`parsing/`** - PDF text extraction and participation parsing
- **`analysis/`** - LLM summarization and topic normalization (includes `AsyncAnalyzer`)
- **`database/`** - Repository pattern for data persistence (asyncpg)

**Note:** The `AsyncAnalyzer` class lives in `analysis/analyzer_async.py`, not `pipeline/`. Processor imports and uses it.

---

**Last Updated:** 2026-02-10 (Audit: fixed line counts, SyncResult fields, admin.py signatures, CLI commands, filtering tiers, retry logic, added parallel sync/committee/city activation/processor CLI, updated data flow diagram, corrected Analyzer references)
