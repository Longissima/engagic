"""Regression tests for the v3.2 primary-source pipeline changes."""

import asyncio
import io
import json
from types import SimpleNamespace
from typing import Any, cast

import fitz
from openpyxl import Workbook

from analysis.analyzer_async import AsyncAnalyzer
from analysis.llm.input_budget import (
    prepare_item_text,
)
from analysis.llm.summarizer import GeminiSummarizer
from database.models import AttachmentInfo
from parsing.pdf import PdfExtractor, _extract_xlsx
from pipeline.processor import Processor


def test_shared_and_item_text_are_not_truncated_before_token_preflight():
    shared = "s" * 3_800_000
    prepared = prepare_item_text(
        "Large item",
        "i" * 100_000,
        shared,
        inline_shared=True,
    )

    assert len(prepared) > 3_800_000
    assert shared in prepared
    assert "i" * 100_000 in prepared
    assert "SHARED CONTEXT" in prepared
    assert "AGENDA ITEM: Large item" in prepared


def _representation_boundary_summarizer():
    summarizer = object.__new__(GeminiSummarizer)
    summarizer.primary_model = "gemini-test"
    summarizer._get_prompt = lambda *args, **kwargs: kwargs.get("text", "")
    return summarizer


def test_streaming_items_use_the_same_representation_boundary_as_batch(monkeypatch):
    explicit_exhibit = "rating review massage provider details\n" * 400
    captured = {}
    summarizer = _representation_boundary_summarizer()

    async def inline(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)

    def summarize_item(title, text, page_count):
        captured.update(title=title, text=text, page_count=page_count)
        return "represented summary", ["other"]

    summarizer.summarize_item = summarize_item
    analyzer = object.__new__(AsyncAnalyzer)
    analyzer.summarizer = summarizer
    request = {
        "item_id": "item-evidence",
        "title": "License review",
        "text": explicit_exhibit,
        "page_count": 78,
        "documents": [
            {
                "name": "Regarding Application_RubmapsReviews",
                "text": explicit_exhibit,
            },
            {"name": "Shared agenda", "text": "shared meeting context"},
        ],
    }

    async def collect():
        return [
            chunk
            async for chunk in analyzer.process_batch_items_async(
                [request], shared_context="shared meeting context"
            )
        ]

    chunks = asyncio.run(collect())

    assert chunks[0][0]["success"] is True
    assert "text_sha256=" in captured["text"]
    assert "shared meeting context" in captured["text"]
    assert "rating review massage provider details" not in captured["text"]


def test_packet_fallback_uses_the_shared_representation_boundary(monkeypatch):
    explicit_exhibit = "rating review massage provider details\n" * 400
    captured = {}
    summarizer = _representation_boundary_summarizer()

    async def inline(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)

    def summarize_meeting(text):
        captured["text"] = text
        return "represented meeting summary"

    summarizer.summarize_meeting = summarize_meeting
    analyzer = object.__new__(AsyncAnalyzer)
    analyzer.summarizer = summarizer

    async def extract(url, banana=None):
        return {
            "success": True,
            "text": explicit_exhibit,
            "method": "pymupdf",
            "page_count": 78,
            "content_sha256": "b" * 64,
            "source_url": "https://example.test/RubmapsReviews.pdf",
            "document_format": "pdf",
        }

    analyzer.extract_document_async = extract

    summary, _method, _participation = asyncio.run(
        analyzer.process_agenda_async(
            "https://example.test/RubmapsReviews.pdf", banana="testCA"
        )
    )

    assert summary == "represented meeting summary"
    assert "text_sha256=" in captured["text"]
    assert "rating review massage provider details" not in captured["text"]


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


def test_processor_keeps_full_public_comment_and_every_named_revision():
    full_comment = "public testimony\n" * 2_000

    class Analyzer:
        async def extract_document_async(self, url, banana=None):
            text = full_comment if "comment" in url else f"contents of {url}"
            return {
                "success": True,
                "text": text,
                "page_count": 10,
                "content_sha256": url[-1] * 64,
                "source_url": url,
                "document_format": "pdf",
            }

    processor = object.__new__(Processor)
    processor.analyzer = cast(Any, Analyzer())
    processor._pdf_semaphore = asyncio.Semaphore(2)
    urls = [
        "https://example.test/contract-Ver1.pdf",
        "https://example.test/contract-Ver2.pdf",
        "https://example.test/comment.pdf",
    ]
    item = SimpleNamespace(
        id="item-1",
        attachments=[
            AttachmentInfo(
                name=("Public Comments" if "comment" in url else url.rsplit("/", 1)[-1]),
                url=url,
                type="pdf",
            )
            for url in urls
        ],
    )

    cache, item_attachments, _shared = asyncio.run(
        processor._build_document_cache([item], banana="testCA")
    )

    assert item_attachments["item-1"] == urls
    assert cache[urls[-1]]["text"] == full_comment
    assert "excerpt" not in cache[urls[-1]]["text"]
