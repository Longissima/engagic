# pipeline

The pipeline moves data from vendor adapters into Postgres and the corpus, and drains the processing queue that turns stored items into summaries. It decides; adapters only adapt.

## Entry points

| Command | Code | Runs as |
|---|---|---|
| `engagic-conductor sync <targets>` | `conductor.py` -> `fetcher.Fetcher.sync_cities` | `run.sh sync`, cron sweeps |
| `engagic-conductor process <targets>` | `conductor.py` -> `processor.Processor.run_pipeline_runtime` | `run.sh process` in a screen session; several can run at once |
| `engagic-conductor pulse` | `pulse.PulseWatcher.run_forever` | the `engagic-pulse` service |
| `engagic-conductor daemon` / `fetcher` / `processor` | always-on loops | units exist but are disabled; sync is change-driven and processing is run deliberately |

Targets are bananas, state codes, county bananas (which expand to their cities), comma lists or `@file`. The names `run.sh` resolves (`knowns`, `all`, `unpulsed`, `schools`) are documented in `scripts/deploy.sh` `resolve_targets`.

## Sync

```
Fetcher._sync_city
  -> vendors.factory adapter .fetch_meetings(window)   FetchResult: success vs "0 meetings"
  -> MeetingSyncOrchestrator.sync_meeting(meeting)      per meeting, one transaction
       meeting row, committee link, participation
       _process_agenda_items                            the item funnel (below)
       _track_matters                                   city_matters + matter_appearances
       _publish_authoritative_work                      queue work via pipeline_outbox
  -> MeetingSyncOrchestrator.archive_sources(...)       stage 1: originals -> corpus
```

**Pulse** (`pulse.py`) replaces syncing on a clock. For each jurisdiction it probes the cheapest signal that moves when something is published: a Legistar delta query, an RSS or agenda feed, or a digest of a small JSON or HTML listing. Only jurisdictions whose signal moved get a sync, narrowed to the hinted dates when they are close together. Probe intervals widen while a jurisdiction stays quiet and back off on failure. A signal is verified per jurisdiction, so a broken feed marks that one row unusable and says nothing about its vendor. BoardBook is excluded because its search index flaps. The daily `sync unpulsed` sweep covers anything pulse is not watching or has not probed cleanly in a day.

**The item funnel** (`MeetingSyncOrchestrator._process_agenda_items`) is the single place items enter the database, whether they come from sync, from claim-time chunking (`attach_items`) or from archive imports. In one pass it:
- drops junk titles (rows of financial tables, bookmark labels) with `quality.classify_title`;
- plans stable item ids (vendor item id, else sequence) and keeps legacy ids that match unambiguously;
- turns chunker `metadata.documents` into page-span attachments (`pages: [first, last]` on the packet URL) and trims `body_text` to the agenda text when every document became a span;
- extracts the durable identifier (`parsing.identifiers`) from title, body and document text, which drives `matter_file` and so the matter id;
- sets `body_source`. When an adapter registered the HTML page an item was scraped from (`BaseAdapter._source_page`), that page's bytes are archived, but only when the item's text is new or changed;
- stores through `items.store_agenda_items`, whose upsert freezes title, attachments, body and sources once an item has a summary.

Item metadata from the chunker is never persisted, so anything the funnel does not carry over is lost.

**Archival** (`archive_sources`) downloads every original a synced meeting names (agenda, packets, minutes, item attachments, agenda sources) into the corpus as bytes only. It is capped at `ENGAGIC_SYNC_ARCHIVE_BUDGET` new downloads per city per sync, newest meetings first. Downloads that failed recently are skipped using the shared `document_ingest_failure` ledger. Text extraction stays with the processor and the corpus-window timer. Afterwards the fetcher refreshes `item_documents` for the meetings it just stored, so what was archived resolves to its items.

