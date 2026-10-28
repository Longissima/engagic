# Engagic

Engagic collects what local governments publish (agendas, packets, minutes, votes, dockets, reports) from every jurisdiction it can reach, keeps the originals, and turns them into structured, summarized, searchable records. Live at **[engagic.org](https://engagic.org)**.

It covers five classes of jurisdiction (city, county, school district, special district, state) through vendor adapters for the platforms governments publish on. Live coverage numbers come from the hourly `platform_metrics_snapshot`, not from this file.

Engagic owns the corpus and ingestion layer of a larger system. Two sibling repos read its Postgres read-only and never write to it:
- **motioncount** owns extraction and analysis (budgets, votes, contracts).
- **spygov** owns presentation.

## How data flows

```
 pulse (change signals) ─┐
 cron sweeps ────────────┼─> SYNC   Fetcher -> vendor adapter -> MeetingSyncOrchestrator
 run.sh sync <target> ───┘            item funnel: junk filter, identifiers, matter ids,
                                      page spans, body_source; one write path
                                    archive_sources: every named original -> R2 (stage 1)
                                             │
                                             ▼
                        Postgres: meetings, items, matters, votes, queue + outbox
                                             │
 run.sh process <target> ──> PROCESS  claim job -> manufacture items if none (chunker)
                                      -> text from corpus (extract on miss) -> LLM summary
                                      -> topics, participation, canonical matter summary
                                             │
                                             ▼
                        API (FastAPI), MCP server, frontend (SvelteKit on Cloudflare),
                        weekly digest, motioncount and spygov readers

 minutes: sweep_minutes -> ingest_minutes (corpus) -> parse_minutes_votes -> votes (source=minutes)
 corpus window timer: backfill_corpus_attachments archives and texts the near-term attachments
```

Everything about R2 lives in two prefixes: `originals/<sha256>` and `text/<sha256>.txt`, indexed by `document_blob` and `document_source`. Storing to the corpus is a passenger: an R2 failure degrades to a warning and never fails a sync or a summary.

## Rules the code depends on

- **One write path.** Meetings and items enter only through `MeetingSyncOrchestrator`:
  - `sync_meeting` for meetings.
  - `_process_agenda_items` for items, which `attach_items` also uses.
  - Backfills, claim-time chunking and archive imports route through the same funnel.
- **One place mints ids.** `database/id_generation.py`. Adapters return vendor ids; the banana (`paloaltoCA`) is the jurisdiction key and is frozen once assigned.
- **Matter identity comes from the government's own text.** The order is `matter_file`, then a labeled identifier extracted from the text (`parsing/identifiers.py`), then the vendor id. Restarting numbering schemes get a period.
- **Summaries are frozen per appearance.** Once `items.summary` is set, a re-sync keeps the item's title, attachments, body and sources. `city_matters.canonical_summary` holds the latest version. An unchanged reappearance reuses the summary with no LLM call.
- **Items cite their sources.** Each source in `items.attachments` is either a whole document or a page span (`pages: [first, last]`) of a meeting PDF. `items.body_source` locates `body_text`.
- **Uniform rules beat lists.** Behavior is derived from the data (printed evidence, document shape) rather than per-city or per-vendor config.

## Repository map

| Path | Owns |
|---|---|
| [vendors/](vendors/README.md) | Adapters per publishing platform, the PDF chunker cascade, HTTP sessions and pacing |
| [pipeline/](pipeline/README.md) | Sync, pulse, the item funnel, the processing queue and its runtime |
| [corpus/](corpus/README.md) | R2 originals and text, content-addressed |
| [parsing/](parsing/README.md) | Document text extraction, OCR, identifiers, participation info, crash containment |
| [analysis/](analysis/README.md) | LLM backends, prompts, summarization, topic taxonomy |
| [database/](database/README.md) | Schema, migrations, async repositories, id generation |
| [server/](server/README.md) | Public HTTP API |
| [userland/](userland/README.md) | Accounts, magic links, weekly digests |
| [deliberation/](deliberation/README.md) | Opinion clustering for structured public input |
| [scripts/](scripts/README.md) | Ingestion jobs, backfills, audits, onboarding, the `run.sh` entry point |
| `mcp_server.py` | MCP server exposing the data to Claude clients |
| `frontend/` | SvelteKit site, deployed as a Cloudflare Worker |
| `munis/` | Target lists for `run.sh` (some regenerated from the DB on every use) |
| `tests/chunker/` | The one sanctioned test suite: real PDFs, golden snapshots |

## Running it

Production is one VPS; Postgres, R2 credentials, LLM keys and every service exist only there. Code is developed in a local checkout and deployed with `./run.sh deploy`.

| What | How |
|---|---|
| API, MCP, pulse | Long-running systemd services. They load code once, so restart after a deploy. |
| Sync | Pulse syncs jurisdictions whose signal moved. Cron sweeps cover the rest: `run.sh sync unpulsed` daily and `run.sh sync knowns` weekly. Manual runs use `./run.sh sync <banana \| state \| @file \| knowns \| all \| schools>`. |
| Process | `./run.sh process knowns` for the routine run; `process all` after onboarding so new jurisdictions earn a first summary. |
| Minutes | Cron: sweep weekly, ingest and vote parsing daily. |
| Migrations | `uv run python -m database.migrate`. A new migration blocks every new process until it is applied. |

Local development is limited to code, lint and the chunker suite:

```bash
uv sync
uv run pytest tests/chunker
cd frontend && npm install && npm run dev
```

## Documentation

| Document | About |
|---|---|
| [docs/VISION.md](docs/VISION.md) | Mission and direction |
| [docs/CORPUS_ARCHITECTURE.md](docs/CORPUS_ARCHITECTURE.md) | The corpus and the stage model it introduced |
| [docs/MATTERS_ARCHITECTURE.md](docs/MATTERS_ARCHITECTURE.md) | Legislative matter tracking |
| [docs/MOTION_DATA_CONTRACT.md](docs/MOTION_DATA_CONTRACT.md) | Votes and motions as published facts |
| [docs/JURISDICTION_ONBOARDING.md](docs/JURISDICTION_ONBOARDING.md) | Adding jurisdictions |
| [docs/API.md](docs/API.md) | HTTP endpoints |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production setup |
| [CHANGELOG.md](CHANGELOG.md) | What changed and why |

## Stack

Python 3.13 (uv), asyncpg on PostgreSQL 17 with PostGIS, FastAPI, aiohttp, PyMuPDF with Tesseract OCR, Cloudflare R2, GLM-5.3-flash on Z.AI behind a provider-neutral backend, SvelteKit on Cloudflare Workers.

## License

AGPL-3.0. See [LICENSE](LICENSE).
