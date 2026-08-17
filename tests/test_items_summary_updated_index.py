"""Contracts for the Motioncount summary-discovery cursor index."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_NAME = "idx_items_summary_updated_id"


def test_summary_updated_cursor_index_matches_canonical_schema() -> None:
    migration = (
        ROOT / "database/migrations/037_items_summary_updated_cursor.sql"
    ).read_text()
    rollback = (
        ROOT / "database/migrations/037_items_summary_updated_cursor.down.sql"
    ).read_text()
    schema = (ROOT / "database/schema_postgres.sql").read_text()
    definition = f"{INDEX_NAME} ON items(summary_updated_at, id)"

    assert definition in " ".join(migration.split())
    assert definition in " ".join(schema.split())
    assert f"DROP INDEX IF EXISTS {INDEX_NAME}" in rollback