**Enqueue** (`orchestrators/enqueue_decider.py`): a meeting is queued unless every item already has a summary or a filter reason that still applies, or it already has a monolithic summary. A matter is queued when its attachments, work version or title changed since it was last processed, or when it still lacks a canonical summary and has no terminal disposition or exhausted attempts. Administrative matter types are skipped (`filters.should_skip_matter`). Work is written to `pipeline_outbox` in the same transaction as the data it describes, then dispatched to the queue, so a crash cannot leave stored data without its queued work.

## Processing

`Processor.run_pipeline_runtime` claims typed jobs (`models.py`: `MeetingJob`, `MatterJob`) with `FOR UPDATE SKIP LOCKED`. `job_runner.py` owns the execution policy and `outcomes.py` the typed results. Each claim carries a work version and a token; every write checks both, so a re-sync that changes the meeting mid-claim supersedes the stale job instead of overwriting newer data.

`process_meeting` chooses a path in this order:

1. **Bare agenda**: when the document profile shows no business content, titles are stored with a filter reason and no LLM call.
2. **Items with content**: when items have attachments or body text, `_process_meeting_with_items` runs.
   1. Already-summarized and filtered items are dropped.
   2. Each distinct document is acquired once, corpus first (`document_acquisition.DocumentSourceAcquirer`: single-flight, conditional revalidation, archival on download).
   3. Text comes from the corpus or is extracted in a guarded child process.
   4. Spans are cut by `--- PAGE n ---` markers (`document_artifacts.span_of`).
   5. Documents shared by several items become meeting-level context and the rest go to their item. The cache key is `attachment_key` (URL plus pages), never the URL alone.
   6. Summaries are written incrementally as model results arrive.
3. **No items**: `_manufacture_items` chunks the agenda PDF first and then the packet, using corpus bytes, through `ground_truth.produce_ground_truth`. Results enter through the same `attach_items` funnel, so items born here look identical to items born at sync.
4. **Monolithic packet summary**: used when no items can be made, or when the chunk audit marks item slices unsafe.

`process_matter` aggregates a matter's attachments by `(identity, pages)` and summarizes a representative item. It stores `canonical_summary` with a compare-and-swap and fills only appearance snapshots that are still null.

**Lanes.** On the default Z.AI backend every job is streaming. On `ENGAGIC_LLM_BACKEND=gemini`, meetings outside the urgent window (default: the next day) go to the Gemini Batch lane at half the token cost. Batch submissions are durable intents, and collection is leased.

**Item filters** (`filters/item_filters.py`): everything is stored and filtering happens at processing time. Procedural, ceremonial and administrative titles are saved with a `filter_reason` and are not summarized. Test and demo meetings are the only data dropped at ingestion. Filter decisions carry a rule id and version (`ITEM_FILTER_VERSION`), so `scripts/recompute_item_filters.py` can audit and requeue after a rule change.

## Other modules

| File | Role |
|---|---|
| `ground_truth.py` | The one producer: archive the original, chunk it in a guarded subprocess, persist complete text, and stamp source URLs and page spans on chunked items. |
| `document_acquisition.py` | Corpus-aware download: freshness, revalidation, the failure ledger, size caps, failing open. |
| `document_artifacts.py` | Typed facts learned from bytes (format, media type, hash), span slicing and attachment keys. |
| `url_refresh.py` | Re-resolves expiring signed URLs (CivicClerk) from stored vendor ids just before fetching. |
| `utils.py` | Attachment hashing, matter attachment aggregation, `meeting_work_version`, identity helpers. |
| `reconciliation.py` | Planning for historical matter queue repair (`scripts/reconcile_matter_queue.py`). |
| `orchestrators/vote_processor.py` | Vote tallies without inferring a legal outcome. |
| `protocols/metrics.py` | The metrics interface injected into the fetcher and processor. |
| `admin.py`, `click_types.py` | Debug commands (`extract-text`, `preview-items`) and CLI argument types. |
