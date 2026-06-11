"""Audit plumbing: _chunk_pdf_bytes must record audits and set sticky hints.

Covers the base-adapter half of the persistence path: cascade runs are
collected per vendor_id, a win registers a hint for the city, and
fetch_meetings() stamps a summarize_runs() summary onto the meeting dict
(asserted shape-wise here; the meeting_sync -> queue half is one
pass-through parameter).
"""

import asyncio

import pytest

import corpus_lib
from vendors.adapters.base_adapter_async import AsyncBaseAdapter
from vendors.adapters.parsers import router


@pytest.fixture(autouse=True)
def fresh_hint_registry(monkeypatch):
    monkeypatch.setattr(router, "_city_hints", {})


@pytest.fixture
def adapter():
    return AsyncBaseAdapter("testville", "granicus")


def _first_winning_fixture():
    for entry in corpus_lib.GOLDENED:
        golden = corpus_lib.load_golden(entry)
        if golden and golden["winning_rung"]:
            return entry, golden
    return None, None


def test_chunk_records_audit_and_hint(adapter):
    entry, golden = _first_winning_fixture()
    if not entry or not golden:
        pytest.skip("no winning fixtures fetched")
    pdf_bytes = (corpus_lib.FIXTURES_DIR / entry["filename"]).read_bytes()
    ladder = golden["ladder"]

    result = asyncio.run(
        adapter._chunk_pdf_bytes(pdf_bytes, vendor_id="m1", ladder=ladder)
    )

    assert result.winning_rung == golden["winning_rung"]
    # audit recorded under the vendor_id
    runs = adapter._chunk_audits["m1"]
    assert len(runs) == 1
    assert runs[0]["winning_rung"] == golden["winning_rung"]
    assert runs[0]["ladder"] == ladder
    # win registered a sticky hint for this (vendor, slug, ladder)
    assert router.get_city_hint("granicus", "testville", ladder) == golden["winning_rung"]
    # summary is queue-ready
    summary = router.summarize_runs(runs)
    assert summary["winning_rung"] == golden["winning_rung"]
    assert summary["winning_ladder"] == ladder


def test_too_small_pdf_records_classified_failure(adapter):
    result = asyncio.run(
        adapter._chunk_pdf_bytes(b"%PDF tiny", vendor_id="m2", ladder="packet")
    )
    assert result.failure_reason == router.TOO_SMALL
    runs = adapter._chunk_audits["m2"]
    assert runs[0]["failure_reason"] == router.TOO_SMALL
    # no hint registered on failure
    assert router.get_city_hint("granicus", "testville", "packet") is None


def test_hint_reorders_next_cascade(adapter):
    """A seeded hint must change rung order on the next run (observable via
    the attempts trail when the hinted rung loses)."""
    entry = next(
        (e for e in corpus_lib.GOLDENED
         if (corpus_lib.load_golden(e) or {}).get("winning_rung") == "v1:url"
         and (corpus_lib.load_golden(e) or {}).get("ladder") == "agenda"),
        None,
    )
    if not entry:
        pytest.skip("no v1:url agenda fixture fetched")
    pdf_bytes = (corpus_lib.FIXTURES_DIR / entry["filename"]).read_bytes()

    # without hint: v2:url attempted first (and lost) before v1:url won
    cold = asyncio.run(adapter._chunk_pdf_bytes(pdf_bytes, "m3", ladder="agenda"))
    assert [a.rung for a in cold.attempts][:2] == ["v2:url", "v1:url"]

    # the win above set the hint; warm run tries v1:url first and stops there
    warm = asyncio.run(adapter._chunk_pdf_bytes(pdf_bytes, "m4", ladder="agenda"))
    assert [a.rung for a in warm.attempts] == ["v1:url"]
    assert warm.winning_rung == "v1:url"
