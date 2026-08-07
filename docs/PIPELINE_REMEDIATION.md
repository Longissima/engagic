# Pipeline Remediation

Status: implementation, verification, and additive schema rollout complete;
new-code runtime canary and historical reconciliation execution remain explicit
operator steps, 2026-08-07.

This document is the integration record for the sync and processing pipeline
remediation. It supersedes point-in-time recommendations where the running code
has moved on. A work item is complete only when its invariant is represented in
code, focused tests, and end-to-end verification.

## Safety boundary

- The protected `engagic-process-processed` command completed normally. Its
  detached shell remains at the post-run `read` prompt; read-only process checks
  found no Python worker, and no audit command sent input to the screen.
- Do not deploy or restart services during this implementation/verification pass.
  Additive schema changes require explicit migration verification first.
- Reconciliation utilities must default to dry-run and require an explicit
  execution flag.

## Baseline evidence

- The pre-remediation live foreground run submits every Batch API job before
  collecting any.
  At the audit snapshot it had submitted 1,478 chunks / 9,119 items and had not
  polled a result.
- Historical Batch API creation-to-ingestion latency: p50 599 minutes, p95 1,603
  minutes across 8,502 collected chunks.
- Matter queue payload audit: 21,819 of 99,230 jobs had an item-count mismatch;
  30,481 current appearances were absent from payloads; 2,238 jobs had current
  unsummarized appearances but none represented in their payload.
- Queue service time (not end-to-end batch latency): matter p50 10.9 seconds,
  p95 210 seconds; meeting p50 1.9 seconds, p95 190 seconds.
- A partial sync log sample of 115 timed jurisdictions had p50 51.5 seconds and
  p95 170.2 seconds. It contained 314 duplicate source item IDs, 192 chunker
  no-item outcomes, and 52 vendor HTTP errors.
- The live screen buffer contained 198 corpus extraction hits and 59 document
  extraction failures, dominated by HTML document/detail pages treated as PDFs.

## Design laws

### Work identity and truth

1. A queue payload contains immutable identity and desired version, not a
   snapshot of mutable related rows.
2. A worker loads current authoritative state when it claims work.
3. Re-enqueue updates the desired work and lifecycle timestamps. Attempt history
   is append-only and is not erased by deduplication.
4. Only the matter projection owns `city_matters.canonical_summary`; appearance
   processing may update appearance snapshots but never races the projection.
5. Content version answers "what work is desired"; monotonic generation answers
   "which occurrence is newer." Source-serialized publication reopens A after an
   A -> B -> A recurrence with a fresh generation.
6. Matter policy outcomes use a bounded, versioned `mnw1` descriptor rather than
   reusing executable `mw1`. Domain writes lock and compare both the originally
   claimed desired descriptor and claim token, so a same-content tombstone or
   replacement owner fences an in-flight worker without breaking legacy claims.

### Execution

1. CLI and daemon are invocation adapters over one scoped pipeline runtime.
   The daemon uses a continuous stop policy; a manual CLI run drains its
   requested jurisdiction scope to terminal local/provider state and exits.
   Neither surface owns an independent processing algorithm.
2. All queue consumers use one job execution policy for timeout, heartbeat,
   outcome classification, retry scheduling, metrics, and final transition.
3. Business failures are typed outcomes. Returning from a coroutine does not by
   itself mean that the work succeeded.
4. Retryable failures have a time-based `retry_at`; priority is not a backoff
   mechanism.
5. Preview and inspection operations are read-only.

### External work

1. Synchronous provider SDK methods never run on the asyncio event loop.
2. External submissions have a durable local intent/idempotency identity and a
   recoverable transition to the provider job ID.
3. Batch submission and collection overlap. Partial failures are reconciled at
   item/chunk granularity without waiting for the next municipal sync.
4. Poll attempts, next-poll time, error classification, and terminal state are
   durable.
5. Provider elapsed time starts at durable `batch_jobs.submitted_at`, after the
   provider accepts the job; local intent/create time is measured separately.

### Sync persistence

