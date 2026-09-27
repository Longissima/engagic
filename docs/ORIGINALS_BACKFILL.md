# Historical archive and meeting backfill

`scripts/archive_meetings.py` uses the existing adapters to discover historical
meetings, save selected original documents and a meeting manifest to the corpus,
and store meetings and native agenda items through the ordinary sync path.
Legistar, CivicClerk, PrimeGov, eScribe, Granicus and CivicPlus are supported.

Meeting storage is enabled by default. `--no-store-meetings` keeps only originals
and manifests. Summary enqueueing is disabled by default; `--enqueue-summaries`
enables the ordinary processing deciders when meetings are stored. Archival
itself does not run OCR or LLMs.

## Run an archive

Commands require the project's configured database and, except for `--plan`,
an enabled corpus.

```sh
# Inspect all active Legistar primary and extra-vendor sources.
.venv/bin/python scripts/archive_meetings.py --all --start 2023-09-17 --end 2026-09-17 --plan

# Archive those sources and store their meetings without enqueueing summaries.
.venv/bin/python scripts/archive_meetings.py --all --start 2023-09-17 --end 2026-09-17

# Limit a pilot to one monthly window for a selected jurisdiction.
.venv/bin/python scripts/archive_meetings.py --banana seattleWA --start 2023-12-01 --end 2024-01-01 --max-windows 1

# Select another vendor and a separate checkpoint.
.venv/bin/python scripts/archive_meetings.py --vendors civicclerk --all --start 2023-09-17 --end 2026-09-17 --checkpoint data/archive-civicclerk.sqlite3
```

Dates have an inclusive start and **exclusive end**, in the source's local
calendar. `--vendors` defaults to `legistar` and accepts comma-separated vendors.
`--banana` accepts comma-separated jurisdiction IDs; use `--all` to select all
active jurisdictions with matching primary or extra-vendor sources.

Use separate checkpoint files for concurrent processes; each checkpoint permits
one owner. Reuse the same checkpoint when resuming or extending an archive's
date range.

## What is saved

Discovery calls `fetch_meetings(start=..., end=..., originals_only=True)`.
Adapters retain native HTML/JSON items and selected published documents while
omitting vote/sponsor enrichment and most PDF processing. Granicus agenda-only
PDFs are the exception: the existing v1 URL parser discovers first-level
attachment links without a full-document extraction pass or recursive traversal.

Documents stream through temporary files into corpus storage. The default limit
is 2048 MiB per document, configurable with `--max-document-mib`. Acquisition
reuses indexed originals with an object key and a positive byte count; a hash
without an original object is insufficient.

Each meeting's JSON manifest has source identity
`engagic://meeting-archive/<vendor>/<slug>/<meeting_id>`. It retains the adapter's
meeting dictionary and document receipts, including hashes, object keys, sizes,
errors and unavailable-document statuses.

After saving the manifest, the runner normally calls `sync_meeting` to store
the meeting, native items and associated records. This uses the same identities
and storage rules as ordinary sync. Fields absent from the archival discovery
still require later enrichment. If storage raises, the receipt records
`store_error` and a `meeting_store_failed` event is emitted; the saved originals
and manifest remain available for replay.

## Resume, retries and completeness

Progress defaults to `data/archive-meetings.sqlite3`. Preserve the checkpoint
when moving work to another machine. Discovery, per-meeting receipts and
per-document outcomes survive interruption.

The runner handles new monthly windows first, then interrupted windows, then
eligible failed windows. Sources rotate between windows. Defaults are three
concurrent sources, two meetings per source and eight document transfers across
the run; adjust `--source-concurrency`, `--meeting-concurrency` and
`--document-concurrency` as needed.

- Completed windows and successful receipts are skipped. New date ranges
  subtract completed coverage; ordinary sync handles later revisions.
- Partial discovery retains successfully archived documents but leaves the
  window incomplete. Subsequent discovery can reuse those originals.
