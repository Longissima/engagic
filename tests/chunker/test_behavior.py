"""Chunk quality: do the chunks make sense, and do they match the golden?

Golden comparison pins item numbers, titles, page starts, and attachment
counts. The invariant tests run on every fixture that yields items —
golden or not — and encode what "sane chunks" means regardless of city.
"""

import pytest

import corpus_lib

pytestmark = pytest.mark.skipif(
    not corpus_lib.FETCHED,
    reason="no chunker fixtures (run fetch_fixtures.py)",
)

@pytest.mark.parametrize(
    "entry", corpus_lib.GOLDENED, ids=lambda e: e["meeting_id"]
)
def test_items_match_golden(entry):
    golden = corpus_lib.load_golden(entry)
    result = corpus_lib.run_cached(entry)

    got = [
        {
            "number": it.get("agenda_number", ""),
            "title": it.get("title", ""),
            "page_start": (it.get("metadata") or {}).get("page_start"),
            "attachments": len(it.get("attachments") or []),
        }
        for it in result.items
    ]
    assert got == golden["items"]


@pytest.mark.parametrize(
    "entry", corpus_lib.FETCHED, ids=lambda e: e["meeting_id"]
)
def test_chunk_invariants(entry):
    result = corpus_lib.run_cached(entry)
    if not result.items:
        pytest.skip(f"no items ({result.failure_reason})")

    page_count = result.metadata.get("page_count") or 0

    sequences = [it.get("sequence") for it in result.items]
    assert sequences == list(range(1, len(result.items) + 1)), "broken sequence"

    for it in result.items:
        title = (it.get("title") or "").strip()
        number = (it.get("number") or it.get("agenda_number") or "").strip()
        assert title or number, f"item with no title and no number: {it}"

        meta = it.get("metadata") or {}
        start, end = meta.get("page_start"), meta.get("page_end")
        if start and page_count:
            assert 1 <= start <= page_count, f"page_start {start} outside doc"
        if start and end:
            assert start <= end, f"page range inverted: {start}-{end}"

        for att in it.get("attachments") or []:
            assert att.get("url") or att.get("name"), f"empty attachment: {att}"