1. Target resolution, schedule inputs, existing matters, and appearances are
   loaded set-wise.
2. A meeting sync uses one database unit of work; repository methods participate
   in the caller's connection instead of acquiring nested pool connections.
3. Meeting/item persistence and downstream work publication are atomic through
   a transactional outbox.
4. Adapter output is validated once at the canonical boundary using the shared
   typed schema. Vendor quirks remain inside adapters.
5. Administrative writers that invalidate or ingest meeting data lock and
   re-read the authoritative meeting/items, compute `work_version`, and publish
   or reactivate in the same transaction.

### Documents

1. Consumers request a typed document artifact rather than independently
   downloading and parsing URLs.
2. Resolution is corpus/source-identity first, network second; acquisition,
   content hashing, archival, media detection, and extraction have one owner.
3. HTML, PDF, and supported office formats are dispatched by observed media,
   not assumed to be PDF from a generic `document` label.
4. Sync archives breadth. Expensive shape/OCR work occurs once behind the shared
   artifact boundary under a global resource budget.

### Observability and deployment

1. Pipeline runs, job attempts, and stage events are durable and correlated by
   IDs across CLI, daemon, and collectors.
2. Record queue wait, service time, provider wait, extraction time, bytes/pages,
   corpus hit, tokens/cost, outcome, and classified failure.
3. Database migrations are a deployment gate. A process cannot start code that
   requires a missing relation or column.
4. Operational dashboards distinguish submitted work, provider-completed work,
   ingested summaries, and user-visible completion.

## Workstreams and acceptance criteria

### Queue and matters

- New matter work is identity/version based and old payloads cannot hide current
  appearances.
- Re-enqueue refreshes mutable fields and lifecycle state without falsifying
  attempt history.
- Canonical matter writes have one owner.
- A dry-run-first reconciliation reports and can requeue the historical affected
  set.

### Job lifecycle

- Daemon and manual CLI commands invoke the same scoped runtime. Batch and
  streaming workers share outcome semantics, timeout, and heartbeat behavior.
- Finite CLI completion means its scoped queue work is terminal and every
  scoped durable provider job has been ingested or classified terminal.
- Retryable, terminal, partial, and successful paths have state-machine tests.
- Stale reclamation cannot create a second active worker for a healthy job.

### Batch lifecycle

- SDK calls are off-loop and concurrency-bounded.
- Submission and collection overlap in the foreground command.
- Provider/local transition failure injection is tested.
- Chunk result ingestion is idempotent and incomplete items are immediately
  recoverable.

### Sync

- Per-city scheduling and target resolution no longer perform sequential N+1
  lookups.
- Meeting persistence uses set-wise prefetch and one transaction connection.
- Unexpected city/vendor exceptions produce explicit failed results.
- Adapter contract tests cover optional fields and malformed outputs.

### Documents

- A corpus original can be processed without another network download.
- HTML document pages either resolve a downloadable artifact or yield useful
  sanitized text with provenance.
- Supported office documents are not written to a `.pdf` temporary path.
- Artifact and extraction behavior has unit tests independent of live vendors.

### Verification

- Focused tests pass for every state transition and failure-injection seam.
- The full non-production test suite and static checks pass.
- Schema and migration definitions agree.
- Final read-only database/log queries re-run the audit metrics and enumerate any
  residual production reconciliation or deployment steps.

## Implementation ledger

