# Scripts - Operational Tools & Data Management

CLI tools for data ingestion, validation, migration, and operations. All scripts use async PostgreSQL via the shared `Database` class.

## Data Ingestion

### `ingest_manual_pdfs.py`
Ingest manually-provided PDFs for cities behind bot protection (Akamai, Cloudflare, etc.).

```bash
uv run scripts/ingest_manual_pdfs.py portolavalleyCA
uv run scripts/ingest_manual_pdfs.py portolavalleyCA --dir /path/to/pdfs
```

- Reads PDFs from `data/manual_pdfs/{banana}/`
- Filename format: `{MMDDYYYY}_{body}_{type}.pdf` (e.g., `03252026_Council_Agenda_Packet.pdf`)
- Falls back to extracting date/title from PDF content
- Three parsing strategies attempted in order: Item-TOC (Portola Valley style), standard `parse_agenda_pdf()`, direct text parse
- Stores the meeting/items and a versioned queue outbox intent in one transaction
  (priority 100). The script re-reads locked rows before hashing so publication
  describes the authoritative snapshot actually retained by freeze-on-summary.

### `probe_vendors.py`
Detect correct vendor and slug for unconfigured cities by probing vendor URL patterns.

```bash
uv run scripts/probe_vendors.py         # Default: top 20 by population
uv run scripts/probe_vendors.py 50      # Probe top 50
```

- Generates plausible slug candidates per vendor (10 vendors supported)
- Validates city identity in page content (handles same-name cities in different states)
- Checks for recent meeting dates (freshness)
- Outputs SQL UPDATE statements for confirmed hits

### `sync_roster.py`
Sync committee roster data from Legistar API.

```bash
uv run scripts/sync_roster.py --city newyorkNY
uv run scripts/sync_roster.py --all-legistar
uv run scripts/sync_roster.py --dry-run
```

## Data Validation

### `health_check.py`
Quick health check of all cities with summary statistics and issue flagging.

```bash
uv run scripts/health_check.py
```

Reports: overall stats, vendor breakdown, AI processing completion rate, cities with no meetings, vendor URL mismatches, recent sync activity.

### `diagnostics.py`
Identify orphaned records and data integrity issues.

```bash
uv run scripts/diagnostics.py           # Run all checks
uv run scripts/diagnostics.py --fix     # Run with optional cleanup
```

Checks: orphaned matters, orphaned happening_items, orphaned queue jobs, summary desync between items and matters.

### `summary_quality_checker.py`
Analyze and validate AI-generated meeting summaries.

```bash
uv run scripts/summary_quality_checker.py
```

Classifies summaries as GOOD, ERROR, TRUNCATED, TOO_SHORT, PROCESSING_FAILURE, EMPTY, or SUSPICIOUS.

## Data Migration

### `migrate_meeting_ids.py` / `migrate_matter_ids.py`
Migrate IDs from vendor-specific formats to unified `{banana}_{hash}` format. Cascades FK changes to all referencing tables.

```bash
uv run scripts/migrate_meeting_ids.py --dry-run
uv run scripts/migrate_meeting_ids.py
```

### `backfill_*.py`
Backfill scripts for data completeness:
- `backfill_body_identifiers.py` - Set matter_file from identifiers cited in item title/body (dry run by default). Sync derives these on every pass; this covers meetings aged out of the sync window. Run `backfill_matter_ids.py` afterwards
- `backfill_matter_ids.py` - Create city_matters for orphan items with matter_file but no matter_id
- `backfill_matter_titles.py` - Fill missing matter titles from items
- `backfill_vote_outcomes.py` - Compute vote outcomes from vote records
- `backfill_committees.py` - Populate committee data

## Geographic & Demographic Data

### `import_census_boundaries.py`
Import Census TIGER/Line Place and cartographic county boundaries into
`jurisdictions.geom`.

```bash
uv run scripts/import_census_boundaries.py --all       # Full pipeline
uv run scripts/import_census_boundaries.py --download   # Download shapefiles
uv run scripts/import_census_boundaries.py --import     # Import to staging
uv run scripts/import_census_boundaries.py --match      # Match to jurisdictions
uv run scripts/import_census_boundaries.py --counties   # Validated county refresh + match
```

Smart name matching: exact, hyphens, township suffixes, Saint/St variations, abbreviation expansion, fuzzy LIKE fallback.
County refreshes validate the national row count, required fields, geometry,
and SRID in a disposable table before atomically replacing the live staging
table.

### `import_city_populations.py`
Import population data from `cities.json` into jurisdictions table.

```bash
uv run scripts/import_city_populations.py --import
uv run scripts/import_city_populations.py --status
```

## Map Generation

### `generate_tiles.py`
Generate PMTiles from jurisdiction geometries for map visualization.

```bash
uv run scripts/generate_tiles.py --all      # Full pipeline
uv run scripts/generate_tiles.py --export   # Export GeoJSON
uv run scripts/generate_tiles.py --tiles    # Generate PMTiles (requires tippecanoe)
uv run scripts/generate_tiles.py --upload   # Upload to Cloudflare R2
```

## Operational Tools

### `reconcile_matter_queue.py`

Audit historical matter projections, appearance snapshots, and queue descriptors.
Dry-run is the default; execution is a separate reviewed operator decision.

```bash
uv run python scripts/reconcile_matter_queue.py --limit 1000
uv run python scripts/reconcile_matter_queue.py
uv run python scripts/reconcile_matter_queue.py --execute  # only after reviewing dry-run
```

Execution recomputes each plan under matter/item/queue locks and publishes the
current identity-only matter descriptor. It does not delete data. Re-run the
default dry-run after execution to record the remaining set.

### `resummarize_items.py`

Invalidate summaries produced before a prompt version and atomically reactivate
their authoritative meeting version. Past meetings route to the Batch lane.

```bash
uv run python scripts/resummarize_items.py --below v3 --dry-run
uv run python scripts/resummarize_items.py --below v3 --limit 200 --yes
```

The apply path locks the meeting/items, clears only rows still below the requested
version, computes the current `work_version`, and reactivates that exact queue
version in the same transaction. It never relies on an unversioned best-effort
enqueue.

### `db_viewer.py`
Interactive database viewer and editor. Browse jurisdictions, meetings, items, queue; add/update jurisdictions with Census data lookup.

```bash
uv run scripts/db_viewer.py
```

### `reset_queue.py`
Manage the processing queue.

```bash
uv run scripts/reset_queue.py --stats
uv run scripts/reset_queue.py --reset all --yes
uv run scripts/reset_queue.py --reset failed --yes
```

### `moderate.py`
Content moderation for summaries and user-submitted content.
