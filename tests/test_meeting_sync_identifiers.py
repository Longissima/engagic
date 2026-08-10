"""Derived identifiers retain their semantic type through matter tracking."""

from types import SimpleNamespace

import pytest

from database.id_generation import generate_matter_id
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator


@pytest.mark.asyncio
async def test_derived_identifier_type_is_written_to_new_matter():
    matter_id = generate_matter_id("alphaCA", matter_file="Contract 6007968")
    stored = []

    class Matters:
        async def store_matter(self, matter, *, conn):
            stored.append(matter)

    orchestrator = MeetingSyncOrchestrator(SimpleNamespace(matters=Matters()))

    async def empty_snapshot(*_args, **_kwargs):
        return SimpleNamespace(matters={}, prior_appearances={})

    orchestrator._load_matter_sync_snapshot = empty_snapshot
    agenda_item = SimpleNamespace(
        id="item-1",
        matter_id=matter_id,
        matter_file="Contract 6007968",
        matter_type="Contract",
        title="Approve contract",
        sequence=1,
        attachments=[],
        meeting_id="meeting-1",
        sponsors=[],
    )
    meeting = SimpleNamespace(
        id="meeting-1",
        banana="alphaCA",
        date=None,
    )

    await orchestrator._track_matters(
        meeting,
        [{"sequence": 1, "title": "Approve contract"}],
        [agenda_item],
        affected_matter_ids={matter_id},
        conn=object(),
    )

    assert len(stored) == 1
    assert stored[0].matter_file == "Contract 6007968"
    assert stored[0].matter_type == "Contract"