| Invariant | Implementation | Focused evidence |
|---|---|---|
| One CLI/daemon runtime | `Conductor.run_sync_cycle`, `Processor.run_pipeline_runtime`, finite/continuous stop policy | `test_pipeline_runtime.py` |
| One job lifecycle | `JobRunner`, typed `JobOutcome`, durable attempts/stages | `test_job_runner.py`, `test_job_outcomes.py` |
| Claim ownership | UUID claim token, stable claim/heartbeat clocks, ID+token+version transition predicates | `test_queue_matter_contracts.py` |
| Read-only inspection | `QueueRepository.preview_jobs` and conductor preview adapter | queue and runtime preview regressions |
| Authoritative matter work | identity-only payload, claim-time appearance load, `sv1` artifact + `mw1` work versions | matter UoW, reconciliation, queue contract tests |
| Stale-write prevention | matter -> items -> desired queue state lock order; content, original descriptor, and claim-token CAS before projection/outcome/snapshot writes | matter UoW, desired-state CAS, and integration tests |
| Set-wise sync | fixed transaction snapshot for matters, prior items, and appearance membership | six-matter 30 -> 5 SQL-read regression |
| Atomic publication | version-keyed transactional outbox, source advisory serialization, recurrence reopening with a fresh generation, generation-ordered FIFO, leased ownership, explicit replay | lifecycle/UoW/PostgreSQL generation tests |
| Authoritative sync publication | one post-write meeting -> sorted matters -> appearances reread computes retained tracking, copies, versions, and outbox intents | meeting-sync retained-row regressions |
| Canonical reconciliation locking | shared source-advisory-before-queue-row boundary used by reconciliation and publisher paths | unit lock-order contract and disposable-PostgreSQL interleaving test |
| Recoverable Batch work | pre-provider intent, bounded off-loop submit, durable provider `submitted_at`, leased poller, partial requeue, expired-intent recovery | `test_batch_lifecycle.py` |
| Truthful finite completion | one supervisor overlaps submit/collect and waits for scoped queue/outbox/provider terminal state | batch/runtime tests |
| Typed artifacts | media sniffing and HTML/PDF/Office/RTF dispatch at one acquisition boundary | `test_document_artifacts.py` |
| Stable-source freshness | validation/observation clocks, conditional HTTP, archived fail-open, failure backoff, single-flight | corpus/artifact tests |
| Shared acquisition owner | corpus-first `DocumentSourceAcquirer` across analyzer, packet, attachment, and staff-report paths; per-session in-flight retirement | acquisition/analyzer/adapter regressions |
| Per-vendor retry isolation | primary and extra vendors retry independently; city checkpoint is separate | sync efficiency tests |
| Interruption-safe sync | cancelled vendor/city outcome prevents checkpoint and cancels the parent run | sync/runtime tests |
| Durable city activation | first-meeting advisory lock, set-wise recipients, deterministic notification outbox events in the meeting transaction | `test_city_activation_outbox.py` |
| Delivery ownership | per-claim outbox UUID plus worker/claim finish fence; per-recipient notification aggregates | lifecycle/activation tests |
| Current-intent replay | one shared monotonic work generation orders outbox intents and direct queue writes; source serialization handles A -> B -> A recurrence; the same predicate gates activity and replay | lifecycle/queue/PostgreSQL generation tests |
| Lifecycle self-healing | atomic stale run/attempt/stage closure independent of queue reclamation | lifecycle repository/runtime tests |
| Daemon supervision | continuous processing runtime restarts after failure; terminal combined-mode failures drain sibling tasks and exit nonzero; every processing surface fails fast without an analyzer | runtime tests |
| Startup recovery parity | finite, processor-only, sync-and-process, combined, and direct shared runtimes recover stale claims through the canonical processor boundary | runtime event-order tests |
| Operational read model | actionable queue/outbox/claim health, last-hour percentiles, and DB-backed 24-hour streaming and Batch throughput series | lifecycle repository/runtime tests |
| Lightweight inspection | status uses a relational-only database construction path; help/inspection skip worker-only OOM adjustment | CLI/runtime construction tests |
| Authoritative admin publication | manual ingest commits meeting/items/outbox together; re-summarization locks current inputs and atomically reactivates the exact version | `test_authoritative_publication_scripts.py` |
| Migration gate | safe migrate CLI, schema-current check at process creation, deploy migration hook | `test_migration_gate.py` |
| Safe manual jobs | service stop no longer kills independent process screens | shell syntax verification and rollout review |
| Honest undated appearances | nullable authoritative appearance date, optional adapter `start`, stable undated meeting identity, and one canonical backend dated/undated meeting slug | nullable-schema, sync-identity, and meeting-URL regressions |

## Schema ledger

