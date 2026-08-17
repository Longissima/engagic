"""Focused contracts for bounded OnBase detail enrichment."""

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from exceptions import VendorHTTPError
from vendors.adapters.onbase_adapter_async import AsyncOnBaseAdapter


class _TextResponse:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.url = "https://example.test/response"

    async def text(self) -> str:
        return self._text


@pytest.mark.asyncio
async def test_onbase_bounds_concurrent_meeting_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = AsyncOnBaseAdapter("friscoTX")
    active = 0
    peak = 0

    async def inline_to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    # The production call remains a thread offload.  This test only exercises
    # the detail-request scheduling, and keeping parsing inline avoids the
    # sandbox's restricted executor shutdown path.
    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)

    async def fake_get(url: str, **kwargs: Any) -> _TextResponse:
        return _TextResponse()

    def fake_listing(html: str) -> list[dict[str, Any]]:
        return [
            {
                "id": str(index),
                "title": f"Meeting {index}",
                "date": datetime(2026, 8, 10),
            }
            for index in range(10)
        ]

    async def fake_detail(meeting: dict[str, Any]) -> dict[str, Any]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "vendor_id": meeting["id"],
            "title": meeting["title"],
            "start": meeting["date"].isoformat(),
        }

    adapter._get = fake_get  # type: ignore[method-assign]
    adapter._parse_meeting_listing = fake_listing  # type: ignore[method-assign]
    adapter._fetch_meeting_detail = fake_detail  # type: ignore[method-assign]

    meetings = await adapter._fetch_site_meetings(
        "https://example.test/",
        datetime(2026, 8, 1),
        datetime(2026, 8, 31),
    )

    assert len(meetings) == 10
    assert peak == adapter._MEETING_DETAIL_CONCURRENCY


@pytest.mark.asyncio
async def test_onbase_attachment_concurrency_is_shared_across_meetings() -> None:
    adapter = AsyncOnBaseAdapter("friscoTX")
    active = 0
    peak = 0

    async def fake_get(url: str, **kwargs: Any) -> _TextResponse:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        item_id = parse_qs(urlparse(url).query)["itemId"][0]
        return _TextResponse(
            '<a href="/Documents/DownloadFile/report.pdf?itemId='
            f'{item_id}">Report {item_id}</a>'
        )

    adapter._get = fake_get  # type: ignore[method-assign]
    first = [
        {"vendor_item_id": f"a-{index}", "title": "Item", "sequence": index}
        for index in range(6)
    ]
    second = [
        {"vendor_item_id": f"b-{index}", "title": "Item", "sequence": index}
        for index in range(6)
    ]

    first_result, second_result = await asyncio.gather(
        adapter._fetch_item_attachments(first, "meeting-a", "https://example.test"),
        adapter._fetch_item_attachments(second, "meeting-b", "https://example.test"),
    )

    assert peak == adapter._ITEM_DETAIL_CONCURRENCY
    assert all(item.get("attachments") for item in first_result + second_result)


@pytest.mark.asyncio
async def test_onbase_failure_budget_stops_amplification_and_retains_items() -> None:
    adapter = AsyncOnBaseAdapter("friscoTX")
    calls: list[str] = []

    async def failing_get(url: str, **kwargs: Any) -> _TextResponse:
        calls.append(url)
        raise VendorHTTPError(
            "Request timeout",
            vendor="onbase",
            city_slug=adapter.slug,
            url=url,
        )

    adapter._get = failing_get  # type: ignore[method-assign]
    items = [
        {
            "vendor_item_id": str(index),
            "title": f"Item {index}",
            "sequence": index,
        }
        for index in range(20)
    ]

    results = await adapter._fetch_item_attachments(
        items,
        "meeting-timeout",
        "https://example.test",
    )

    assert len(calls) == adapter._ITEM_DETAIL_FAILURE_BUDGET
    assert results == items
    assert all("attachments" not in item for item in results)


@pytest.mark.asyncio
async def test_onbase_keeps_successful_enrichment_when_later_requests_fail() -> None:
    adapter = AsyncOnBaseAdapter("friscoTX")

    async def mixed_get(url: str, **kwargs: Any) -> _TextResponse:
        item_id = parse_qs(urlparse(url).query)["itemId"][0]
        if item_id.startswith("bad"):
            raise VendorHTTPError("Request timeout", vendor="onbase", url=url)
        return _TextResponse(
            '<a href="/Documents/DownloadFile/good.pdf?itemId='
            f'{item_id}">Good attachment</a>'
        )

    adapter._get = mixed_get  # type: ignore[method-assign]
    items = [
        {"vendor_item_id": "good-1", "title": "Good 1", "sequence": 1},
        {"vendor_item_id": "good-2", "title": "Good 2", "sequence": 2},
        {"vendor_item_id": "bad-1", "title": "Bad 1", "sequence": 3},
        {"vendor_item_id": "bad-2", "title": "Bad 2", "sequence": 4},
        {"vendor_item_id": "bad-3", "title": "Bad 3", "sequence": 5},
        {"vendor_item_id": "not-started", "title": "Deferred", "sequence": 6},
    ]

    results = await adapter._fetch_item_attachments(
        items,
        "meeting-partial",
        "https://example.test",
    )

    assert results[0]["attachments"][0]["name"] == "Good attachment"
    assert results[1]["attachments"][0]["name"] == "Good attachment"
    assert len(results) == len(items)
