"""The single producer + sync deferral: pipeline/ground_truth.py contract.

chunk_pdf's own behavior is covered in tests/chunker/; these tests cover the
stage wrapper -- guard dispatch classification, and the SYNC_CHUNKING=false
deferral branch in the base adapter (archive-only, DEFERRED audit).
"""

import asyncio

import fitz
import pytest

from config import config
from pipeline.ground_truth import produce_ground_truth
from vendors.adapters.base_adapter_async import AsyncBaseAdapter
from vendors.adapters.parsers.router import DEFERRED, TOO_SMALL


AGENDA_LINES = [
    "CITY COUNCIL REGULAR MEETING AGENDA",
    "1. Call to Order and roll call of members of the council",
    "2. Approval of Minutes from the prior regular meeting",
    "3. Public Hearing on Ordinance 2026-14 zoning map amendment",
    "4. Adjournment of the regular meeting",
]


def make_pdf_bytes(pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        y = 72
        for line in AGENDA_LINES:
            page.insert_text((72, y), line, fontsize=11)
            y += 24
    data = doc.tobytes()
    doc.close()
    return data


def test_produce_ground_truth_end_to_end():
    result = asyncio.run(produce_ground_truth(
        make_pdf_bytes(2), vendor="testvendor", slug="testslug", ladder="auto",
    ))
    # shape may or may not parse from a synthetic agenda; the text pass must
    assert result.extraction is not None
    assert result.extraction["ocr_pending"] == 0
    assert "Ordinance 2026-14" in result.extraction["text"]


def test_produce_ground_truth_too_small():
    result = asyncio.run(produce_ground_truth(
        b"tiny", vendor="v", slug="s", ladder="auto",
    ))
    assert result.failure_reason == TOO_SMALL
    assert result.extraction is None


def test_sync_deferral_archives_and_defers(monkeypatch):
    monkeypatch.setattr(config, "SYNC_CHUNKING", False)

    archived = {}

    async def fake_archive(pdf_bytes, source_url=None, banana=None):
        archived["bytes"] = len(pdf_bytes)
        archived["source_url"] = source_url
        archived["banana"] = banana
        return "fakesha"

    import vendors.adapters.base_adapter_async as base_mod
    monkeypatch.setattr(base_mod, "archive_bytes", fake_archive)

    adapter = AsyncBaseAdapter(city_slug="testslug", vendor="testvendor")
    adapter.banana = "testCA"
    result = asyncio.run(adapter._chunk_pdf_bytes(
        make_pdf_bytes(), vendor_id="m1", ladder="agenda",
        source_url="https://example.gov/agenda.pdf",
    ))

    assert result.failure_reason == DEFERRED
    assert result.items == []
    assert archived["source_url"] == "https://example.gov/agenda.pdf"
    assert archived["banana"] == "testCA"
    # the deferral lands in the audit trail so telemetry can count it
    assert result.audit()["failure_reason"] == DEFERRED
    # and adapters record it per meeting like any other chunk outcome
    assert adapter._chunk_audits["m1"][0]["failure_reason"] == DEFERRED


def test_sync_chunking_on_still_chunks(monkeypatch):
    monkeypatch.setattr(config, "SYNC_CHUNKING", True)
    adapter = AsyncBaseAdapter(city_slug="testslug", vendor="testvendor")
    result = asyncio.run(adapter._chunk_pdf_bytes(
        make_pdf_bytes(), vendor_id="m2", ladder="auto",
    ))
    assert result.failure_reason != DEFERRED
    assert result.extraction is not None  # the producer ran


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