| Version | Purpose | Production state at audit handoff |
|---|---|---|
| 029 | pipeline runs, job attempts, stage events, outbox | applied |
| 030 | durable Batch lifecycle | applied |
| 031 | jurisdiction sync lifecycle | applied |
| 032 | leased/FIFO/dead-letter outbox delivery and shared work-generation sequence | applied 2026-08-07 16:43 UTC |
| 033 | queue claim-token ownership, claim clocks, and desired-work generation | applied 2026-08-07 16:43 UTC |
| 034 | document source observation/validation/validators | applied 2026-08-07 16:43 UTC |
| 035 | durable Batch provider-submission clock | applied 2026-08-07 16:43 UTC |
| 036 | nullable authoritative appearance date for undated meetings | applied 2026-08-07 18:11 UTC |

Repeated undated syncs now retain one deterministic `undated` meeting identity.
One identity transition remains intentionally unresolved: if the source later
publishes a real date, the existing date-and-title-inclusive ID algorithm yields
a different ID. The `meetings` table does not persist the source `vendor_id`, so
the old undated meeting and its item/appearance graph cannot be authoritatively
matched and relinked in place. Fixing that safely requires a versioned identity
migration that stores source identity and rewrites every dependent reference;
guessing from title or URLs would risk merging distinct recurring meetings.

Migration 033 is additive at install time: it does not requeue active legacy
workers. Tokenless claims age through the normal stale timeout, avoiding a
migration-created overlap. Its backfill first aborts on ambiguous
active/dead-letter queue/outbox version relationships, then allocates every
queue generation above the outbox high-water mark rather than inventing an
unsafe cross-table order. Migrations 032-036 were applied with the producer and
worker fleet quiescent. Migration 036 changed only the column constraint and
comment; it did not rewrite the 197,618 existing appearance rows. Its rollback
fails closed after any real NULL appearance exists.

## Reconciliation and rollout

`scripts/reconcile_matter_queue.py` is dry-run by default. Its report compares
current appearances, canonical projection versions, missing snapshots, and the
queue descriptor. Explicit execution is a separate operator decision; it uses
identity-only matter payloads and exact-version reactivation.

Controlled rollout order:

1. Full tests, static checks, migration/schema agreement, and independent
   read-only re-audits are complete.
2. The 2026-08-07 schema-only rollout completed with fetcher, processor, and
   manual process workers quiescent; `database.migrate --status` confirmed
   migrations through 036, with no pending migrations. The post-migration
   catalog reports `matter_appearances.appeared_at` nullable and all 197,618
   existing appearances unchanged. No reconciliation or restart was part of
   that step.
3. The full dry-run reconciliation inspected 152,214 matters and proposed
   30,822 authoritative enqueues: 16,438 missing queue rows and 14,384
   legacy/stale versions. Execution remains a separate operator decision.
4. During a deliberate deployment window, restart onto the new code and verify
   service health, claim tokens, outbox publishing, and screen independence.
5. Run a representative finite scope and compare the stage-specific baseline in
   `docs/PIPELINE_PERFORMANCE.md`.

Known residuals are deliberately bounded: provider Batch latency is external;
source single-flight is process-local; stable URLs are revalidated when acquired
after the TTL rather than by a background sweep; downstream extraction still
materializes whole artifact bytes; notification delivery is at-least-once; and
municipal/OCR/vendor tails remain classified failures. The status time series is a
24-hour operational window, and migration 035's legacy `submitted_at` values are
creation-time estimates; new provider jobs use the exact activation clock. None of
these weakens queue ownership, domain version fencing, or atomic publication.

Final non-production verification completed with 426 passing tests, 16 skips,
14 passing subtests, all 11 disposable-PostgreSQL concurrency tests enabled,
Pyright at zero errors, Ruff clean, shell syntax clean, and `git diff --check`
clean. A historical test seam that signalled an `asyncio.Event` from an executor
thread had left idle pytest processes during the audit; the current deterministic
offload seam is threadless, the exact leaked audit processes were terminated, and
post-suite process/thread inventories are clean.
