"""Contracts for authoritative undated matter appearances."""

import re
from pathlib import Path
from typing import Any, cast

import pytest
from asyncpg import Connection

from database.models import Meeting
from database.repositories_async.matters import MatterRepository
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from vendors.schemas import validate_meeting_output


ROOT = Path(__file__).parents[1]


def test_nullable_appearance_migration_matches_canonical_schema() -> None:
    migration = (
        ROOT / "database/migrations/036_nullable_appearance_dates.sql"
    ).read_text()
    rollback = (
        ROOT / "database/migrations/036_nullable_appearance_dates.down.sql"
    ).read_text()
    schema = (ROOT / "database/schema_postgres.sql").read_text()
    appearance_table = schema.split(
        "CREATE TABLE IF NOT EXISTS matter_appearances (", 1
    )[1].split("\n);", 1)[0]

    assert "ALTER COLUMN appeared_at DROP NOT NULL" in migration
    assert re.search(r"\bappeared_at TIMESTAMP\s*(?:,|--)", appearance_table)
    assert "appeared_at TIMESTAMP NOT NULL" not in appearance_table

    assert "WHERE appeared_at IS NULL" in rollback
    assert "RAISE EXCEPTION" in rollback
    assert "ALTER COLUMN appeared_at SET NOT NULL" in rollback
    assert "UPDATE matter_appearances" not in rollback
    assert "COALESCE" not in rollback


@pytest.mark.parametrize("source", [{}, {"start": None}])
def test_adapter_contract_preserves_authoritative_undated_meeting(source) -> None:
    validated = validate_meeting_output(
        {
            "vendor_id": "vendor-undated-1",
            "title": "City Council",
            **source,
        }
    )

    assert validated.start is None
    assert "start" not in validated.model_dump(exclude_none=True)


@pytest.mark.asyncio
async def test_meeting_sync_persists_authoritative_undated_appearance_as_null() -> None:
    executions: list[tuple[str, tuple[Any, ...]]] = []

    class RecordingConnection:
        async def execute(self, query: str, *args: Any) -> str:
            normalized = " ".join(query.split())
            executions.append((normalized, args))
            return "DELETE 0" if normalized.startswith("DELETE") else "INSERT 1"

    connection = RecordingConnection()
    matters = MatterRepository(cast(Any, object()))
    orchestrator = MeetingSyncOrchestrator(
        cast(Any, type("Database", (), {"matters": matters})())
    )
    meeting = Meeting(
        id="undated-meeting",
        banana="exampleCA",
        title="City Council",
        date=None,
    )

    changes = await orchestrator._reconcile_matter_appearances(
        meeting,
        {},
        conn=cast(Connection, connection),
    )

    insert_query, insert_args = executions[1]
    assert changes == {"deleted": 0, "inserted": 1}
    assert "INSERT INTO matter_appearances" in insert_query
    assert "i.matter_id, i.meeting_id, i.id, $2" in insert_query
    assert insert_args == (
        "undated-meeting",
        None,
        "City Council",
        None,
    )
