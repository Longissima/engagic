"""Flat-text extractor: synthetic guards and extraction shape."""

import fitz

from vendors.adapters.parsers.text_chunker import (
    MIN_ITEMS,
    TEXT_AGENDA_MAX_PAGES,
    parse_agenda_pdf_text,
)


def _pdf(tmp_path, name, pages):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


AGENDA = (
    "TOWN COUNCIL AGENDA\n"
    "1. Call to Order\n"
    "some preamble text\n"
    "2. Public Comment\n"
    "speakers limited to three minutes\n"
    "3.a Approval of Minutes from March 11\n"
    "draft minutes attached for review\n"
    "4. Adjournment\n"
)


def test_extracts_items_with_bodies(tmp_path):
    result = parse_agenda_pdf_text(_pdf(tmp_path, "a.pdf", [AGENDA]))
    items = result["items"]
    assert result["metadata"]["parse_method"] == "text_items"
    assert [i["agenda_number"] for i in items] == ["1", "2", "3.a", "4"]
    assert [i["sequence"] for i in items] == [1, 2, 3, 4]
    assert items[0]["title"] == "Call to Order"
    assert "preamble" in items[0]["body_text"]
    assert "three minutes" in items[1]["body_text"]
    assert items[0]["attachments"] == []
    assert items[0]["metadata"]["page_start"] == 1


def test_refuses_too_few_headings(tmp_path):
    text = "MEMO\n1. Only item\nbody\n2. Second item\n"
    result = parse_agenda_pdf_text(_pdf(tmp_path, "b.pdf", [text]))
    assert result["items"] == []
    assert MIN_ITEMS == 3


def test_refuses_long_documents(tmp_path):
    pages = [AGENDA] + ["filler"] * TEXT_AGENDA_MAX_PAGES
    result = parse_agenda_pdf_text(_pdf(tmp_path, "c.pdf", pages))
    assert result["items"] == []


def test_dedupes_repeated_header_lines(tmp_path):
    # same heading on both pages = running page header, not two items
    page = "1. Regular Meeting Agenda\n2. Roll Call\n3. New Business\n4. Adjourn\n"
    result = parse_agenda_pdf_text(_pdf(tmp_path, "d.pdf", [page, page]))
    assert len(result["items"]) == 4


def test_titles_need_words(tmp_path):
    text = "1. 03/25/2026\n2. Roll Call\n3. Minutes\n4. Budget\n"
    result = parse_agenda_pdf_text(_pdf(tmp_path, "e.pdf", [text]))
    numbers = [i["agenda_number"] for i in result["items"]]
    assert "1" not in numbers  # date-only title rejected
    assert numbers == ["2", "3", "4"]
