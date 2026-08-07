# Pipeline Remediation

Status: active implementation goal, started 2026-08-07.

This document is the integration record for the sync and processing pipeline
remediation. It supersedes point-in-time recommendations where the running code
has moved on. A work item is complete only when its invariant is represented in
code, focused tests, and end-to-end verification.

## Safety boundary

- Do not interrupt or send input to the live `engagic-process-processed` screen
  session.
- Code and additive migrations may be prepared while it runs; do not deploy,
  restart services, apply migrations, or mutate production data as part of this
  implementation pass.
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

### Sync persistence

1. Target resolution, schedule inputs, existing matters, and appearances are
   loaded set-wise.
2. A meeting sync uses one database unit of work; repository methods participate
   in the caller's connection instead of acquiring nested pool connections.
3. Meeting/item persistence and downstream work publication are atomic through
   a transactional outbox.
4. Adapter output is validated once at the canonical boundary using the shared
   typed schema. Vendor quirks remain inside adapters.

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

The final pass will replace this paragraph with completed changes, tests, schema
versions, reconciliation commands, and remaining operational rollout steps.
