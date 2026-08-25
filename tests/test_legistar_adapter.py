"""Focused contracts for normalizing Legistar API responses."""

from typing import Any

import pytest

from vendors.adapters.legistar_adapter_async import AsyncLegistarAdapter
from vendors.schemas import validate_meeting_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_title,matter_name,expected",
    [
        ("  Agenda item  ", "Matter fallback", "Agenda item"),
        ("  ", "  Matter fallback  ", "Matter fallback"),
        ("  ", "\t", "Untitled Item"),
    ],
)
async def test_api_item_uses_first_non_blank_title(
    monkeypatch: pytest.MonkeyPatch,
    event_title: str,
    matter_name: str,
    expected: str,
) -> None:
    adapter = AsyncLegistarAdapter("newportbeach")

    async def no_votes(_event_item_id: int) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(adapter, "_fetch_event_item_votes_api", no_votes)

    item = await adapter._process_api_item(
        {
            "EventItemId": 123,
            "EventItemTitle": event_title,
            "EventItemMatterName": matter_name,
            "EventItemAgendaSequence": 1,
        }
    )

    assert item is not None
    assert item["title"] == expected

    # Pin the original failure mode: the normalized nested item must not cause
    # the entire parent meeting to fail boundary validation.
    meeting = validate_meeting_output(
        {
            "vendor_id": "public-notice-1",
            "title": "Public Notice",
            "start": "2026-08-19T18:00:00",
            "items": [item],
        }
    )
    assert meeting.items is not None
    assert meeting.items[0].title == expected