- Failed windows and document URLs have a six-hour retry cooldown and a
  three-attempt cap. A failed document is not retried again in the same process.
  Immediate restarts do not bypass the cooldown.
- HTTP 403, 404 and 410 document responses are recorded as `unavailable` and are
  not automatically retried. Other failures leave work pending. A completed
  window can therefore contain unavailable documents.
- `--refresh-discovery` re-fetches eligible unfinished windows and can discover
  changed source URLs. It does not reset completed coverage, retry limits or
  document outcomes for unchanged URLs.

CivicClerk refreshes eligible unarchived attachment URLs through the shared
URL-refresh layer. Already archived originals bypass that refresh.

Inspect `deferred_windows`, unavailable counts, discovery errors and meeting
storage errors before treating a run as complete. Empty discovery means only
that the configured source exposed no meetings in that interval. A successful
archive receipt does not prove that every document was available or that its
meeting was stored.

Inspect a checkpoint without changing it:

```sh
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect("file:data/archive-meetings.sqlite3?mode=ro", uri=True) as db:
    for row in db.execute("SELECT status, count(*) FROM windows GROUP BY status"):
        print(row)
    for row in db.execute(
        "SELECT source, start, end, attempts, next_retry_at, error "
        "FROM windows WHERE status='failed'"
    ):
        print(row)
PY
```

## Replay manifests into missing meeting rows

`scripts/backfill_meetings_from_manifests.py` recovers older archive-only runs,
`--no-store-meetings` runs and failed meeting inserts. It reads manifests from
the corpus and passes their meeting dictionaries through ordinary sync without
re-fetching vendor documents.

```sh
.venv/bin/python scripts/backfill_meetings_from_manifests.py --vendor legistar --dry-run
.venv/bin/python scripts/backfill_meetings_from_manifests.py --vendor legistar --concurrency 4
```

Omit `--vendor` to consider all archived vendors. `--banana` and `--limit` narrow
the candidates. Summary enqueueing remains off unless `--enqueue-summaries` is
supplied. Replay skips existing meeting IDs, so it does not repair missing items
or enrichment on rows already present.

## Vendor discovery and document selection

| Vendor | Historical behavior and limits |
| --- | --- |
| Legistar | Reuses API/HTML discovery, pagination and calendar fallbacks. Unconfirmed year selection or incomplete listings fail discovery. |
| CivicClerk | Retains published meeting files, reports and section attachments, including native and PDF URLs when exposed. An internal DOCX storage name may still download as PDF; it does not prove the native file was captured. |
| PrimeGov | Enumerates requested years and caches annual listings. Prefers HTML agendas and listed attachments, then packet/agenda fallbacks. Minutes are independent; alternative representations are not all downloaded. |
| eScribe | Queries disjoint seven-day intervals, splitting results with 100 or more meetings. A still-dense single day fails discovery. Prefers native HTML and attachments, with published packet/agenda fallbacks and independent minutes. |
| Granicus | Enumerates configured ViewPublisher views and follows explicit same-view page links. Unknown layouts or pagination fail discovery. It does not guess adjacent view IDs. HTML items and attachments are preferred; full packets bypass chunking, while agenda-only PDFs use v1 URL discovery. |
| CivicPlus | Enumerates exposed categories and requested years, caching annual listings. Keeps native HTML items, directly listed documents, supplements and minutes. Unambiguous same-date agenda/packet categories are paired; ambiguous sessions stay separate. No PDF link extraction runs. |

Unsupported portals, removed categories, vendor migrations, unpublished records
and documents only discoverable inside unprocessed PDFs can leave coverage gaps.
Raw listing evidence and manifests retain what discovery observed.

## Regression tests

`tests/test_original_archive_acquisition.py` covers document outcomes, transfer
limits, partial discovery, checkpoint recovery, source rotation and retry limits.

```sh
.venv/bin/python -m pytest -q tests/test_original_archive_acquisition.py
```
