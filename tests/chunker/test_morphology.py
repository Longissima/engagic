"""Morphology classifier: rule table behavior + corpus blast-radius tripwire."""

import pytest

import corpus_lib
from vendors.adapters.parsers import morphology as m
from vendors.adapters.parsers.pdf_profile import PdfProfile
from vendors.adapters.parsers.router import LADDERS


def _profile(**kw):
    defaults = dict(page_count=5, has_text_layer=True, text_chars=2000)
    defaults.update(kw)
    return PdfProfile(**defaults)


@pytest.mark.parametrize("profile,expected,rung", [
    (_profile(external_links=8), m.LINKED_AGENDA, "v2:url"),
    (_profile(internal_links=5, page_count=40), m.ANCHORED_PACKET, "v2:auto"),
    (_profile(toc_real_entries=12, page_count=60), m.TOC_PACKET, "v2:toc"),
    (_profile(toc_real_entries=13, page_count=1), m.TOC_AGENDA, "v2:toc"),  # nampa
    (_profile(item_number_lines=9), m.FLAT_TEXT_AGENDA, "text:auto"),
    (_profile(has_text_layer=False), m.SCANNED, None),
    (_profile(), m.MONOLITH, None),
])
def test_classification_rules(profile, expected, rung):
    morph, suggested = m.classify(profile)
    assert morph == expected
    assert suggested == rung


def test_links_beat_headings():
    # linked agendas usually also have numbered lines; links own the doc
    morph, _ = m.classify(_profile(external_links=10, item_number_lines=15))
    assert morph == m.LINKED_AGENDA


def test_outline_beats_scannedness():
    # nampa lesson: an outline slices page ranges without a text layer
    morph, _ = m.classify(_profile(has_text_layer=False, toc_real_entries=13,
                                   page_count=1))
    assert morph == m.TOC_AGENDA


def test_thin_toc_is_not_structure():
    # census: 1-2 entry outlines failed 8/9 — they must not classify as TOC
    morph, _ = m.classify(_profile(toc_real_entries=2, item_number_lines=4))
    assert morph == m.FLAT_TEXT_AGENDA


def test_nav_chrome_threshold():
    morph, _ = m.classify(_profile(external_links=2, item_number_lines=5))
    assert morph == m.FLAT_TEXT_AGENDA


@pytest.mark.skipif(not corpus_lib.GOLDENED, reason="no goldens")
def test_corpus_blast_radius_is_pinned():
    """Suggestions may only change outcomes the goldens have blessed.

    For every fixture, if the classifier's suggestion is in the ladder,
    the golden already reflects whatever the suggestion does — so any
    NEW behavior change from a classifier edit shows up as a routing
    test failure, and this test documents the mechanism. It asserts the
    audit fields are coherent: a used suggestion implies it was in the
    ladder's vocabulary.
    """
    for entry in corpus_lib.GOLDENED:
        result = corpus_lib.run_cached(entry)
        golden = corpus_lib.load_golden(entry)
        assert result.morphology == golden["morphology"], entry["meeting_id"]
        if result.suggestion_used:
            assert result.suggested_rung in LADDERS[corpus_lib.ladder_for(entry)]
