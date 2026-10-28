# database

This package holds the PostgreSQL 17 schema (with PostGIS), the migration runner, deterministic id generation, and the async repositories every other package uses for data access. There is no ORM: repositories issue SQL through one asyncpg pool.

## Schema: where the truth is

- **`migrations/`** is authoritative. Production is whatever the applied migrations made it. See [migrations/README.md](migrations/README.md).
- **`schema_postgres.sql`** and **`schema_userland.sql`** are hand-kept references with comments on intent. They lag the migrations: `jurisdiction_kinds`, `jurisdiction_pulse`, `numbering_runs` and `platform_metrics_snapshot` exist only in migrations. When the two disagree, trust the migration, or `\d table` on production.

## Tables by domain

| Domain | Tables |
|---|---|
| Jurisdictions | `jurisdictions` (banana, vendor, slug, `kind`; `type` is copied from the kind by trigger), `jurisdiction_kinds`, `zipcodes`, `census_government_units`, `census_unit_scans`, `jurisdiction_pulse` |
| Meetings and items | `meetings`, `items` (`attachments` with optional page spans, `body_text`, `body_source`, `summary`, `filter_reason`), `meeting_revisions`, `item_revisions`, `meeting_topics`, `item_topics` |
| Matters | `city_matters` (`canonical_summary`), `matter_appearances`, `matter_topics`, `numbering_runs` |
| People and bodies | `council_members`, `council_member_profiles`, `sponsorships`, `committees`, `committee_members`, `bodies`, `member_bodies` |
| Votes and minutes | `votes` (grain `(item_id, motion_index)`, `source` api or minutes, with a receipt), `item_motions`, `motion_actors`, `matter_actors`; minutes runs in `minutes_documents`, `minutes_text_snapshots`, `minutes_parse_runs`, `minutes_observations`, `minutes_publications` |
| Corpus | `document_blob`, `document_source`, `document_ingest_failure`, `corpus_extract_versions` (see [corpus/README.md](../corpus/README.md)) |
| Non-meeting records | `dockets`, `docket_filings` |
| Pipeline | `queue`, `pipeline_outbox`, `pipeline_runs`, `job_attempts`, `pipeline_stage_events`, `batch_jobs`, `item_filter_audits`, `meeting_ingest_audits`, `cache` |
| Product | `userland.*` (users, alerts, magic links, watches, ratings), `deliberations` and their participants, comments, votes and results, `tenants`, `tracked_items`, `session_events`, `happening_items` (deprecated), `platform_metrics_snapshot` |

## Rules

- **Ids are minted in one place.** `id_generation.py` is the only code that makes meeting, item, matter, member and committee ids. Adapters return vendor ids. The matter id hashes, in order of preference, `matter_file`, the vendor matter id, or a normalized title, plus a numbering period when the city's numbers restart.
- **The banana is the jurisdiction key** and is frozen once assigned (`scripts/_jurisdiction_naming.py`). Writers set `kind`, never `type`. A new kind arrives by migration, and `tests/test_jurisdiction_kinds.py` fails on drift.
- **Frozen snapshots.** The items upsert keeps title, attachments, hashes, `body_text` and `body_source` once `summary` is set. `matter_file` is not coalesced, so a value not re-derived on each sync is erased.
- **Related writes share a transaction.** Repository methods take `conn=`, and the sync orchestrator runs a meeting's writes, matter tracking and outbox events in one transaction.
- **JSONB goes in as Python objects.** The pool registers a codec, so `json.dumps` on a JSONB parameter double-encodes it into a JSON string. See `ASYNCPG_JSONB_HANDLING.md`.
- **Vote counters have one owner:** `recompute_attribution_counts`. Tallies use `vote_utils.py`.
- **Large aggregates never run on a request path.** They go into a snapshot table refreshed hourly.

## Code

- `db_postgres.Database.create()` opens the pool, checks the schema is current (`migrate.assert_schema_current`: a process refuses to start against pending migrations), initializes the corpus and publishes the extract policy. It exposes the repositories as attributes: `db.meetings`, `db.items`, `db.matters`, `db.queue`, `db.pipeline_lifecycle`, `db.document_blobs`, `db.pulse` and so on.
- `repositories_async/` has one module per domain. `helpers.py` builds model objects from rows. `minutes.py` holds the replayable minutes observations, which scripts use directly.
- `models.py` holds the Pydantic models passed between layers (`Meeting`, `AgendaItem`, `AttachmentInfo`, `Matter`, ...).
- Pool size comes from `ENGAGIC_POSTGRES_POOL_MIN_SIZE` and `_MAX_SIZE` (default 2 to 10). Background jobs use 3 or 4, because Postgres allows 100 connections in total.
