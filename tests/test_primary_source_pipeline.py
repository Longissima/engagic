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
from parsing.pdf import XLSX_MAX_TOTAL_CHARS, PdfExtractor, _extract_xlsx
from pipeline.processor import Processor
import scripts.backfill_v32_summaries as backfill
from scripts.backfill_v32_summaries import (
    CORPUS_TEXT_FOR_IDENTITY,
    LATEST_MATTER_ITEM,
    STATE_PATH,
    build_item_text,
    refresh_canonical_summaries,
)


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


def test_v32_backfill_does_not_reuse_v31_state():
    assert STATE_PATH.name == "backfill_v32_state.jsonl"


def test_backfill_state_rejects_records_from_other_prompt_versions(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.jsonl"
    records = [
        {
            "prompts_version": "v3.1",
            "kind": "submitted",
            "item_ids": ["old-item"],
            "gemini_job_name": "old-job",
        },
        {
            "prompts_version": backfill.PROMPT_VERSION,
            "kind": "submitted",
            "item_ids": ["current-item"],
            "gemini_job_name": "current-job",
        },
    ]
    state_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setattr(backfill, "STATE_PATH", state_path)

    submitted, _ingested, jobs, _metadata = backfill.load_state()

    assert submitted == {"current-item"}
    assert [job["gemini_job_name"] for job in jobs] == ["current-job"]


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


def test_xlsx_row_and_total_size_caps_are_enforced():
    row_limited = _extract_xlsx(_xlsx_bytes(150))
    size_limited = _extract_xlsx(_xlsx_bytes(100, columns=30, cell_chars=500))

    assert row_limited is not None
    assert "row-100" in row_limited
    assert "row-101" not in row_limited
    assert "[50 more rows omitted]" in row_limited
    assert size_limited is not None
    assert len(size_limited) <= XLSX_MAX_TOTAL_CHARS
    assert "remaining workbook content omitted" in size_limited


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _CorpusConnection:
    def __init__(self, rows):
        self.rows = rows
        self.identities = []

    async def fetchrow(self, query, identity):
        assert query == CORPUS_TEXT_FOR_IDENTITY
        self.identities.append(identity)
        return self.rows.get(identity)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _R2:
    def __init__(self, objects):
        self.objects = objects

    async def get(self, key):
        return self.objects.get(key)


def test_backfill_preserves_identity_query_parameters_and_spreadsheets():
    first_url = "https://example.test/View.ashx?ID=1"
    second_url = "https://example.test/View.ashx?ID=2"
    conn = _CorpusConnection(
        {
            first_url: {"text_key": "one", "page_count": 1},
            second_url: {"text_key": "two", "page_count": 2},
        }
    )
    attachments = [
        {"name": "Quote.xlsx", "url": first_url, "type": "spreadsheet"},
        {"name": "Contract.pdf", "url": second_url, "type": "pdf"},
    ]

    text, pages = asyncio.run(
        build_item_text(
            _Pool(conn),
            _R2({"one": b"FIRST", "two": b"SECOND"}),
            attachments,
        )
    )

    assert conn.identities == [first_url, second_url]
    assert "FIRST" in text and "SECOND" in text
    assert pages == 3


def test_processor_routes_spreadsheet_attachments_to_extraction():
    class Analyzer:
        async def extract_pdf_async(self, url, banana=None):
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


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _CanonicalConnection:
    def __init__(self):
        self.updated = []
        self.topic_deletes = []
        self.topic_inserts = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, matter_id):
        assert query == LATEST_MATTER_ITEM
        return {"id": f"latest-{matter_id}", "summary": f"latest {matter_id}"}

    async def fetch(self, query, item_id):
        assert "FROM item_topics" in query
        return [{"topic": "contracts"}]

    async def execute(self, query, *args):
        if "UPDATE city_matters" in query:
            self.updated.append(args)
            return "UPDATE 1"
        if "DELETE FROM matter_topics" in query:
            self.topic_deletes.append(args)
            return "DELETE 1"
        raise AssertionError(query)

    async def executemany(self, query, records):
        assert "INSERT INTO matter_topics" in query
        self.topic_inserts.extend(records)


def test_canonical_refresh_uses_latest_appearance_after_collection():
    conn = _CanonicalConnection()
    db = SimpleNamespace(pool=_Pool(conn))

    refreshed = asyncio.run(refresh_canonical_summaries(db, {"matter-b", "matter-a"}))

    assert refreshed == 2
    assert conn.updated == [
        ("matter-a", "latest matter-a", ["contracts"]),
        ("matter-b", "latest matter-b", ["contracts"]),
    ]
    assert conn.topic_inserts == [
        ("matter-a", "contracts"),
        ("matter-b", "contracts"),
    ]
