# Migrations

These numbered SQL files define the production schema. Each one documents its own intent in comments at the top, and `ls` is the list of migrations.

```bash
uv run python -m database.migrate              # apply pending
uv run python -m database.migrate --status     # applied vs pending
uv run python -m database.migrate --rollback 1 # undo the last one, if it has a .down.sql
```

- **Files**: `NNN_snake_name.sql` (three digits, next free number), plus an optional `NNN_snake_name.down.sql`. Write a down file only when rolling back loses nothing; a migration that drops data has none.
- **Tracking**: each migration runs in one transaction and is recorded in `schema_migrations` (version, name, applied_at). A migration applied by hand needs its row inserted too.
- **Idempotence**: use `IF NOT EXISTS` / `IF EXISTS` so a partial manual run can be finished by the runner.

## Deploying one

A new file here is a deploy. `Database.create()` calls `assert_schema_current`, which refuses to start any process while a migration is pending. Every service and cron job restarted after the code lands fails until `migrate` runs. Apply it in the same change that ships the file.

Order matters when code and schema change together:
- **Adding a column that code selects**: migrate, then restart. Repository SELECTs name their columns, so they fail against the old schema.
- **Replacing a constraint that running writers name** (for example in `ON CONFLICT`): ship the code, restart the writers, then drop the old constraint.

Also update `../schema_postgres.sql` by hand so it stays a readable reference. It is not applied anywhere, and it already lags several migrations.
