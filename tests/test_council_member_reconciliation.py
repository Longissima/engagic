"""Exact sponsor/vote projection contracts for corrected matter links."""

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncpg import Connection

from database.id_generation import generate_council_member_id
from database.repositories_async.council_members import CouncilMemberRepository


class _AttributionConnection:
    def __init__(self, *, item_rows=None, sponsorship_rows=None, vote_rows=None):
        self.item_rows = item_rows or []
        self.sponsorship_rows = sponsorship_rows or []
        self.vote_rows = vote_rows or []
        self.fetch_calls = []
        self.execute_calls = []
        self.executemany_calls = []

    async def fetch(self, query, *args):
        normalized = " ".join(query.split())
        self.fetch_calls.append((normalized, args))
        if "FROM items i JOIN meetings m" in normalized:
            return self.item_rows
        if "FROM sponsorships" in normalized:
            return self.sponsorship_rows
        if normalized.startswith("SELECT DISTINCT matter_id FROM items"):
            return [{"matter_id": "matter-b"}]
        if "FROM votes" in normalized and normalized.startswith("SELECT"):
            return self.vote_rows
        if normalized.startswith("DELETE FROM votes"):
            return [{"council_member_id": "member-old-a"}]
        if "FROM council_members" in normalized:
            return []
        raise AssertionError(f"unexpected fetch: {normalized}")

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.execute_calls.append((normalized, args))
        if normalized.startswith("DELETE FROM sponsorships"):
            return "DELETE 1"
        return "UPDATE 1"

    async def executemany(self, query, args):
        self.executemany_calls.append((" ".join(query.split()), list(args)))


class _ReconciliationRepository(CouncilMemberRepository):
    def __init__(self):
        self.member_calls = []
        self.vote_calls = []

    async def find_or_create_member(
        self,
        banana,
        name,
        appeared_at=None,
        conn=None,
    ):
        member_id = generate_council_member_id(banana, name)
        self.member_calls.append((member_id, name, appeared_at, conn))
        return SimpleNamespace(id=member_id)

    async def record_vote(self, **kwargs):
        self.vote_calls.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_sponsorship_reconcile_preserves_retained_evidence_and_removes_stale():
    banana = "alphaCA"
    alice = generate_council_member_id(banana, "Alice Smith")
    bob = generate_council_member_id(banana, "Bob Jones")
    carol = generate_council_member_id(banana, "Carol Lee")
    stale = generate_council_member_id(banana, "Stale Sponsor")
    connection = _AttributionConnection(
        item_rows=[
            {
                "matter_id": "matter-a",
                "meeting_id": "meeting-1",
                "item_id": "item-a1",
                "sequence": 1,
                "sponsors": ["Alice Smith"],
                "meeting_date": datetime(2026, 1, 1),
            },
            {
                "matter_id": "matter-a",
                "meeting_id": "meeting-2",
                "item_id": "item-a2",
                "sequence": 1,
                "sponsors": ["Carol Lee", "ALICE SMITH"],
                "meeting_date": datetime(2026, 2, 1),
            },
            {
                "matter_id": "matter-b",
                "meeting_id": "meeting-3",
                "item_id": "item-b",
                "sequence": 1,
                "sponsors": ["Bob Jones", "Alice Smith"],
                "meeting_date": datetime(2026, 3, 1),
            },
        ],
        sponsorship_rows=[
            {"council_member_id": alice, "matter_id": "matter-a"},
            {"council_member_id": stale, "matter_id": "matter-a"},
        ],
    )
    repository = _ReconciliationRepository()

    result = await repository.reconcile_matter_sponsorships(
        banana=banana,
        affected_matter_ids=["matter-b", "matter-a", "matter-a"],
        conn=cast(Connection, connection),
    )

    assert result == {"desired": 4, "deleted": 1, "members_recounted": 4}
    assert [call[0] for call in repository.member_calls] == sorted(
        {alice, bob, carol}
    )
    upsert_query, desired_records = connection.executemany_calls[0]
    assert "ON CONFLICT (council_member_id, matter_id) DO UPDATE" in upsert_query
    assert desired_records == [
        (alice, "matter-a", True, 1),
        (carol, "matter-a", False, 2),
        (bob, "matter-b", True, 1),
        (alice, "matter-b", False, 2),
    ]
    delete_query, delete_args = next(
        call
        for call in connection.execute_calls
        if call[0].startswith("DELETE FROM sponsorships")
    )
    assert "NOT EXISTS ( SELECT 1 FROM unnest(" in delete_query
    assert delete_args == (
        ["matter-a", "matter-b"],
        ["matter-a", "matter-a", "matter-b", "matter-b"],
        [alice, carol, bob, alice],
    )
    recount_query, recount_args = connection.execute_calls[-1]
    assert "sponsorship_count = ( SELECT COUNT(*)::int" in recount_query
    assert "vote_count = ( SELECT COUNT(*)::int" in recount_query
    assert recount_args == (sorted({alice, bob, carol, stale}),)


