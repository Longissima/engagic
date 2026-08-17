"""The single producer + sync deferral: pipeline/ground_truth.py contract.

chunk_pdf's own behavior is covered in tests/chunker/; these tests cover the
stage wrapper -- guard dispatch classification, and the SYNC_CHUNKING=false
deferral branch in the base adapter (archive-only, DEFERRED audit).
"""

import asyncio
from datetime import datetime, timedelta

import fitz
import pytest

from config import config
from corpus.store import CorpusOriginal, sha256_hex
from parsing.subprocess_guard import GuardCrashed
from pipeline.ground_truth import produce_ground_truth
from vendors.adapters.base_adapter_async import AsyncBaseAdapter
from vendors.adapters.parsers.router import ChunkResult, DEFERRED, TOO_SMALL


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


def test_guard_crash_uses_reduced_recovery_and_links_incomplete_pages(monkeypatch):
    import pipeline.ground_truth as ground_truth

    calls = []

    def fake_guard(target, args, **kwargs):
        calls.append((target, args, kwargs))
        if len(calls) == 1:
            raise GuardCrashed("native parser crash", exitcode=255)
        return ChunkResult(
            items=[{
                "title": "Recovered structural item",
                "sequence": 1,
                "body_text": "[SOURCE EXTRACTION INCOMPLETE: verify source.]",
                "attachments": [],
                "metadata": {
                    "page_start": 7,
                    "page_end": 8,
                    "extraction_incomplete": True,
                    "ocr_pending_pages": [7, 8],
                    "shape_basis": "structural",
                },
            }],
            metadata={"parse_method": "v2_toc"},
            winning_rung="v2:toc",
            extraction={"ocr_pending": 2, "ocr_pending_pages": [7, 8]},
        )

    monkeypatch.setattr(ground_truth, "run_guarded", fake_guard)

    result, recovery = ground_truth._run_chunk_with_recovery(
        "/tmp/not-read-by-fake-guard.pdf", "auto", None
    )
    result.quality["guard_recovery"] = recovery
    ground_truth._attach_incomplete_source_links(
        result, "https://example.gov/agenda.pdf?rev=2#old"
    )

    assert calls[0][0] is ground_truth.chunk_pdf
    assert calls[1][0] is ground_truth.recover_pdf_text
    assert calls[1][2]["timeout"] <= config.CHUNKER_TIMEOUT_SECONDS
    assert recovery["exitcode"] == 255
    assert result.items[0]["attachments"] == [{
        "name": "Source page 7-8 (OCR incomplete)",
        "url": "https://example.gov/agenda.pdf?rev=2#page=7",
        "type": "source_link",
    }]


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


def test_packet_chunking_reuses_fresh_corpus_without_origin_get(monkeypatch):
    pdf_bytes = make_pdf_bytes()
    content_sha256 = sha256_hex(pdf_bytes)
    sightings = []

    class Corpus:
        async def get_original_artifact_by_identity(self, source_url):
            return CorpusOriginal(
                pdf_bytes,
                content_sha256,
                "application/pdf",
                last_validated_at=datetime.now(),
                last_validation_attempt_at=datetime.now(),
            )

        async def record_sighting(self, *args):
            sightings.append(args)

    import vendors.adapters.base_adapter_async as base_mod

    monkeypatch.setattr(base_mod, "get_corpus", lambda: Corpus())
    adapter = AsyncBaseAdapter(city_slug="testslug", vendor="testvendor")
    adapter.banana = "testCA"

    async def forbidden_get(*args, **kwargs):
        raise AssertionError("fresh corpus packet reached the municipal origin")

    captured = {}

    async def chunk_bytes(
        data,
        vendor_id=None,
        ladder="auto",
        source_url=None,
        archived_content_sha256=None,
    ):
        captured.update(
            data=data,
            vendor_id=vendor_id,
            ladder=ladder,
            source_url=source_url,
            archived_content_sha256=archived_content_sha256,
        )
        return ChunkResult(ladder=ladder)

    adapter._get = forbidden_get
    adapter._chunk_pdf_bytes = chunk_bytes
    result = asyncio.run(
        adapter._chunk_packet_pdf(
            "https://example.gov/agenda.pdf", "meeting-1", "agenda"
        )
    )

    assert result.ladder == "agenda"
    assert captured["data"] == pdf_bytes
    assert captured["archived_content_sha256"] == content_sha256
    assert sightings


