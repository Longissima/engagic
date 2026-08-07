# `pipeline/orchestrators/`

Business workflow coordination above the repository layer. Repositories own SQL;
orchestrators own cross-repository decisions and transaction boundaries.

## Classes

| Class | Purpose | Used by |
|---|---|---|
| `MeetingSyncOrchestrator` | Transform and persist one fetched meeting, its items/matters/appearances, and downstream intents in one unit of work | `Fetcher` |
| `EnqueueDecider` | Decide whether current meeting inputs require processing and calculate urgency | `MeetingSyncOrchestrator` |
| `MatterEnqueueDecider` | Compare authoritative matter artifact/work versions and bound repeated attempts | `MeetingSyncOrchestrator` |
| `MatterFilter` | Exclude procedural matter types from LLM work while preserving relational records | `MeetingSyncOrchestrator` |
| `VoteProcessor` | Normalize vote tallies and outcomes | `MeetingSyncOrchestrator` |

## Transaction pattern

`MeetingSyncOrchestrator.sync_meeting()` is the canonical sync persistence
boundary. It loads matter/appearance state set-wise, locks aggregates in stable
order, writes meeting domain rows, and records queue and city-activation outbox
intents on the caller's connection. A successful commit therefore contains both
the authoritative domain state and its durable publication intent.

Queue intent identity combines the stable source and `work_version`. Publication
for one source is advisory-lock serialized and carries a monotonic generation. If
authoritative inputs recur A -> B -> A, the existing A event is reopened with a
fresh generation; an older delivery cannot replace the later desired state.

Workers do not trust relationship snapshots in queue payloads. Meeting jobs carry
meeting identity; matter jobs carry matter identity. The processor reloads current
rows and checks the claimed `work_version` before projection writes.
