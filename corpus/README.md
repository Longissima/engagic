# corpus

The corpus keeps every public document engagic has seen. It holds the original bytes and the extracted text, content-addressed in R2 (bucket `engagic-corpus`), and indexes them in Postgres. The design and its stage model are in [docs/CORPUS_ARCHITECTURE.md](../docs/CORPUS_ARCHITECTURE.md). The goal is that extraction writes once and every downstream consumer reads.

## Layout

| Where | What |
|---|---|
| R2 `originals/<sha256>` | source bytes, keyed by their own sha256 |
| R2 `text/<sha256>.txt` | extracted text, with `--- PAGE n ---` markers (1-indexed) that page spans and vote receipts index into |
| `document_blob` | one row per sha256: size, type, R2 keys, extract method and version, page and OCR counts, extraction status |
| `document_source` | where those bytes were seen: `source_identity` (the URL with only signed query parameters stripped, `pipeline.utils.attachment_identity`), banana, freshness and validation clocks, and the title and date a publications listing printed |
| `document_ingest_failure` | the shared failure ledger keyed by identity and extract version: bounded retries, then permanent |
| `corpus_extract_versions` | the published extract policy: which versions are readable and which is current |

Writes are idempotent by construction: two workers storing the same bytes converge on one object and one row.

## Rules

- **The corpus is a passenger.** Every public method of `CorpusStore` traps its own errors and degrades to "no corpus" with a warning. An R2 outage never fails a sync or a summary.
- **Identity is exact.** A URL's query string often carries the document's identity (LinkClick-style links), so only signature parameters are stripped, never the whole query.
- **Text is written once per extract version.** Partial OCR text is stored marked partial, with its pending pages, and a later complete pass replaces it. An empty or failed extraction is recorded on the blob, not stored as text.
- **Readers outside this repo read the policy, not a constant.** `publish_extract_policy` writes `EXTRACT_VERSION` and `COMPATIBLE_EXTRACT_VERSIONS` to `corpus_extract_versions` whenever a process opens the database. motioncount's `engagic_client` reads that table. Bump the version here to force re-extraction; that is the whole change.
- **R2 is spoken over the S3 data plane** (`r2.py`, hand-rolled SigV4). The REST management endpoint has a global request cap that would silently thin coverage.

## Who writes

| Writer | What |
|---|---|
| `MeetingSyncOrchestrator.archive_sources` | Originals for everything a synced meeting names, bytes only, budgeted per city per sync. |
| `pipeline.ground_truth.produce_ground_truth` | Chunked PDFs: original plus text, from adapters at sync time and from the processor at claim time. |
| `pipeline.document_acquisition.DocumentSourceAcquirer` | Every processor download is archived, and extracted text is persisted. |
| `scripts/backfill_corpus_attachments.py` | Archives and extracts attachments the processor has not reached. The `engagic-corpus-window` timer runs it over a rolling window (two weeks back, about six weeks ahead), so readers who want documents rather than summaries never wait on the LLM queue. |
| `scripts/ingest_minutes.py`, `ingest_publications.py`, `ingest_dockets.py`, `extract_archived_text.py`, `ingest_corpus_attachment.py` | The other ingestion routes. |

Readers include the processor (corpus first, download on a miss), motioncount, and spygov through motioncount.
