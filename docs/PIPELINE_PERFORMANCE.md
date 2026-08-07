# Sync and Processing Performance Map

Status: implementation baseline and rollout measurement plan, 2026-08-07.

This document maps elapsed time to the stage that owns it. It also explains why
the foreground run appeared to slow down: the old command submitted the complete
Batch API backlog before it began collection, so its visible counter changed from
fast local queue claims to provider-bound work. That phase change was real; the
single cumulative counter was not a valid throughput measurement.

## End-to-end flow

| Stage | Owner and durable boundary | Primary limit | Measurements |
|---|---|---|---|
| Resolve | `Conductor.run_sync_cycle` / jurisdiction repository | database reads | targets, schedules, stage duration, outcome |
| Fetch | typed vendor adapter | municipal host, rate limit | HTTP outcome, meetings, duplicate source IDs |
| Persist | `MeetingSyncOrchestrator` transaction | database writes/locks | meetings/items/matters, fixed snapshot queries |
| Publish | generation-ordered transactional outbox -> queue | database availability | pending/publishing/failed/dead-letter, work generation, attempt age |
| Claim | token-fenced queue | ready backlog | ready wait from `ready_at`, desired-work age from `last_enqueued_at`, claim/service age |
| Acquire | typed document artifact | municipal host/R2 | bytes, media, corpus hit, validation outcome |
| Extract | guarded parser/OCR | CPU/RAM/OCR | pages, OCR pages, extraction duration/failure |
| Summarize | streaming or Batch API | provider latency/quota | tokens/cost, local service time, provider wait |
| Project | version-fenced domain transaction | database locks | success/partial/failure, filled snapshots |

CLI and daemon select a scope and stop policy around the same runtime. A finite
manual process command is complete only when its scoped ready/retry work, outbox
intents, and durable provider jobs are locally terminal or ingested.

## Measured pre-remediation baseline

These are production observations captured during the audit. They are a baseline,
not target SLOs.

| Signal | Observation | Interpretation |
|---|---:|---|
| Batch create -> ingest | p50 599 min; p95 1,603 min; p99 1,795 min | Provider wait dominates elapsed Batch work. |
| Batch history | 8,502 chunks / 57,142 items | Large enough to make phase-aware percentiles meaningful. |
| Matter queue service | p50 10.89 s; p95 210.28 s | Local processing long tail, excluding provider wait. |
| Meeting queue service | p50 1.93 s; p95 189.87 s | Most claims are short; extraction/LLM create the tail. |
| Sync jurisdiction sample | p50 51.5 s; p95 170.2 s across 115 timed jurisdictions | Vendor and document work dominate, with avoidable DB amplification in the old path. |
| Corpus | about 149k documents, 275 GB, 1.548m pages, 15.1% OCR | OCR is a page-level resource tail, not a document category. |
| Queue payload drift | 21,819 / 99,230 matter jobs mismatched appearance counts | Snapshot payloads became stale. |
| Missing appearances | 30,481 current appearances absent from payloads | Jobs did not represent authoritative matter state. |
| Missing unsummarized work | 2,238 jobs omitted a current unsummarized appearance | Concrete correctness and completeness failure. |
| Sync recurring outcomes | 314 duplicate vendor IDs; 192 no-item chunk outcomes; 52 HTTP errors | Must be classified separately from successful zero work. |
| Dead letter | 993 rows; 906 were old batch-relation failures | Deployment/schema drift dominated the historical DLQ. |

The protected foreground screen finished without intervention. Its final phase
reported 6,482 streaming completions, 5,782 batch queue submissions, 500 collected
chunks, 77 submission failures, and 4,549 new items. Those counters describe
different units and must not be collapsed into one “meetings processed” rate.

## Removed amplification and roundabout paths

