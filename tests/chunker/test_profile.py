"""PdfProfile: synthetic signal extraction + corpus-wide invariants.

Synthetic tests build minimal PDFs with fitz so each signal is asserted in
isolation; corpus tests confirm the profiler runs on every real fixture and
agrees with what the goldens already pinned.
"""

import fitz
import pytest

import corpus_lib
from vendors.adapters.parsers.pdf_profile import (
    LINK_SCAN_PAGES,
    PdfProfile,
    profile_pdf,
)


def _make_pdf(tmp_path, name, pages):
    """pages: list of text strings, one per page."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    path = tmp_path / name
    doc.save(str(path))
    doc.close()
    return str(path)


def test_text_agenda_with_item_numbers(tmp_path):
    path = _make_pdf(tmp_path, "agenda.pdf", [
        "CITY COUNCIL AGENDA\n1. Call to Order\n2. Roll Call\n"
        "3. Approval of Minutes\n4.a Public Hearing on Zoning\n"
    ])
    p = profile_pdf(path)
    assert p.page_count == 1
    assert p.has_text_layer
    assert p.item_number_lines >= 4
    assert p.external_links == 0
    assert p.internal_links == 0
    assert p.toc_entries == 0


def test_blank_pages_have_no_text_layer(tmp_path):
    path = _make_pdf(tmp_path, "scanned.pdf", ["", "", ""])
    p = profile_pdf(path)
    assert p.page_count == 3
    assert not p.has_text_layer
    assert p.text_chars == 0


def test_toc_shape_measured(tmp_path):
    path = _make_pdf(tmp_path, "packet.pdf", ["Agenda"] + ["body"] * 9)
    doc = fitz.open(path)
    doc.set_toc([
        [1, "Item 1", 2],
        [2, "Staff Report", 3],
        [1, "Item 2", 5],
        [2, "Exhibit A", 6],
        [1, "Item 3", 8],
    ])
    doc.saveIncr()
    doc.close()

    p = profile_pdf(path)
    assert p.toc_entries == 5
    assert p.toc_real_entries == 5
    assert p.toc_distinct_pages == 5
    assert p.toc_max_depth == 2
    assert p.toc_depth_counts == {"1": 3, "2": 2}


def test_external_and_internal_links_counted(tmp_path):
    path = _make_pdf(tmp_path, "linked.pdf", ["1. Item one\n2. Item two", "deep page"])
    doc = fitz.open(path)
    page = doc[0]
    rect = fitz.Rect(72, 60, 200, 80)
    page.insert_link({"kind": fitz.LINK_URI, "from": rect,
                      "uri": "https://example.com/staffreport.pdf"})
    page.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(72, 90, 200, 110),
                      "page": 1})
    doc.saveIncr()
    doc.close()

    p = profile_pdf(path)
    assert p.external_links == 1
    assert p.internal_links == 1
    assert p.link_pages == [1]


def test_scan_is_bounded(tmp_path):
    path = _make_pdf(tmp_path, "monster.pdf", ["text"] * (LINK_SCAN_PAGES + 20))
    p = profile_pdf(path)
    assert p.page_count == LINK_SCAN_PAGES + 20
    assert p.scanned_pages == LINK_SCAN_PAGES


def test_to_dict_round_trips_json(tmp_path):
    import json
    path = _make_pdf(tmp_path, "x.pdf", ["1. Item"])
    d = profile_pdf(path).to_dict()
    assert json.loads(json.dumps(d)) == d


# --- corpus invariants ------------------------------------------------------

pytestmark_corpus = pytest.mark.skipif(
    not corpus_lib.FETCHED, reason="no chunker fixtures"
)


@pytestmark_corpus
@pytest.mark.parametrize("entry", corpus_lib.FETCHED, ids=lambda e: e["meeting_id"])
def test_corpus_results_carry_profiles(entry):
    result = corpus_lib.run_cached(entry)
    assert isinstance(result.profile, PdfProfile)
    assert result.profile.page_count > 0
    assert result.audit()["profile"]["page_count"] == result.profile.page_count


@pytestmark_corpus
@pytest.mark.parametrize(
    "entry",
    [e for e in corpus_lib.GOLDENED
     if (corpus_lib.load_golden(e) or {}).get("parse_method") == "v2_toc"],
    ids=lambda e: e["meeting_id"],
)
def test_toc_winners_show_toc_signals(entry):
    """Fixtures the cascade chunked via TOC must measurably HAVE an outline.

    Deliberately loose (>=1, not >=3): the forced packet ladder skips
    detection, and the corpus contains 1-page agendas whose entire outline
    points at page 1 (nampaID: 13 entries, 1 page, 13 items extracted) —
    a real morphology the >=2-distinct-pages folklore would reject.
    """
    result = corpus_lib.run_cached(entry)
    assert result.profile.toc_real_entries >= 1
