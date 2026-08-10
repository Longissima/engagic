"""Regression tests for the v3.2 primary-source pipeline changes."""

import asyncio
import io
import json
from types import SimpleNamespace
from typing import Any, cast

import fitz
from openpyxl import Workbook

from analysis.llm.input_budget import (
    MAX_ITEM_INPUT_CHARS,
    MAX_SHARED_CONTEXT_CHARS,
    fit_parts_to_budget,
    prepare_item_text,
    render_document_parts,
)
from analysis.llm.summarizer import GeminiSummarizer
from database.models import AttachmentInfo
import parsing.pdf
from parsing.pdf import PdfExtractor, _extract_xlsx
from pipeline.processor import Processor


def test_many_documents_still_obey_strict_budget():
    parts = [(str(i), "x" * 50_001) for i in range(73)]

    fitted, notes = fit_parts_to_budget(parts, MAX_ITEM_INPUT_CHARS)
    rendered, render_notes = render_document_parts(parts, MAX_ITEM_INPUT_CHARS)

    assert sum(len(text) for _, text in fitted) == MAX_ITEM_INPUT_CHARS
    assert len(rendered) <= MAX_ITEM_INPUT_CHARS
    assert notes
    assert render_notes


def test_shared_and_item_text_share_one_budget():
    prepared = prepare_item_text(
        "Large item",
        "i" * 100_000,
        "s" * MAX_SHARED_CONTEXT_CHARS,
        inline_shared=True,
    )

    assert len(prepared) == MAX_ITEM_INPUT_CHARS
    assert "SHARED CONTEXT" in prepared
    assert "AGENDA ITEM: Large item" in prepared


def test_rendered_prompt_is_status_aware():
    summarizer = object.__new__(GeminiSummarizer)
    with open("analysis/llm/prompts_v3.json") as prompt_file:
        summarizer.prompts = json.load(prompt_file)

    prompt = summarizer._get_prompt(
        "item",
        "unified",
        title="Purchase",
        text="Attached quote",
    )

    assert "transactional document IS the purchase" not in prompt
    assert "operative documents are the action itself" not in prompt
    assert "a quote or proposal is not automatically the approved purchase" in prompt
    assert "do not say the jurisdiction is purchasing" in prompt


def _small_redline_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    x, baseline = 72, 72
    text = "keep remove add"
    page.insert_text((x, baseline), text, fontsize=12)

    keep_width = fitz.get_text_length("keep ", fontsize=12)
    remove_width = fitz.get_text_length("remove", fontsize=12)
    gap_width = fitz.get_text_length(" ", fontsize=12)
    add_width = fitz.get_text_length("add", fontsize=12)
    remove_x = x + keep_width
    add_x = remove_x + remove_width + gap_width

    # Word commonly emits tracked-change marks in saturated colors. Pure red
    # was previously rejected by max(rgb) < .85 despite the implementation
    # claiming to support it.
    page.draw_line(
        (remove_x, baseline - 4),
        (remove_x + remove_width, baseline - 4),
        color=(1, 0, 0),
        width=0.7,
    )
    page.draw_line(
        (add_x, baseline + 4),
        (add_x + add_width, baseline + 4),
        color=(0, 0, 1),
        width=0.7,
    )
    page.insert_text((x, 100), "second paragraph", fontsize=12)
    pdf_bytes = document.tobytes()
    document.close()
    return pdf_bytes


def test_small_colored_redline_activates_and_preserves_paragraphs():
    extractor = PdfExtractor(ocr_threshold=0)
    result = extractor.extract_from_bytes(_small_redline_pdf())

    assert "[DELETED: remove]" in result["text"]
    assert "[ADDED: add]" in result["text"]
    assert "[ADDED: add]\n\nsecond paragraph" in result["text"]


def test_pdf_markup_annotations_are_redline_evidence():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "keep remove add", fontsize=12)
    page.add_strikeout_annot(page.search_for("remove")[0])
    page.add_underline_annot(page.search_for("add")[0])
    pdf_bytes = document.tobytes()
    document.close()

    result = PdfExtractor(ocr_threshold=0).extract_from_bytes(pdf_bytes)

    assert "[DELETED: remove]" in result["text"]
    assert "[ADDED: add]" in result["text"]


def _xlsx_bytes(rows: int, columns: int = 1, cell_chars: int = 8) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row_number in range(1, rows + 1):
        sheet.append(
            [f"row-{row_number}-" + ("x" * cell_chars) for _ in range(columns)]
        )
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_xlsx_extraction_is_lossless():
    text = _extract_xlsx(_xlsx_bytes(150, columns=40, cell_chars=500))

    assert text is not None
    assert "row-1-" in text and "row-150-" in text
    assert text.count("row-150-") == 40
    assert "omitted" not in text


def test_xlsx_sanity_cap_guards_pathological_workbooks(monkeypatch):
    monkeypatch.setattr(parsing.pdf, "XLSX_SANITY_MAX_CHARS", 200)
    text = _extract_xlsx(_xlsx_bytes(150))

    assert text is not None
    assert "extraction sanity cap" in text
    assert "row-150-" not in text


def test_processor_routes_spreadsheet_attachments_to_extraction():
    class Analyzer:
        async def extract_document_async(self, url, banana=None):
            return {"success": True, "text": "sheet text", "page_count": 0}

    processor = object.__new__(Processor)
    processor.analyzer = cast(Any, Analyzer())
    processor._pdf_semaphore = asyncio.Semaphore(1)
    item = SimpleNamespace(
        id="item-1",
        attachments=[
            AttachmentInfo(
                name="Pricing.xlsx",
                url="https://example.test/pricing.xlsx",
                type="spreadsheet",
            )
        ],
    )

    cache, item_attachments, _shared = asyncio.run(
        processor._build_document_cache([item], banana="testCA")
    )

    assert item_attachments["item-1"] == ["https://example.test/pricing.xlsx"]
    assert cache["https://example.test/pricing.xlsx"]["text"] == "sheet text"
