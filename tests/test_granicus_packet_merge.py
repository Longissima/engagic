"""Granicus packets enrich an agenda; they do not redefine its item shape."""

from types import SimpleNamespace

import pytest

from vendors.adapters.granicus_adapter_async import (
    AsyncGranicusAdapter,
    _merge_packet_evidence,
)


def test_packet_count_jump_keeps_agenda_shape_and_merges_evidence() -> None:
    agenda_items = [
        {
            "vendor_item_id": "6.a",
            "agenda_number": "6.a",
            "title": "Approve the downtown streetscape contract",
            "sequence": 1,
            "body_text": "Consider approval.",
            "attachments": [],
            "metadata": {"section": "Consent Calendar"},
        },
        {
            "vendor_item_id": "7",
            "agenda_number": "7",
            "title": "Adopt the housing element amendment",
            "sequence": 2,
            "attachments": [],
            "metadata": {"section": "Public Hearings"},
        },
    ]
    packet_items = [
        {
            "agenda_number": "6.a",
            "title": "Item 6.a - Cover Page",
            "sequence": 1,
            "body_text": "Staff recommends awarding the contract to Acme.",
            "attachments": [
                {
                    "name": "Staff report",
                    "url": "https://example.test/6a-report.pdf",
                    "type": "pdf",
                }
            ],
            "metadata": {
                "page_start": 20,
                "page_end": 31,
                "parse_method": "v2_toc",
            },
        },
        {
            "vendor_item_id": "6.a",
            "title": "Item 6.a - Exhibit A",
            "sequence": 2,
            "attachments": [
                {
                    "name": "Exhibit A",
                    "url": "https://example.test/6a-exhibit.pdf",
                    "type": "pdf",
                }
            ],
        },
        {
            "agenda_number": "7",
            "title": "Item 7 - Cover Page",
            "sequence": 3,
            "body_text": "The amendment updates the sites inventory.",
            "attachments": [],
        },
        {
            "agenda_number": "549.1",
            "title": "Section 549.1 of an attached agreement",
            "sequence": 4,
            "body_text": "This is a packet clause, not an agenda item.",
            "attachments": [],
        },
    ]

    merged = _merge_packet_evidence(agenda_items, packet_items)

    assert len(merged) == 2
    assert [item["title"] for item in merged] == [
        "Approve the downtown streetscape contract",
        "Adopt the housing element amendment",
    ]
    assert [attachment["name"] for attachment in merged[0]["attachments"]] == [
        "Staff report",
        "Exhibit A",
    ]
    assert "Staff recommends" in merged[0]["body_text"]
    assert merged[0]["metadata"]["packet_page_start"] == 20
    assert merged[0]["metadata"]["packet_evidence_items"] == 2
    assert "packet clause" not in " ".join(
        str(item.get("body_text") or "") for item in merged
    )
    assert agenda_items[0]["attachments"] == []


def test_repeated_local_number_requires_section_or_title_match() -> None:
    agenda_items = [
        {
            "vendor_item_id": "1",
            "title": "Approve the consent calendar minutes",
            "sequence": 1,
            "attachments": [],
            "metadata": {"section": "Consent Calendar"},
        },
        {
            "vendor_item_id": "1",
            "title": "Open the zoning public hearing",
            "sequence": 2,
            "attachments": [],
            "metadata": {"section": "Public Hearings"},
        },
    ]
    packet_items = [
        {
            "vendor_item_id": "1",
            "title": "Item 1 - Cover Page",
            "sequence": 1,
            "attachments": [
                {
                    "name": "Zoning report",
                    "url": "https://example.test/zoning.pdf",
                    "type": "pdf",
                }
            ],
            "metadata": {"section": "Public Hearings"},
        },
        {
            "vendor_item_id": "1",
            "title": "Item 1 - Ambiguous appendix",
            "sequence": 2,
            "attachments": [
                {
                    "name": "Must remain unmatched",
                    "url": "https://example.test/ambiguous.pdf",
                    "type": "pdf",
                }
            ],
        },
    ]

    merged = _merge_packet_evidence(agenda_items, packet_items)

    assert merged[0]["attachments"] == []
    assert [a["name"] for a in merged[1]["attachments"]] == ["Zoning report"]


def test_packet_defines_shape_only_when_agenda_is_empty() -> None:
    packet_items = [
        {"title": "Packet item", "sequence": 1, "attachments": []},
    ]

    merged = _merge_packet_evidence([], packet_items)

    assert merged == packet_items
    assert merged is not packet_items


@pytest.mark.asyncio
async def test_direct_pdf_fallback_uses_merge_instead_of_packet_replacement() -> None:
    adapter = AsyncGranicusAdapter.__new__(AsyncGranicusAdapter)
    adapter.slug = "test-city"

    async def get_response(_url: str) -> SimpleNamespace:
        return SimpleNamespace(
            url="https://test-city.granicus.com/agenda.pdf",
            headers={"Content-Type": "application/pdf"},
        )

    async def parse_agenda(*_args, **_kwargs):
        return [
            {
                "vendor_item_id": "4",
                "agenda_number": "4",
                "title": "Authorize the bridge design contract",
                "sequence": 1,
                "attachments": [],
            }
        ]

    async def parse_packet(*_args, **_kwargs):
        return [
            {
                "agenda_number": "4",
                "title": "Item 4 - Cover Page",
                "sequence": 1,
                "attachments": [
                    {
                        "name": "Staff report",
                        "url": "https://example.test/report.pdf",
                        "type": "pdf",
                    }
                ],
            },
            {
                "agenda_number": "4.1",
                "title": "Section 4.1 of the design agreement",
                "sequence": 2,
                "attachments": [],
            },
        ]

    adapter._get = get_response
    adapter._parse_pdf_response = parse_agenda
    adapter._parse_packet_pdf = parse_packet

    meeting = await adapter._fetch_meeting_detail(
        {
            "event_id": "event-1",
            "title": "City Council",
            "start": "2026-08-17T18:00:00",
            "agenda_viewer_url": "https://test-city.granicus.com/AgendaViewer.php?event_id=1",
            "packet_url": "https://example.test/packet.pdf",
        }
    )

    assert meeting is not None
    assert meeting["packet_url"] == "https://example.test/packet.pdf"
    assert len(meeting["items"]) == 1
    assert meeting["items"][0]["title"] == "Authorize the bridge design contract"
    assert meeting["items"][0]["attachments"][0]["name"] == "Staff report"