| Prior pattern | Replacement | Expected effect |
|---|---|---|
| Per-matter `get_matter`, item-history, and appearance-membership reads inside a meeting loop | One transaction-scoped set-wise snapshot, with sorted matter then item locks | For six populated matters: 30 (`5N`) SQL reads -> five fixed reads; orchestrator point calls 18 -> three batch calls. |
| Meeting/matter versions hashed proposed scrape objects before COALESCE and freeze-on-summary upserts settled | One post-write meeting -> sorted matters -> appearances lock/reread boundary computes tracking, copies, versions, and publication from retained rows | Queue descriptors cannot disagree with the authoritative snapshot and terminal-fail their own CAS fence. |
| Matter queue payload copied `meeting_id` and `item_ids` | Identity-only `matter_id` plus `mw1` desired version | No stale relationship snapshots; authoritative rows load at claim time. |
| Attachment hash stood in for every summarization input | Separate `sv1` artifact version and `mw1` desired-work version | Title-only changes are visible without treating signed-URL rotation as new work. |
| CLI and daemon owned subtly different loops | `run_sync_cycle` and `run_pipeline_runtime` | One lifecycle, outcome, heartbeat, retry, and completion contract. |
| Batch command submitted everything before collection | Bounded concurrent submitters plus leased collector under one supervisor | Submission and ingestion overlap; progress remains visible by phase. |
| Provider submission existed only after the remote call | Durable pre-provider intent and recovery lease | Crash/timeout ambiguity becomes recoverable and auditable. |
| Heartbeat rewrote claim start; stale completion matched only row ID | Stable `claimed_at`, moving `heartbeat_at`, fresh `claim_token`, work-version fence | Accurate service time and no stale-worker completion. |
| A definitively lost heartbeat let expensive work continue until final transition | `JobRunner` races the handler against claim ownership and cancels/journals abandonment on a false heartbeat | Superseded extraction/LLM work stops at the next heartbeat; transactional CAS remains the immediate commit fence. |
| Procedural no-work reused the executable content version | Bounded `mnw1:<reason>:<digest>` desired-state tombstone plus descriptor/token CAS | Same-content policy changes reopen correctly, while an older in-flight worker cannot commit after invalidation. |
| Queue preview called the claiming query | Pure ordered `preview_jobs` select | Inspection cannot mutate production. |
| Reconciliation locked a queue row before entering the publisher's per-source advisory order | Shared `lock_desired_state` boundary acquires source advisory lock before queue row | Dispatcher and reconciler cannot form the inverse-lock deadlock. |
| Domain commit and enqueue were separate | Transactional outbox with version identity, source-serialized recurrence, leases, and FIFO per aggregate | No silently lost downstream job after a successful sync commit; an A -> B -> A recurrence receives a fresh generation instead of disappearing behind the first A event. |
| Outbox replay/newer-work checks depended on transaction-start clocks and row IDs | One database sequence assigns `work_generation` to intents and `desired_generation` to queue rows; authoritative producers serialize by source before allocating order | Overlapping publishers cannot replace newer desired state, and sequence gaps or commit order are not mistaken for domain truth. |
| Stable URL meant permanent cache hit | validation TTL, ETag/Last-Modified, 304 reuse, archived fail-open, failure backoff | Avoids repeated bytes while detecting changed content and preventing outage storms. |
| Each concurrent consumer acquired the same URL | keyed process-local single-flight | One origin request per source identity per process. |
| Generic “document” assumed PDF | typed artifact from observed bytes/media | HTML and Office content follow the correct extractor and suffix. |
| A failed extra vendor retried the whole city | independently retried primary/extra vendor passes, followed by one city checkpoint | A successful source is never replayed because a sibling source failed. |
| Shutdown between vendor streams still advanced the city checkpoint | explicit cancelled sync outcome, no checkpoint, cancelled parent run | An unfinished extra source remains due on the next schedule. |
| First-city notification happened after commit through per-recipient lookups | advisory-locked first-meeting transition plus deterministic per-user outbox events in the meeting transaction | No lost activation after a successful meeting commit; recipient lookup is set-wise. |
| Orphaned run/attempt/stage rows stayed “running” | one periodic atomic stale-lifecycle recovery alongside queue claim recovery | Operational history closes truthfully after process loss without reclaiming healthy work. |
| Processing surfaces differed on initial stale-claim recovery | One recovery at the canonical processor runtime boundary, used by finite, processor-only, sync-and-process, and combined surfaces | Crash leftovers are reclaimed before every runtime begins, including a supervised restart, without wrapper-specific behavior. |
| Finite drains polled uniformly during future retries | next-due queue/outbox timestamps choose an interruptible bounded sleep | Less empty polling while preserving prompt handling of newly ready work. |
| Outbox completion was fenced only by a process owner | a fresh UUID token on every delivery claim, plus owner/token predicates | An expired slow delivery cannot finalize a replacement claim, including inside one process. |
| A processor exception could leave the combined daemon alive but sync-only | restart-supervised continuous runtimes with fail-fast analyzer validation | A live combined daemon always has a live processor or exits for service supervision. |
| Status/help constructed worker components and touched worker-only OOM policy | Relational-only status construction plus command-scoped OOM adjustment | Inspection avoids parser/LLM/worker startup and worker-only side effects. |

## Operational read model

`PipelineLifecycleRepository.get_operational_snapshot()` returns one aggregate
read model used by status surfaces. It separates:

- queue, Batch, and outbox counts by state;
- active pipeline runs; queue and actionable outbox oldest-ready age;
- unresolved current queue-publication dead letters and tokenless/stale claims;
- unbound submission intents and provider jobs due to poll;
- last-hour attempt success/non-success totals;
- p50/p95 ready-queue wait, desired-work age, and local service milliseconds;
- a DB-backed trailing-24-hour series by job type, lane, and outcome, including
  attempt/item throughput and average queue, desired, and service latency;
