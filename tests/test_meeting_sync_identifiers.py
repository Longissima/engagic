"""Derived identifiers retain their semantic type through matter tracking."""

from types import SimpleNamespace

import pytest

from database.id_generation import generate_item_id, generate_matter_id
from database.models import AgendaItem
from pipeline.orchestrators.meeting_sync import (
    MeetingSyncOrchestrator,
    _stable_item_id_plan,
)


def test_unique_vendor_item_id_keeps_legacy_identity() -> None:
    items = [
        {"vendor_item_id": "ORD-12", "sequence": 1, "title": "Adopt ordinance"},
        {"vendor_item_id": "RES-9", "sequence": 2, "title": "Adopt resolution"},
    ]

    assert _stable_item_id_plan("alphaCA_deadbeef", items) == [
        generate_item_id("alphaCA_deadbeef", 1, "ORD-12"),
        generate_item_id("alphaCA_deadbeef", 2, "RES-9"),
    ]


def test_section_local_ids_are_stable_across_reorder_and_insertion() -> None:
    original = [
        {
            "vendor_item_id": "A",
            "sequence": 1,
            "title": "Approve the annual paving contract",
            "metadata": {"section": "Consent Calendar"},
        },
        {
            "vendor_item_id": "A",
            "sequence": 2,
            "title": "Open the appeal hearing",
            "metadata": {"section": "Public Hearings"},
        },
    ]
    original_ids = _stable_item_id_plan("alphaCA_deadbeef", original)
    by_title = dict(zip((item["title"] for item in original), original_ids))

    updated = [
        {
            "vendor_item_id": "A",
            "sequence": 1,
            "title": "Receive the auditor presentation",
            "metadata": {"section": "Presentations"},
        },
        {**original[1], "sequence": 2},
        {**original[0], "sequence": 3},
    ]
    updated_ids = _stable_item_id_plan("alphaCA_deadbeef", updated)
    updated_by_title = dict(zip((item["title"] for item in updated), updated_ids))

    assert updated_by_title[original[0]["title"]] == by_title[original[0]["title"]]
    assert updated_by_title[original[1]["title"]] == by_title[original[1]["title"]]
    assert all("_local_" in str(item_id) for item_id in updated_ids)


def test_repeated_sequence_fallback_uses_semantics_not_encounter_order() -> None:
    items = [
        {"vendor_item_id": None, "sequence": 1, "title": "Proclamation"},
        {"vendor_item_id": None, "sequence": 1, "title": "Approve minutes"},
    ]
    first = _stable_item_id_plan("alphaCA_deadbeef", items)
    reversed_ids = _stable_item_id_plan("alphaCA_deadbeef", list(reversed(items)))

    assert dict(zip((item["title"] for item in items), first)) == dict(
        zip((item["title"] for item in reversed(items)), reversed_ids)
    )


def test_exact_semantic_duplicates_collapse_instead_of_getting_dup_suffixes() -> None:
    duplicate = {
        "vendor_item_id": "1",
        "sequence": 1,
        "title": "Approve the minutes",
        "metadata": {"section": "Consent Calendar"},
    }

    planned = _stable_item_id_plan(
        "alphaCA_deadbeef", [duplicate, {**duplicate, "sequence": 2}]
    )

    assert sum(item_id is not None for item_id in planned) == 1
    assert "_dup" not in str(next(item_id for item_id in planned if item_id))


@pytest.mark.asyncio
async def test_semantic_id_migration_reuses_legacy_rows_instead_of_inserting_twins():
    meeting_id = "alphaCA_deadbeef"
    existing = [
        AgendaItem(
            id=f"{meeting_id}_a",
            meeting_id=meeting_id,
            title="Approve the annual paving contract",
            sequence=1,
            agenda_number="A",
            summary="Existing paving summary",
        ),
        AgendaItem(
            id=f"{meeting_id}_a_dup2",
            meeting_id=meeting_id,
            title="Open the appeal hearing",
            sequence=2,
            agenda_number="A",
            summary="Existing appeal summary",
        ),
    ]

    class Items:
        async def get_agenda_items(self, _meeting_id):
            return existing

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(items=Items())
    )
    current = [
        {
            "vendor_item_id": "A",
            "agenda_number": "A",
            "sequence": 2,
            "title": "Open the appeal hearing",
            "metadata": {"section": "Public Hearings"},
        },
        {
            "vendor_item_id": "A",
            "agenda_number": "A",
            "sequence": 1,
            "title": "Approve the annual paving contract",
            "metadata": {"section": "Consent Calendar"},
        },
    ]

    processed = await orchestrator._process_agenda_items(
        current,
        SimpleNamespace(id=meeting_id, banana="alphaCA"),
        {},
    )

    assert {item.id for item in processed} == {item.id for item in existing}
    assert {item.summary for item in processed} == {
        "Existing paving summary",
        "Existing appeal summary",
    }


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
