# Parsers and the chunker cascade

This folder turns documents into agenda items. HTML parsers handle vendor agenda pages (`legistar_parser.py`, `granicus_parser.py`, `primegov_parser.py`, `municode_parser.py`, `novusagenda_parser.py`, `civicplus_parser.py`). The rest is the PDF chunker, which works on the PDF itself and is therefore vendor-agnostic.

## Routing (`router.py`)

Routing policy is data. A rung is one engine invocation, `engine:method`. A ladder is an ordered list of rungs, tried until one yields items. `LADDERS` holds the current set:

| Ladder | Used for |
|---|---|
| `agenda` | short hyperlinked agenda PDFs; URL anchoring first |
| `packet` | compiled packets; outline (TOC) first |
| `auto` | unforced callers |
| `url_legacy`, `v2_url_only` | old `force_method` callers (Granicus) |
| `archive_url` | archival: both URL parsers run and their linked documents are merged, because each finds documents the other misses |

`chunk_pdf(path, ladder, hint)` returns a `ChunkResult` with the winning items plus every attempt: item counts, durations and a classified failure reason (`open_failed`, `encrypted`, `no_text_layer`, `no_items`, `engine_error`, ...). A crash in one engine is recorded and the cascade continues.

Hints reorder rungs; they never remove any. The first hint source is the city's last winning rung (sticky, seeded at fetcher startup from stored audits). The second, for cities with no history, is the morphology classifier's suggestion. A wrong hint costs one extra attempt.

Callers do not run engines directly. Adapters go through `AsyncBaseAdapter` (`_chunk_agenda_then_packet`, `_chunk_packet_pdf`), and the processor goes through `pipeline.ground_truth.produce_ground_truth`. Both run the chunker in a guarded child process (`parsing/subprocess_guard.py`) so a pathological PDF cannot take down the caller.

## Engines

- **v1** (`agenda_chunker.py`) works from root to leaf. It finds item headers by regex, then looks for content and links below them. It has a URL-anchored path and several fixed outline shapes, and is kept for the layouts where it still wins.
- **v2** (`agenda_chunker_v2.py`) works from leaf to root. It finds invariant anchors first (hyperlinks, outline page boundaries, internal page jumps), then walks back to the structure. It reuses v1's primitives.
- **text** (`text_chunker.py`) splits on numbered headings for short flat-text agendas. It is the last rung everywhere and limits itself so it only converts would-be `no_items` failures.

## Measurement

- `pdf_profile.py` measures signals in one bounded pass: outline shape, external versus internal links, numbered heading lines, text layer.
- `morphology.py` maps a profile to a named shape (`linked_agenda`, `toc_packet`, `scanned`, ...) and holds every detection threshold in one table. The classification and whether it agreed with the winning rung go into the audit.
- `quality.py` lints titles. Garbage titles (bookmark labels, filenames) are repaired only from trustworthy sources. `seg_smell` flags item counts that diverge from the document's own numbering. `classify_title` is also the junk filter in the sync item funnel.
- `pdf_links.py` is the shared link-annotation reader for the engines, the profiler and the HTML parsers that meet embedded PDFs.

After a URL rung wins, the base adapter's `_resolve_sub_attachments` follows each item's cover sheet to the exhibits it links.

## Output contract

Each chunked item is a dict with `title`, `agenda_number`, `sequence`, `attachments` (linked documents) and a transient `metadata` carrying:
- `page_start` / `page_end`;
- `agenda_text`, the item's own text on the agenda;
- `documents`: memos or staff reports inside the packet, each `{name, page_start, page_end, text}`.

The producer stamps the source URL on these. The sync funnel turns each document into a page-span attachment and keeps only `agenda_text` as `body_text`. Item metadata is never stored, so a field the funnel does not consume is lost.

## Changing the chunker

`tests/chunker/` is the gate: real production PDFs pinned by sha256, goldens for the winning rung, item structure and failure reasons, plus ground-truth recall and precision ratchets. After an intentional change, run `update_goldens.py` and read the diff; the diff is the behavior change. New fixtures come from the `no_items` failure pool in production audits.

To try a single PDF: `uv run python -m vendors.adapters.parsers.router <pdf> [ladder]`. `agenda_chunker_template.py` at the repo root is the standalone prototype that new patterns are developed in before being ported here.