- a separate trailing-24-hour Batch series for submitted and terminal chunks,
  item counts, outcome, and provider elapsed time.

Terminal streaming attempts are bucketed by completion hour; a currently running
attempt is bucketed by start hour. Batch provider wait begins at durable
`submitted_at`, when the provider accepts the job, rather than at the earlier local
intent reservation. Migration 035 backfills legacy provider jobs from `created_at`,
so only that historical cohort uses a lower-bound estimate. Counters intentionally
use distinct names such as `batch_queue_completed` and `batch_chunks_collected`.
Queue-publication dead letters are considered unresolved only while they are the
latest unfulfilled source intent. Exact work-version equality proves fulfillment;
otherwise the shared monotonic generation proves whether a later event or queue
write superseded the candidate. FIFO claiming, queue upserts, activity reporting,
and replay all use that ordering, so obsolete work cannot replace newer desired
state even when transactions overlap or commit out of order. Repeated publication
of the same version is a no-op while it is still current; after another generation
supersedes it, the stable event is reopened with a fresh generation. This is the
A -> B -> A recurrence rule.

## Final quiescent production snapshot

At 2026-08-07 18:12 UTC, after the additive schema rollout and with both sync
and processing services intentionally inactive, the lightweight status path
reported 99 pending, 159,841 completed, and 1,018 historical dead-letter queue
rows; 9,980 collected Batch chunks; an empty outbox; and zero active runs,
unresolved current queue/outbox dead letters, tokenless or stale claims,
submission intents, or provider jobs due. The oldest ready/desired age was about
16,968 seconds and will grow while workers remain stopped; that is an operator
state, not evidence of a slower running worker.

The trailing Batch series shows hourly provider submissions from 01:00 through
12:00 UTC and the legacy terminal cohort at 13:00 UTC. Its approximately
19,936,812 ms terminal average is the migration-035 `created_at` estimate for
pre-migration rows, not a new provider measurement. New jobs measure from exact
provider acceptance in `submitted_at`.

## Post-rollout checks

Compare equal windows and scopes; never compare the submit phase of one run with
the collect phase of another.

1. Record `uv run engagic-conductor status` before the first new-code run:
   queue/outbox/Batch states, oldest ready ages, claim-health counts, last-hour
   percentiles, and both hourly performance series.
2. Run one representative finite scope through the manual CLI. Record sync stage
   duration, local queue service, Batch submit rate, provider wait, collection
   rate, extraction failures, bytes/pages/OCR, and final scoped counts.
3. Acquire the same sources again after the document validation TTL. Confirm
   unchanged sources use 304 and changed stable URLs create a new content
   revision. Validation is acquisition-triggered, not a background sweep.
4. Alert on monotonic oldest-ready growth, tokenless/stale processing claims,
   expired submission intents, outbox dead letters, provider jobs past next poll,
   and repeated failure classifications by vendor/media type.
5. Review p50/p95/p99 by stage. End-to-end Batch elapsed time should always report
   local queue wait, local service, and provider wait separately.

Suggested initial operational objectives (tune after two comparable weeks):

- no healthy claim is reclaimed and no stale claim can finalize;
- no committed domain change lacks a publishable/published outbox event;
- queue/outbox oldest-ready age does not grow across successive finite drains;
- fixed matter snapshot query count is independent of agenda matter count;
- origin-validation failures serve archived bytes and retry no more frequently
  than the configured failure backoff;
- every non-success is classified, version-fenced, and visible in attempts/stages.

## Remaining inherent or deferred costs

- Gemini Batch provider wait remains measured in hours and is not removable by
  local concurrency. Overlap and truthful progress reduce idle time and confusion.
- A document artifact is currently materialized as bytes at the analyzer boundary;
  R2 upload/download helpers stream where available, but very large downstream
  extractor inputs still need a bounded resource budget.
- Single-flight is process-local. Content addressing gives cross-process
  convergence, but it does not prevent two processes from validating the same URL.
- Ownership-loss cancellation is observed on the heartbeat cadence (currently up
  to 300 seconds); every domain commit is independently fenced in its transaction,
  so that cadence affects wasted work, not correctness.
- Stable URLs that are fully processed and never acquired again are not revisited
  merely because their validation TTL expires. A periodic validation scheduler is
  a separate future workstream; current freshness guarantees apply at acquisition.
- Email delivery from an outbox is durable and retryable but remains at-least-once:
  a provider success followed by lease-finalization failure can rarely duplicate a
  city-activation message.
- Municipal HTML/JavaScript behavior, malformed Office files, OCR, and vendor HTTP
  reliability remain classified external tails rather than pipeline correctness
  failures.
- Historical matter/queue repair is intentionally dry-run first. Its production
  counts must be reviewed before any explicit execution.
- The built-in time series is a bounded 24-hour operational window, not a permanent
  analytics warehouse. Long-term trends require exporting or retaining the durable
  attempt/Batch rows separately.