@pytest.mark.asyncio
async def test_vote_reconcile_updates_observed_and_only_deletes_orphan_matter():
    banana = "alphaCA"
    current = generate_council_member_id(banana, "Current Member")
    newcomer = generate_council_member_id(banana, "New Member")
    connection = _AttributionConnection(
        vote_rows=[
            {"council_member_id": "member-old-a", "matter_id": "matter-a"},
            {"council_member_id": current, "matter_id": "matter-b"},
        ]
    )
    repository = _ReconciliationRepository()
    vote_date = datetime(2026, 3, 1)

    result = await repository.reconcile_meeting_votes(
        banana=banana,
        meeting_id="meeting-3",
        affected_matter_ids=["matter-b", "matter-a"],
        observed_votes={
            "matter-a": [{"name": "Unsafe Ghost", "vote": "yes"}],
            "matter-b": [
                {
                    "name": "Current Member",
                    "vote": "nay",
                    "sequence": 2,
                    "metadata": {"source": "corrected"},
                },
                {"name": "New Member", "vote": "aye", "sequence": 3},
            ],
        },
        vote_date=vote_date,
        conn=cast(Connection, connection),
    )

    assert result == {"observed": 2, "deleted": 1, "members_recounted": 3}
    assert [call[0] for call in repository.member_calls] == sorted(
        {current, newcomer}
    )
    observed_calls = [
        (call["matter_id"], call["council_member_id"], call["vote"])
        for call in repository.vote_calls
    ]
    assert observed_calls == sorted([
        ("matter-b", current, "no"),
        ("matter-b", newcomer, "yes"),
    ])
    assert all(call["meeting_id"] == "meeting-3" for call in repository.vote_calls)
    delete_query, delete_args = next(
        call
        for call in connection.fetch_calls
        if call[0].startswith("DELETE FROM votes")
    )
    assert "NOT EXISTS ( SELECT 1 FROM items i" in delete_query
    assert delete_args == ("meeting-3", ["matter-a", "matter-b"])
    recount_args = connection.execute_calls[-1][1]
    assert recount_args == (sorted({"member-old-a", current, newcomer}),)


class _VoteCorrectionConnection:
    def __init__(self):
        self.execute_calls = []

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        self.execute_calls.append((" ".join(query.split()), args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_record_vote_corrects_conflict_without_incrementing_count():
    connection = _VoteCorrectionConnection()
    repository = CouncilMemberRepository(cast(Any, None))

    inserted = await repository.record_vote(
        council_member_id="member-1",
        matter_id="matter-b",
        meeting_id="meeting-3",
        vote="no",
        vote_date=datetime(2026, 3, 1),
        sequence=4,
        metadata={"corrected": True},
        conn=cast(Connection, connection),
    )

    assert inserted is False
    assert len(connection.execute_calls) == 1
    query, args = connection.execute_calls[0]
    assert query.startswith("UPDATE votes SET vote = $4")
    assert "vote IS DISTINCT FROM $4" in query
    assert "UPDATE council_members" not in query
    assert args[3:] == (
        "no",
        datetime(2026, 3, 1),
        4,
        {"corrected": True},
    )
