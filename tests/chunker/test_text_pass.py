"""Ground-truth text pass in chunk_pdf: produced, gated, and kept out of audits."""

import tempfile

import fitz
import pytest

from parsing.subprocess_guard import run_guarded
from vendors.adapters.parsers.router import (
    OCR_REQUIRED,
    Attempt,
    ChunkResult,
    _apply_ocr_shape_policy,
    chunk_pdf,
)


AGENDA_LINES = [
    "CITY COUNCIL REGULAR MEETING AGENDA",
    "1. Call to Order and Pledge of Allegiance to the flag of the United States",
    "2. Approval of Minutes from the June 3 regular meeting of the council",
    "3. Public Hearing: Ordinance 2026-14 amending the zoning map for the",
    "   Riverside overlay district as recommended by the planning commission",
    "4. Consent Calendar including monthly financial report and claims",
    "5. Adjournment of the regular meeting of the city council",
]


def make_text_pdf(path: str, pages: int = 2) -> None:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page()
        y = 72
        for line in AGENDA_LINES:
            page.insert_text((72, y), line, fontsize=11)
            y += 24
    doc.save(path)
    doc.close()


def make_blank_pdf(path: str, pages: int = 3) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    doc.close()


@pytest.fixture
def text_pdf():
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        make_text_pdf(tmp.name)
        yield tmp.name


@pytest.fixture
def blank_pdf():
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        make_blank_pdf(tmp.name)
        yield tmp.name


def test_text_layer_pdf_yields_ground_truth(text_pdf):
    result = chunk_pdf(text_pdf, "auto")
    assert result.extraction is not None
    assert result.extraction["success"] is True
    assert result.extraction["ocr_pending"] == 0
    assert "Riverside overlay district" in result.extraction["text"]
    assert result.extraction["page_count"] == 2
    # the audit that lands in queue metadata must not carry megabytes of text
    assert "extraction" not in result.audit()
    assert "text" not in result.audit()


def test_blank_pdf_skips_text_pass(blank_pdf):
    result = chunk_pdf(blank_pdf, "auto")
    # no text layer -> no ground-truth pass; the OCR-owning path owns this doc
    assert result.extraction is None


def test_ground_truth_survives_the_guard(text_pdf):
    # The production dispatch: chunk_pdf through run_guarded, ChunkResult
    # (including the extraction dict) pickled back across the process hop.
    result = run_guarded(chunk_pdf, (text_pdf, "auto"), timeout=120)
    assert result.extraction is not None
    assert result.extraction["ocr_pending"] == 0
    assert "Riverside overlay district" in result.extraction["text"]
    assert result.attempts  # audit trail survived the hop too


def test_mixed_pdf_reports_ocr_pending(text_pdf):
    # Append blank pages to a text PDF: text layer present, but the blank
    # pages would OCR in the process extractor -> ocr_pending > 0 -> the
    # base adapter must NOT persist this text as complete ground truth.
    doc = fitz.open(text_pdf)
    for _ in range(2):
        doc.new_page()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        doc.save(tmp.name)
        doc.close()
        result = chunk_pdf(tmp.name, "auto")
        assert result.extraction is not None
        assert result.extraction["ocr_pending"] == 2
        assert result.extraction["ocr_pending_pages"] == [3, 4]


def test_structural_shape_survives_incomplete_ocr_with_disclosure():
    result = ChunkResult(
        items=[
            {
                "title": "Approve the project",
                "sequence": 1,
                "body_text": "Useful embedded text",
                "attachments": [],
                "metadata": {"page_start": 4, "page_end": 5},
            }
        ],
        metadata={"parse_method": "v2_toc"},
        winning_rung="v2:toc",
        attempts=[Attempt(rung="v2:toc", item_count=1, parse_method="v2_toc")],
        extraction={"ocr_pending": 1, "ocr_pending_pages": [5]},
    )

    _apply_ocr_shape_policy(result)

    assert result.failure_reason is None
    assert result.winning_rung == "v2:toc"
    assert len(result.items) == 1
    item = result.items[0]
    assert item["metadata"]["ocr_pending_pages"] == [5]
    assert item["metadata"]["shape_basis"] == "structural"
    assert item["body_text"].startswith("[SOURCE EXTRACTION INCOMPLETE:")
    assert item["body_text"].endswith("Useful embedded text")


def test_text_derived_shape_is_rejected_when_its_page_needs_ocr():
    result = ChunkResult(
        items=[
            {
                "title": "Possibly broken heading",
                "sequence": 1,
                "attachments": [],
                "metadata": {"page_start": 2, "page_end": 2},
            }
        ],
        metadata={"parse_method": "text_items"},
        winning_rung="text:auto",
        attempts=[Attempt(
            rung="text:auto", item_count=1, parse_method="text_items"
        )],
        extraction={"ocr_pending": 1, "ocr_pending_pages": [2]},
    )

    _apply_ocr_shape_policy(result)

    assert result.items == []
    assert result.winning_rung is None
    assert result.failure_reason == OCR_REQUIRED
    assert result.attempts[0].failure_reason == OCR_REQUIRED


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
