"""Canonical vendor-boundary validation contracts."""

from typing import Any

import pytest
from pydantic import ValidationError

from vendors.schemas import validate_meeting_output


def test_invalid_optional_attachment_does_not_drop_parent_meeting() -> None:
    meeting = validate_meeting_output(
        {
            "vendor_id": "meeting-1",
            "title": "City Council",
            "start": "2026-08-19T18:00:00",
            "items": [
                {
                    "vendor_item_id": "item-1",
                    "title": "Approve the contract",
                    "sequence": 1,
                    "attachments": [
                        {
                            "name": "Staff report",
                            "url": " https://example.test/report.pdf ",
                            "type": "pdf",
                        },
                        {"name": "Broken link", "url": ""},
                        {"name": "Missing link"},
                        None,
                    ],
                }
            ],
        }
    )

    assert meeting.items is not None
    assert len(meeting.items) == 1
    assert [attachment.url for attachment in meeting.items[0].attachments] == [
        "https://example.test/report.pdf"
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        {"title": ""},
        {"items": [{"title": "", "sequence": 1}]},
        {"items": [{"title": "Valid", "sequence": 1, "attachments": {}}]},
    ],
)
def test_required_meeting_and_item_contracts_still_fail(
    mutation: dict[str, Any],
) -> None:
    source = {
        "vendor_id": "meeting-1",
        "title": "City Council",
        "start": "2026-08-19T18:00:00",
        **mutation,
    }

    with pytest.raises(ValidationError):
        validate_meeting_output(source)
