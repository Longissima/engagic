"""Packet-local documents remain evidence without fake download URLs."""

import pytest

from vendors.adapters.parsers import agenda_chunker as v1
from vendors.adapters.parsers import agenda_chunker_v2 as v2


@pytest.mark.parametrize(
    ("module", "internal_name", "public_name"),
    [
        (v1, "_parse_agenda_internal", "parse_agenda_pdf"),
        (v2, "_parse_v2_internal", "parse_agenda_pdf_v2"),
    ],
)
def test_embedded_documents_use_body_text_and_page_metadata(
    monkeypatch, module, internal_name, public_name
):
    parsed = v1._ParsedAgenda()
    parsed.metadata.parse_method = "toc_hierarchical"
    parsed.items = [
        v1._AgendaItem(
            number="4",
            title="Approve the project",
            section="BUSINESS",
            body="Agenda context",
            recommended_action="Approve",
            attachments=[
                v1._Attachment(
                    label="Staff Report.pdf",
                    url="",
                    page_start=12,
                    page_end=18,
                ),
                v1._Attachment(
                    label="External exhibit",
                    url="https://example.test/exhibit.pdf",
                    page_start=1,
                    page_end=1,
                ),
            ],
            memos=[
                v1._MemoContent(
                    full_text="Extracted packet-local report text",
                    page_start=12,
                    page_end=18,
                )
            ],
        )
    ]

    monkeypatch.setattr(module, internal_name, lambda *args, **kwargs: parsed)
    result = getattr(module, public_name)("unused.pdf")
    item = result["items"][0]

    assert item["attachments"] == [
        {
            "name": "External exhibit",
            "url": "https://example.test/exhibit.pdf",
            "type": "pdf",
        }
    ]
    assert "Extracted packet-local report text" in item["body_text"]
    assert item["metadata"]["embedded_documents"] == [
        {
            "name": "Staff Report.pdf",
            "page_start": 12,
            "page_end": 18,
        }
    ]