def test_packet_chunking_conditionally_archives_changed_stable_url(monkeypatch):
    old_bytes = make_pdf_bytes()
    changed_document = fitz.open()
    page = changed_document.new_page()
    page.insert_text((72, 72), "CHANGED AGENDA CONTENT", fontsize=11)
    new_bytes = changed_document.tobytes()
    changed_document.close()
    old_sha = sha256_hex(old_bytes)
    new_sha = sha256_hex(new_bytes)
    requests = []
    archives = []

    class Corpus:
        async def get_original_artifact_by_identity(self, source_url):
            return CorpusOriginal(
                old_bytes,
                old_sha,
                "application/pdf",
                etag='"old"',
                last_validated_at=datetime.now() - timedelta(days=2),
                last_validation_attempt_at=datetime.now() - timedelta(days=2),
            )

        async def archive_original(self, content_sha256, **kwargs):
            archives.append((content_sha256, kwargs))
            return True

        async def record_validation(self, *args, **kwargs):
            return None

        async def record_validation_failure(self, *args, **kwargs):
            raise AssertionError("changed origin response must not fail open")

        async def record_sighting(self, *args, **kwargs):
            return None

    class Response:
        status = 200
        url = "https://example.gov/agenda.pdf"
        headers = {
            "Content-Type": "application/pdf",
            "ETag": '"new"',
        }

        async def read(self):
            return new_bytes

    import vendors.adapters.base_adapter_async as base_mod

    corpus = Corpus()
    monkeypatch.setattr(base_mod, "get_corpus", lambda: corpus)
    adapter = AsyncBaseAdapter(city_slug="testslug", vendor="testvendor")
    adapter.banana = "testCA"

    async def get(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    captured = {}

    async def chunk_bytes(data, *args, **kwargs):
        captured["data"] = data
        captured["archived_content_sha256"] = kwargs.get(
            "archived_content_sha256"
        )
        return ChunkResult(ladder="agenda")

    adapter._get = get
    adapter._chunk_pdf_bytes = chunk_bytes
    asyncio.run(
        adapter._chunk_packet_pdf(
            "https://example.gov/agenda.pdf", "meeting-1", "agenda"
        )
    )

    assert requests == [
        (
            "https://example.gov/agenda.pdf",
            {"headers": {"If-None-Match": '"old"'}},
        )
    ]
    assert archives[0][0] == new_sha
    assert archives[0][1]["etag"] == '"new"'
    assert captured == {
        "data": new_bytes,
        "archived_content_sha256": new_sha,
    }


def test_sub_attachment_resolution_singleflights_fresh_corpus(monkeypatch):
    pdf_bytes = make_pdf_bytes()
    content_sha256 = sha256_hex(pdf_bytes)
    lookups = 0

    class Corpus:
        async def get_original_artifact_by_identity(self, source_url):
            nonlocal lookups
            lookups += 1
            await asyncio.sleep(0.01)
            return CorpusOriginal(
                pdf_bytes,
                content_sha256,
                "application/pdf",
                last_validated_at=datetime.now(),
                last_validation_attempt_at=datetime.now(),
            )

        async def record_sighting(self, *args, **kwargs):
            return None

    import vendors.adapters.base_adapter_async as base_mod

    monkeypatch.setattr(base_mod, "get_corpus", lambda: Corpus())
    adapter = AsyncBaseAdapter(city_slug="testslug", vendor="testvendor")

    async def forbidden_get(*args, **kwargs):
        raise AssertionError("fresh staff report reached the municipal origin")

    adapter._get = forbidden_get
    primary_url = "https://example.gov/staff-report.pdf"
    items = [
        {
            "vendor_item_id": f"item-{number}",
            "attachments": [
                {"name": "Staff report", "url": primary_url, "type": "pdf"}
            ],
        }
        for number in range(2)
    ]

    resolved = asyncio.run(adapter._resolve_sub_attachments(items))

    assert resolved == items
    assert lookups == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
