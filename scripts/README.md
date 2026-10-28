# scripts

Operational jobs, backfills, audits and onboarding tools. Each script's module docstring is its documentation (purpose, flags, examples). This file is the index. Scripts run on the VPS, where the database and credentials live: `uv run python scripts/<name>.py` or `uv run python -m scripts.<name>`.

Rules for new scripts are in the cookbook skill: stream and resume, bounded fan-out, classified errors, one write path. Backfills route through `MeetingSyncOrchestrator` (`sync_meeting`, `attach_items`), never a staging table. History is never put on a timer; a backfill runs in reviewed batches.

## Entry point

`deploy.sh` is `run.sh` at the repo root (a symlink; never `sed -i` it). It wraps sync, process, deploy, logs, services, moderation and map commands, and resolves the target names `knowns`, `all`, `unpulsed` and `schools` by regenerating lists in `munis/` from the database.

## Index

**Jurisdictions and onboarding**
- `db_viewer.py`: interactive DB viewer; "Add jurisdiction" runs the shared flow in `_jurisdiction_onboarding.py` (kinds registry, `_jurisdiction_naming.py` bananas, `_jurisdiction_geography.py` enrichment)
- `probe_vendors.py`, `discover_vendor_slugs.py`: find a city's vendor and slug (guesses are never registrations)
- `onboard_selfhosted.py`, `boardbook_bulk_import.py`: create jurisdictions from the selfhosted registry or BoardBook
- `repair_civicplus_domains.py`, `audit_jurisdiction_collisions.py`: fix moved CivicPlus domains; find jurisdictions scraping another government's agendas
- `import_census_boundaries.py`, `import_zcta_boundaries.py`, `import_city_populations.py`, `generate_tiles.py`: geometry, ZIPs, population, map tiles (to R2 `engagic-tiles`)

**Corpus and documents**
- `backfill_corpus_attachments.py`: archive and extract item attachments; its rolling form is the `engagic-corpus-window` timer
- `extract_archived_text.py`, `ingest_corpus_attachment.py`: text for archived originals; one-off single attachment
- `archive_meetings.py`, `backfill_meetings_from_manifests.py`: historical originals and meetings through the normal sync path
- `ingest_publications.py`, `ingest_dockets.py`: non-meeting bodies' publications and commission dockets
- `ingest_manual_pdfs.py`: PDFs supplied by hand for bot-walled cities
- `backfill_civicclerk_portal_agendas.py`: re-acquire CivicClerk agendas archived as the portal shell

**Minutes and votes**
- `sweep_minutes.py` (weekly cron), `ingest_minutes.py` (daily), `parse_minutes_votes.py` (daily): minutes into the corpus, then published motions and votes
- `audit_minutes_identity.py`, `audit_minutes_votes.py`, `eval_rollcall.py`, `replay_rollcall_ab.py`: read-only audits, scoring against API votes, A/B against a git ref
- `backfill_vote_outcomes.py`, `sync_roster.py`, `normalize_roster.py`, `backfill_committees.py`: outcomes, Legistar rosters, person and body clustering, committees

**Matters and identity**
- `rekey_matters.py` (weekly cron): re-key history onto the identity contract and refresh `numbering_runs`
- `backfill_body_identifiers.py`, `backfill_matter_ids.py`, `backfill_matter_titles.py`, `relink_vendor_keyed_items.py`: fill or repair matter linkage on old items
- `migrate_meeting_ids.py`, `migrate_matter_ids.py`: one-time id format migrations
- `reconcile_matter_queue.py`: audit or repair historical matter queue descriptors

**Queue, summaries and quality**
- `recompute_item_filters.py`: apply changed item filters and requeue newly eligible meetings
- `resummarize_items.py`, `compare_llm_backends.py`, `estimate_tokens.py`: prompt-version refresh, model bake-offs, token estimates
- `audit_chunker.py`, `summary_quality_checker.py`: spot checks on item and summary quality
- `reset_queue.py`, `health_check.py`, `diagnostics.py`: queue reset, per-city health, orphan and integrity checks
- `moderate.py`: deliberation moderation

**Shell**
- `run_happening.sh`: fills the deprecated `happening_items`
- `setup_vps_deps.sh`, `tune_postgres.sh`: early box setup; the tuning targets a 2 GB box and predates the current one

`archive/` and `migrations/` hold retired tools (SQLite migration, a pre-runner SQL file); `spikes/` holds experiments.
