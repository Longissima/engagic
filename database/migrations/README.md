# Database Migrations

Simple versioned SQL migrations for PostgreSQL. No ORM dependencies.

## Usage

```bash
# Apply all pending migrations
python -m database.migrate

# Check migration status
python -m database.migrate --status

# Rollback last migration (if .down.sql exists)
python -m database.migrate --rollback 1
```

## Creating Migrations

1. Create a numbered SQL file in this directory:
   ```
   002_feature_name.sql
   ```

2. Optionally create a rollback file:
   ```
   002_feature_name.down.sql
   ```

## Naming Convention

```
{version}_{name}.sql       # Up migration
{version}_{name}.down.sql  # Down migration (optional)
```

- **version**: 3-digit zero-padded number (001, 002, 003)
- **name**: snake_case description

## Migration Tracking

Applied migrations are tracked in the `schema_migrations` table:

```sql
CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Current Migrations

| Version | Name | Description |
|---------|------|-------------|
| 001 | council_members | Council members + sponsorships tables |
| 019 | jurisdictions | Rename `cities` -> `jurisdictions`, add `type` column (city/county/transit/utility/etc.), rename `county` -> `county_banana` with self-referencing FK for hierarchical jurisdiction relationships. Full rollback support. |
| 029 | pipeline_lifecycle | Durable pipeline runs, job attempts, stage events, and transactional outbox. |
| 030 | batch_lifecycle | Recoverable Batch provider intents, polling clocks, leases, and submission identities. |
| 031 | jurisdiction_sync_lifecycle | Set-wise sync scheduling inputs and jurisdiction lifecycle state. |
| 032 | outbox_delivery | FIFO outbox delivery with per-claim UUID fencing, leases, retries, dead letters, and the shared monotonic work-generation sequence. |
| 033 | queue_claim_ownership | Per-claim queue ownership, stable claim/heartbeat clocks, separate ready-work time, and generation-fenced desired work. |
| 034 | document_source_freshness | Separate cached observations from successful origin validation; persist HTTP validators and retry-attempt state for bounded conditional revalidation. |
| 035 | batch_submission_clock | Record provider acceptance separately from durable pre-provider intent creation so provider wait is measured from the correct boundary. |
| 036 | nullable_appearance_dates | Preserve authoritative appearances for undated meetings by allowing `matter_appearances.appeared_at` to remain NULL. |
| 037 | items_summary_updated_cursor | Add the `(summary_updated_at, id)` item index used by Motioncount incremental summary discovery. |
| 038 | processing_observability | Version filter decisions, retain append-only ingest-path audits, and record corpus extraction outcomes. |

## Guidelines

1. **Atomic**: Each migration runs in a single transaction
2. **Idempotent**: Use `IF NOT EXISTS` / `IF EXISTS` where possible
3. **Forward-only**: Prefer new columns over altering existing ones
4. **Documented**: Include comments explaining design decisions
5. **Tested**: Test on VPS before production deploy

## Rollback Safety

Not all migrations can be safely rolled back:
- Adding columns: Safe to rollback (drop column)
- Dropping columns: **Cannot rollback** (data lost)
- Data migrations: Depend on implementation

If a migration cannot be rolled back, don't create a `.down.sql` file.
