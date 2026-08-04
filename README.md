# Engagic

AI-powered civic engagement platform that discovers, processes, and summarizes local government meeting agendas from 500+ cities.

Live at **[engagic.org](https://engagic.org)**

---

## What It Does

Engagic fetches meeting agendas from civic tech platforms (Legistar, PrimeGov, Granicus, CivicPlus, Municode, and 14 more) used by cities, counties, and other public jurisdictions, extracts structured data, and uses LLMs to generate summaries that civilians can understand.

**Key capabilities:**
- **Item-level processing:** 97% of cities get structured agenda items (not just PDF blobs)
- **Matters-first architecture:** Legislative items tracked across meetings with deduplication
- **Council member profiles:** Elected officials tracked with normalized names, sponsorship history
- **Committee tracking:** Legislative bodies with rosters and member assignments
- **Voting records:** Individual votes per member per matter, tallies, outcomes across meetings
- **Topic extraction:** 16 canonical civic topics for filtering and alerts
- **Participation info:** Email, phone, Zoom links for civic action
- **Deliberation:** Opinion clustering for structured public input, trust-based moderation
- **Civic alerts:** Weekly digests with keyword matching, magic link auth
- **Happening this week:** AI-curated important upcoming items per city

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              PRESENTATION LAYER                             │
│  ┌─────────────────────┐                    ┌────────────────────────────┐  │
│  │  Frontend           │  HTTP/JSON         │  Server (FastAPI)          │  │
│  │  (SvelteKit)        │ ◄───────────────── │  17 route modules          │  │
│  │  Cloudflare Pages   │                    │  Tiered rate limiting      │  │
│  └─────────────────────┘                    └─────────────┬──────────────┘  │
└───────────────────────────────────────────────────────────┼─────────────────┘
                                                            │ Cache-only reads
┌───────────────────────────────────────────────────────────┼─────────────────┐
│                              DATA LAYER                   ▼                 │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  Database (PostgreSQL)                                                │  │
│  │  ┌─────────────┬─────────────┬─────────────┬─────────────────────────┐│  │
│  │  │ Core        │ Legislative │ Engagement  │ Userland                ││  │
│  │  │-jurisdictions│ - matters │ - watches   │ - users                 ││  │
│  │  │ - meetings  │ - votes     │ - activity  │ - alerts                ││  │
│  │  │ - items     │ - members   │ - ratings   │ - alert_matches         ││  │
│  │  │ - queue     │ - committees│ - issues    │ - magic_links           ││  │
│  │  └─────────────┴─────────────┴─────────────┴─────────────────────────┘│  │
│  │  14 async repositories, connection pooling (5-20 conns)               │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────▲─────────────────┘
                                                            │ Writes
┌───────────────────────────────────────────────────────────┼─────────────────┐
│                           PROCESSING LAYER                │                 │
│  ┌────────────────────────────────────────────────────────┴───────────────┐ │
│  │  Pipeline (Conductor)                                                  │ │
│  │  ┌─────────────────────────┐    ┌────────────────────────────────────┐ │ │
│  │  │  Sync Loop (24h)        │    │  Processing Loop (continuous)      │ │ │
│  │  │  ┌─────────────────┐    │    │  ┌──────────────────────────────┐  │ │ │
│  │  │  │ Fetcher         │    │    │  │ Processor                    │  │ │ │
│  │  │  │ - rate limiting │    │    │  │ - item-level path (97%)      │  │ │ │
│  │  │  │ - vendor routing│    │    │  │ - monolithic fallback (3%)  │  │ │ │
│  │  │  │ - matter track  │    │    │  │ - matters-first dedup        │  │ │ │
│  │  │  └────────┬────────┘    │    │  └──────────────┬───────────────┘  │ │ │
│  │  └───────────┼─────────────┘    └─────────────────┼──────────────────┘ │ │
│  │              │                                    │                    │ │
│  │              ▼                                    ▼                    │ │
│  │  ┌───────────────────────┐          ┌─────────────────────────────┐   │ │
│  │  │  Vendors (19)         │          │  Analysis (LLM)             │   │ │
│  │  │  - Legistar (110)     │          │  - Gemini 2.5 Flash/Lite    │   │ │
│  │  │  - Granicus (467)     │          │  - Adaptive prompting (v3)  │   │ │
│  │  │  - PrimeGov (64)      │          │  - 16 topic taxonomy        │   │ │
│  │  │  - IQM2 (45)          │          │  - Batch processing (50%)   │   │ │
│  │  │  - 15 more adapters   │          │  - Context caching          │   │ │
│  │  └───────────┬───────────┘          └──────────────┬──────────────┘   │ │
│  └──────────────┼──────────────────────────────────────┼─────────────────┘ │
│                 │                                      │                   │
│                 ▼                                      ▼                   │
│  ┌────────────────────────┐            ┌────────────────────────────────┐  │
│  │  External APIs         │            │  Parsing (PDF)                 │  │
│  │  Civic tech platforms  │            │  - PyMuPDF extraction          │  │
│  │  (rate limited)        │            │  - OCR fallback (Tesseract)    │  │
│  └────────────────────────┘            │  - Participation info          │  │
│                                        │  - Legislative formatting      │  │
│                                        └────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                           USER ENGAGEMENT LAYER                             │
│  ┌─────────────────────────────────┐    ┌─────────────────────────────────┐ │
│  │  Userland (Civic Alerts)        │    │  Deliberation (Opinion)         │ │
│  │  - Magic link auth              │    │  - PCA dimensionality reduction │ │
│  │  - Weekly digest emails         │    │  - K-means clustering           │ │
│  │  - Keyword matching             │    │  - Consensus detection          │ │
│  │  - Matter-based alerts          │    │  - Group vote analysis          │ │
│  │  - CAN-SPAM compliance          │    │  - Trust-based moderation       │ │
│  └─────────────────────────────────┘    └─────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

**~41,000 lines Python backend** organized into 8 focused modules:

| Module | Lines | Purpose |
|--------|-------|---------|
| [vendors/](vendors/README.md) | ~15,300 | 22 async adapters for Legistar, Granicus, PrimeGov, IQM2, NovusAgenda, CivicClerk, CivicPlus, CivicEngage, CivicWeb, eScribe, Municode, OnBase, ProudCity, Vision Internet, WP Events, AgendaOnline, BoardBook, Destiny, Berkeley, Chicago, Menlo Park, Ross. HTML parsers, PDF chunker (1749 lines), rate limiting, vendor-agnostic ID contract. |
| [database/](database/README.md) | ~8,300 | PostgreSQL with 14 async repositories (jurisdictions, meetings, items, matters, queue, search, userland, council_members, committees, engagement, feedback, deliberation, happening, helpers). asyncpg connection pooling, UPSERT preservation, normalized topics. |
| [pipeline/](pipeline/README.md) | ~3,900 | Conductor orchestration with dual loops: Fetcher (24h sync) and Processor (continuous queue). Orchestrators for business logic (MeetingSyncOrchestrator, EnqueueDecider, MatterFilter, VoteProcessor). |
| [analysis/](analysis/README.md) | ~2,300 | Gemini API integration with reactive rate limiting, unified adaptive prompting, 16-topic taxonomy, batch processing (50% cost savings), context caching. |
| [server/](server/README.md) | ~8,100 | FastAPI with 17 route modules (search, meetings, topics, matters, votes, committees, auth, dashboard, engagement, feedback, deliberation, flyer, donate, admin, monitoring, events, happening). Tiered rate limiting, JWT sessions. |
| [userland/](userland/README.md) | ~2,500 | Civic alerts: magic link auth, weekly digests (Sundays 9am), dual-track matching (keyword + matter-based), Mailgun delivery. |
| [parsing/](parsing/README.md) | ~1,200 | PDF extraction: PyMuPDF primary, OCR fallback (Tesseract), legislative formatting detection ([DELETED]/[ADDED]), participation info parsing (emails, phones, Zoom links). |
| [deliberation/](deliberation/README.md) | ~300 | Opinion clustering: PCA to 2D, dynamic K-means, Laplace-smoothed consensus detection, group vote tallies. |

---

## Key Patterns

**Matters-First:** Legislative items (bills, ordinances) tracked across meetings. Process once, reuse summary. Attachment hash detects changes.

**Item-Level Processing:** HTML agendas parsed into structured items. Each item gets focused summary. Topics aggregated to meeting level.

**Cache-First API:** Server never fetches live. Background daemon syncs cities every 24 hours, processes queue continuously.

**Async PostgreSQL:** Connection pooling (asyncpg, 5-20 connections), `FOR UPDATE SKIP LOCKED` for queue processing, UPSERT for idempotent updates.

**Legislative Accountability:** Council members tracked across votes and sponsorships. Committees tracked with rosters. Vote outcomes computed per matter per meeting.

**Repository Pattern:** 14 async repositories with shared connection pool. Each repository handles one domain (jurisdictions, meetings, items, matters, queue, search, userland, council_members, committees, engagement, feedback, deliberation, happening, helpers).

**AsyncBaseAdapter:** All 22 vendor adapters implement unified interface. Async HTTP with retry, rate limiting, date parsing. Adapters return `vendor_id`, database generates canonical IDs.

**Orchestrator Delegation:** Pipeline delegates business logic to orchestrators (MeetingSyncOrchestrator, EnqueueDecider, MatterFilter, VoteProcessor). Enables testing and separation of concerns.

---

## Processing Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  SYNC LOOP (every 24h)                                                      │
│                                                                             │
│    Conductor ──► Fetcher ──► Vendors (22 adapters)                          │
│                     │              │                                        │
│                     │              ▼                                        │
│                     │        External APIs (Legistar, Granicus, etc.)       │
│                     │              │                                        │
│                     │              ▼                                        │
│                     └──────► Database                                       │
│                              - Store meetings + items                       │
│                              - Track matters (city_matters)                 │
│                              - Extract votes + sponsors                     │
│                              - Enqueue for processing                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼ Queue (priority-sorted, typed jobs)
┌─────────────────────────────────────────────────────────────────────────────┐
│  PROCESSING LOOP (continuous)                                               │
│                                                                             │
│    Conductor ──► Processor ──► Dequeue job (FOR UPDATE SKIP LOCKED)         │
│                                     │                                       │
│           ┌─────────────────────────┴─────────────────────────┐             │
│           ▼                                                   ▼             │
│    ┌──────────────────────────┐               ┌──────────────────────────┐  │
│    │  ITEM-LEVEL PATH (97%)   │               │  MONOLITHIC PATH (3%)   │  │
│    │  Meeting has agenda_url  │               │  Meeting has packet_url  │  │
│    └────────────┬─────────────┘               └────────────┬─────────────┘  │
│                 │                                          │                │
│                 ▼                                          ▼                │
│    ┌──────────────────────────┐               ┌──────────────────────────┐  │
│    │  For each agenda item:   │               │  Single PDF packet:      │  │
│    │  - Filter procedural     │               │  - Extract via Parsing   │  │
│    │  - Extract PDFs (Parsing)│               │  - LLM: comprehensive    │  │
│    │  - Document cache (dedup)│               │    summary (5-10 sent)   │  │
│    │  - LLM: per-item summary │               └────────────┬─────────────┘  │
│    │    (1-5 sentences each)  │                            │                │
│    │  - Topic normalization   │                            │                │
│    │  - Aggregate to meeting  │                            │                │
│    └────────────┬─────────────┘                            │                │
│                 │                                          │                │
│                 └──────────────────┬───────────────────────┘                │
│                                    ▼                                        │
│                              Database                                       │
│                              - Update summaries                             │
│                              - Store topics (normalized)                    │
│                              - Mark job complete                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Two Processing Paths:**
- **Item-level (97%):** HTML agendas parsed into structured items. Each item gets focused 1-5 sentence summary. Topics aggregated to meeting. Document cache prevents re-extracting shared PDFs.
- **Monolithic (3%):** PDF-only meetings. Single comprehensive 5-10 sentence summary. Falls back when vendor doesn't expose structured agenda.

---

## Quick Start

### Prerequisites
- Python 3.13+
- PostgreSQL 14+
- Node.js 18+
- Google Gemini API key

### Backend

```bash
# Install dependencies
uv sync

# Set environment variables
export GEMINI_API_KEY="your-key"
export POSTGRES_HOST="localhost"
export POSTGRES_DB="engagic"
export POSTGRES_USER="engagic"
export POSTGRES_PASSWORD="***"

# Run API server
uvicorn server.main:app --host 0.0.0.0 --port 8000

# Run background daemon (separate terminal)
python -m pipeline.conductor daemon
```

### Frontend

```bash
cd frontend
npm install
npm run dev  # localhost:5173
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/VISION.md](docs/VISION.md) | Mission, roadmap, architectural philosophy |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production deployment guide |
| [docs/API.md](docs/API.md) | Complete API endpoint reference |
| [docs/SCHEMA.md](docs/SCHEMA.md) | Database schema quick reference |
| [docs/MATTERS_ARCHITECTURE.md](docs/MATTERS_ARCHITECTURE.md) | Legislative matter tracking design |
| [docs/ARCHITECTURE_REVIEW.md](docs/ARCHITECTURE_REVIEW.md) | Code health audit with end-to-end flow tracing (Apr 2026) |
| [docs/TERMS_OF_SERVICE.md](docs/TERMS_OF_SERVICE.md) | API rate tiers and usage policies |
| [docs/DELIBERATION_LOCAL_FIRST.md](docs/DELIBERATION_LOCAL_FIRST.md) | PWA local-first deliberation plan |
| [CHANGELOG.md](CHANGELOG.md) | Version history and recent changes |

---

## Stack

Python 3.13, FastAPI, PostgreSQL, asyncpg, BeautifulSoup, PyMuPDF, Google Gemini, SvelteKit, Cloudflare Pages

---

## License

Open source. See LICENSE file.
