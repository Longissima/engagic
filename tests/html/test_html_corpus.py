"""HTML parser corpus: dialect routing + item structure regression.

Mirror of tests/chunker for the HTML layer: same fixture must keep
matching the same dialect pattern (test_pattern), and produce the same
items (test_items), plus shape invariants. Regenerate via
update_goldens.py and read the diff.
"""

import pytest

import html_corpus_lib as lib

pytestmark = pytest.mark.skipif(
    not lib.GOLDENED,
    reason="no html fixtures/goldens (run fetch_fixtures.py + update_goldens.py)",
)


@pytest.mark.parametrize("entry", lib.GOLDENED, ids=lambda e: e["meeting_id"])
def test_pattern_matches_golden(entry):
    golden = lib.load_golden(entry)
    parsed = lib.run_cached(entry)
    assert parsed.get("html_pattern") == golden["html_pattern"], (
        f"{entry['banana']} dialect rerouted"
    )
    assert len(parsed.get("items") or []) == golden["item_count"]


@pytest.mark.parametrize("entry", lib.GOLDENED, ids=lambda e: e["meeting_id"])
def test_items_match_golden(entry):
    golden = lib.load_golden(entry)
    parsed = lib.run_cached(entry)
    got = [
        {
            "number": it.get("agenda_number", ""),
            "title": (it.get("title") or "")[:120],
            "attachments": len(it.get("attachments") or []),
        }
        for it in (parsed.get("items") or [])
    ]
    assert got == golden["items"]


@pytest.mark.parametrize("entry", lib.GOLDENED, ids=lambda e: e["meeting_id"])
def test_item_invariants(entry):
    parsed = lib.run_cached(entry)
    items = parsed.get("items") or []
    if not items:
        pytest.skip("no items parsed")
    for it in items:
        assert (it.get("title") or "").strip() or (it.get("agenda_number") or "").strip()
        for att in it.get("attachments") or []:
            assert att.get("url") or att.get("name")
