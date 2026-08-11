"""Contracts for adopting the vendor's own matter lifecycle verdict.

A motion-scoped vote outcome cannot answer "is this matter alive". A motion to
place an ordinance on file passes 15-0 and kills it, so `passed` describes the
motion while the matter is dead. Legistar publishes MatterStatusName for
exactly this question; these tests pin the translation and the write guards.
"""

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional, cast

import pytest
from asyncpg import Connection

from database.id_generation import generate_matter_id
from database.repositories_async.matters import MatterRepository
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from vendors.adapters.legistar_adapter_async import map_matter_status

MATTER_ID = cast(str, generate_matter_id("alphaCA", matter_file="ORD-1"))


class _RecordingConn:
    """Minimal connection double recording UPDATE statements."""

    def __init__(self, rowcount: int = 1):
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._rowcount = rowcount

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return f"UPDATE {self._rowcount}"


def _repo() -> MatterRepository:
    return MatterRepository(cast(Any, SimpleNamespace()))


# ---------------------------------------------------------------------------
# Vendor vocabulary translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Passed", "passed"),
        ("Adopted", "passed"),
        ("Approved", "passed"),
        ("Placed On File", "failed"),
        ("Dead", "failed"),
        ("Denied", "failed"),
        ("In Committee", "referred"),
        ("Withdrawn", "withdrawn"),
        ("Tabled", "tabled"),
        ("Vetoed", "vetoed"),
        ("Enacted", "enacted"),
    ],
)
def test_maps_unambiguous_vendor_statuses(raw: str, expected: str):
    assert map_matter_status(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # "Filed" marks an item COMPLETED in Oakland and killed elsewhere.
        # Mapping it either way asserts a fact we do not have.
        "Filed",
        "Settled",
        "Presentation",
        "Agenda Ready",
        "To be Scheduled",
        "Introduced",
        "some status nobody has seen",
    ],
)
def test_leaves_ambiguous_vendor_statuses_unmapped(raw: str):
    assert map_matter_status(raw) is None


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_absent_status_maps_to_none(raw: Optional[str]):
    assert map_matter_status(raw) is None


def test_mapping_is_case_and_whitespace_insensitive():
    # Legistar returns configured free text; 'In Rules Committee ' is real.
    assert map_matter_status("  pAsSeD  ") == "passed"
    assert map_matter_status("In Rules Committee ") == "referred"


def test_every_mapped_status_is_writable():
    """The translation cannot emit a value the CHECK constraint rejects."""
    produced = {
        map_matter_status(raw)
        for raw in (
            "Passed", "Adopted", "Approved", "Confirmed", "Granted", "Enacted",
            "Signed", "Vetoed", "Failed", "Defeated", "Denied", "Dead",
            "Disallowed", "Placed On File", "Tabled", "Held", "Withdrawn",
            "Referred", "In Committee", "In Commission", "Amended",
        )
    }
    assert produced <= MatterRepository.ALLOWED_STATUSES


# ---------------------------------------------------------------------------
# Write guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, ""])
async def test_absent_status_writes_nothing(status: Optional[str]):
    """A silent vendor must never overwrite a status another sync established."""
    conn = _RecordingConn()
    changed = await _repo().sync_vendor_status(
        MATTER_ID, status, conn=cast(Connection, conn)
    )
    assert changed is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_out_of_vocabulary_status_is_refused_not_raised():
    """One unrecognized status must not abort the surrounding sync transaction.

    Passing a raw vendor string would violate city_matters_status_check; the
    Python guard turns that from a CheckViolationError costing the whole
    meeting into a single skipped write.
    """
    conn = _RecordingConn()
    changed = await _repo().sync_vendor_status(
        MATTER_ID, "Placed On File", conn=cast(Connection, conn)
    )
    assert changed is False
    assert conn.executed == []


@pytest.mark.asyncio
async def test_mapped_status_is_written():
    conn = _RecordingConn(rowcount=1)
    changed = await _repo().sync_vendor_status(
        MATTER_ID, "failed", conn=cast(Connection, conn)
    )
    assert changed is True
    assert len(conn.executed) == 1
    query, args = conn.executed[0]
    assert "UPDATE city_matters" in query
    # The guard lives in SQL too, so a concurrent writer cannot cause a
    # redundant update to report a change.
    assert "IS DISTINCT FROM" in query
    assert args == (MATTER_ID, "failed")


@pytest.mark.asyncio
async def test_unchanged_status_reports_no_change():
    """Re-syncs revisit the same matters constantly; they must not churn rows."""
    conn = _RecordingConn(rowcount=0)
    changed = await _repo().sync_vendor_status(
        MATTER_ID, "failed", conn=cast(Connection, conn)
    )
    assert changed is False


# ---------------------------------------------------------------------------
# Orchestrator wiring
# ---------------------------------------------------------------------------


def _existing_matter_orchestrator(recorded: list):
    """Orchestrator whose snapshot already knows MATTER_ID."""

    class Matters:
        async def get_matters_for_sync_snapshot(self, matter_ids, *, conn):
            return {
                MATTER_ID: SimpleNamespace(
                    metadata=None, canonical_summary=None
                )
            }

        async def get_existing_appearance_matter_ids(
            self, matter_ids, meeting_id, *, conn
        ):
            return {MATTER_ID}

        async def sync_vendor_status(self, matter_id, status, *, conn):
            recorded.append((matter_id, status, conn))
            return True

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            return {MATTER_ID: []}

    database = SimpleNamespace(
        matters=Matters(), items=Items(), council_members=SimpleNamespace()
    )
    return MeetingSyncOrchestrator(database)


def _agenda_item():
    return SimpleNamespace(
        id="item-1",
        meeting_id="meeting-1",
        sequence=1,
        title="An ordinance relating to the municipal identification card.",
        matter_id=MATTER_ID,
        matter_file="ORD-1",
        matter_type="Ordinance",
        attachments=[],
        body_text="",
        summary=None,
        filter_reason=None,
    )


def _meeting():
    return SimpleNamespace(
        id="meeting-1",
        banana="alphaCA",
        title="City Council",
        date=datetime(2026, 6, 2, 9, 0),
    )


@pytest.mark.asyncio
async def test_sync_adopts_status_for_an_already_tracked_matter():
    """The kill vote lands on a matter's LAST appearance, not its first.

    A matter is created once and revisited for years, so the existing-matter
    path is the only place a transition to 'failed' can ever be observed.
    """
    recorded: list = []
    orchestrator = _existing_matter_orchestrator(recorded)
    connection = cast(Connection, object())

    await orchestrator._track_matters(
        cast(Any, _meeting()),
        [{"sequence": 1, "matter_type": "Ordinance", "matter_status": "failed"}],
        cast(Any, [_agenda_item()]),
        affected_matter_ids={MATTER_ID},
        conn=connection,
    )

    assert recorded == [(MATTER_ID, "failed", connection)]


@pytest.mark.asyncio
async def test_sync_skips_the_write_when_vendor_publishes_no_status():
    """Most vendors publish no status; those syncs must not pay for a write."""
    recorded: list = []
    orchestrator = _existing_matter_orchestrator(recorded)

    await orchestrator._track_matters(
        cast(Any, _meeting()),
        [{"sequence": 1, "matter_type": "Ordinance"}],
        cast(Any, [_agenda_item()]),
        affected_matter_ids={MATTER_ID},
        conn=cast(Connection, object()),
    )

    assert recorded == []
