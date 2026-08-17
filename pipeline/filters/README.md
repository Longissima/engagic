# pipeline/filters/ — Filtering Architecture

Processing decision logic. Adapters adapt, pipeline decides.

All items are stored to the database. Filtering happens at processing time, in one
place. The `filter_reason` column records why an item was skipped, enabling auditing
and honest metrics.

## The Three Logical Stages

The active item path has 4 filter checkpoints. Attachment-name helpers remain
for legacy matter change detection, but they do not remove documents from
meeting item assembly. These checkpoints still map to 3 questions:

1. **Should we store this?** (ingestion — only test/demo meetings are dropped)
2. **Should we process this?** (queuing)
3. **Should we feed this to the LLM?** (extraction)

## Filter Levels (execution order)

```
Vendor API response
    │
    ├─ 1. Meeting filter ─────── skip entire test/demo meetings
    │
    │  ── stored in database ──
    │
    ├─ 2. Matter type filter ─── skip administrative matter types from queue
    ├─ 3. Enqueue decider ────── skip already-processed meetings/matters
    │
    │  ── added to processing queue ──
    │
    ├─ 4. Processor item filter ─ skip procedural/ceremonial/admin items
    │                              (saved with filter_reason, not summarized)
    │
    │  ── sent to LLM ──
    ▼
  Summary + Topics
```

### 1. Meeting Level — `should_skip_meeting(title)`

**Stage:** Ingestion
**Impact:** Entire meeting skipped, nothing saved
**Patterns:** `MEETING_SKIP_PATTERNS` — mock, test, demo, training, practice
**Called in:** Adapters, before iterating items

### 2. Matter Type Level — `MatterFilter.should_skip()` → `should_skip_matter()`

**Stage:** Queuing
**Impact:** Matter record created (FK constraints need it) but skipped from processing queue
**Patterns:** `SKIP_MATTER_TYPES` — Minutes, IRC, Information Items
**Called in:** `meeting_sync.py`
**Note:** Uses substring matching, not regex. The `MatterFilter` class in
`orchestrators/matter_filter.py` is a thin wrapper — arguably unnecessary.

### 3. Enqueue Decider — `EnqueueDecider` / `MatterEnqueueDecider`

**Stage:** Queuing
**Impact:** Meeting/matter not added to processing queue
**Logic:** (not pattern-based)
- Skip if all items already have summaries
- Skip if meeting already has monolithic summary
- Skip if matter attachments unchanged (hash comparison)
- Skip if matter has no attachments
- Also calculates priority scores (0-150 for meetings, -100-50 for matters)

**Called in:** `meeting_sync.py`, `processor.py`
**Lives in:** `orchestrators/enqueue_decider.py`

### 4. Processor Item Level — `get_skip_reason(title)` / `should_skip_processing(title)`

**Stage:** Extraction
**Impact:** Item saved to DB with `filter_reason` set, skips LLM summarization
**Pattern categories:**
- `PROCEDURAL_PATTERNS` — roll call, pledge, minutes approval, adjournment, public comment period
- `CEREMONIAL_PATTERNS` — proclamations, commendations, birthdays, retirements
- `ADMINISTRATIVE_PATTERNS` — appointments, liquor licenses, fee waivers, signboard permits
**Called in:** `processor.py`
**Rationale:** These have search value but no policy substance worth summarizing.
The `filter_reason` column enables auditing what % of items are filtered and why.

### Legacy matter identity — `is_public_comment_attachment(name)`

**Stage:** Matter change detection only
**Impact:** Excludes low-signal attachments from the legacy substantive hash;
does not filter the current meeting item document cache or model input
**Pattern groups:**
- `PUBLIC_COMMENT_PATTERNS` — public comments, letters, correspondence (20+ patterns)
- `PARCEL_TABLE_PATTERNS` — property lists, assessor data
- `BOILERPLATE_CONTRACT_PATTERNS` — master agreements, T&Cs, insurance certs
- `SF_PROCEDURAL_PATTERNS` — SF-specific routing forms (city-specific!)
- `EIR_PATTERNS` — environmental impact reports

**Called in:** `pipeline/utils.py` compatibility hashing. The current processor
retains all listed revisions and public-comment attachments. Oversized inputs
fail provider preflight unless a high-confidence deterministic representation
exists; no bulk-document content heuristic discards extracted text.

## Files

| File | What it holds |
|------|---------------|
| `item_filters.py` | All pattern constants + filter functions (meeting, processor, matter, attachment) |
| `orchestrators/matter_filter.py` | Thin class wrapper around `should_skip_matter()` |
| `orchestrators/enqueue_decider.py` | Queue enrollment logic + priority scoring |
| `processor.py` | Complete meeting item document assembly |
| `analysis/llm/document_representation.py` | Auditable model-input receipts |

## Known Issues and Debt

### Naming is misleading
- `is_public_comment_attachment()` filters way more than public comments — it also
  catches parcel tables, boilerplate contracts, EIRs, and SF procedural docs. The
  name stuck from when it only handled public comments.
- `item_filters.py` holds meeting filters, matter filters, and attachment filters.
  The name implies one level; it's actually the central registry for all of them.

### City-specific patterns mixed with general ones
- `SF_PROCEDURAL_PATTERNS` is San Francisco-specific, sitting alongside universal
  patterns. As we add more cities, this will get messier. Should either be namespaced
  per city or pulled into city config.

### No unified interface
- `MatterFilter` is a class. Everything else is bare functions + pattern lists.
  There's no common protocol, no consistent signature across levels.

### All patterns are hardcoded Python constants
- 80+ regex patterns that require a code change and deploy to modify. Fine at current
  scale, approaching the point where at least some patterns want to be config-driven.

## How To Add a New Pattern

1. Decide the category: procedural (zero content), ceremonial (names matter, not policy),
   or administrative (record-keeping, no LLM value)?
2. Add the regex to the appropriate list in `item_filters.py`:
   - `PROCEDURAL_PATTERNS`, `CEREMONIAL_PATTERNS`, or `ADMINISTRATIVE_PATTERNS`
3. Use `\b` word boundaries to avoid substring false positives (lesson learned from
   "commendation" matching inside "Recommendation")
4. Add a comment with an example of the real title/name that motivated the pattern
5. Test — there are no unit tests for filters yet (another debt item)

## Future Direction

If this were cleaned up, the 6 levels would consolidate into 3 modules matching the
logical stages:

- **`ingestion_filters.py`** — should we store this? (meeting level only)
- **`queue_filters.py`** — should we process this? (matter type + enqueue decider + processor item)
- **`content_filters.py`** — should we feed this to the LLM? (attachment name + document heuristics)

Each module would:
- Expose a consistent interface (function or protocol)
- Emit structured filter decisions with reasons (for auditability)
- Namespace city-specific patterns separately
- Optionally load patterns from config for hot-reload without deploys

The enqueue decider is the odd one out — it's stateful (checks DB for existing
summaries/hashes) rather than pattern-based. It probably stays as its own thing,
but logically it's part of the queuing stage.
