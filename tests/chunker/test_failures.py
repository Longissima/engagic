"""Failure classification: when the chunker fails, it must say why.

"If it fails, what is the reason Z" — every fixture that yields zero items
must carry a classified failure_reason from the router taxonomy, and
goldened failures must keep failing the same way (a known-bad PDF that
suddenly fails differently — or starts succeeding — is signal).
"""

import pytest

import corpus_lib
from vendors.adapters.parsers import router

pytestmark = pytest.mark.skipif(
    not corpus_lib.FETCHED,
    reason="no chunker fixtures (run fetch_fixtures.py)",
)

TAXONOMY = {
    router.DOWNLOAD_FAILED,
    router.TOO_SMALL,
    router.OPEN_FAILED,
    router.ENCRYPTED,
    router.NO_TEXT_LAYER,
    router.NO_ITEMS,
    router.ENGINE_ERROR,
    router.OCR_REQUIRED,
}


@pytest.mark.parametrize(
    "entry", corpus_lib.FETCHED, ids=lambda e: e["meeting_id"]
)
def test_failures_are_classified(entry):
    result = corpus_lib.run_cached(entry)
    if result.items:
        assert result.failure_reason is None
        assert result.winning_rung is not None
    else:
        assert result.failure_reason in TAXONOMY, (
            f"unclassified failure: {result.audit()}"
        )
        # every attempted rung must have recorded why it lost
        assert all(a.failure_reason for a in result.attempts)


@pytest.mark.parametrize(
    "entry",
    [e for e in corpus_lib.GOLDENED
     if (corpus_lib.load_golden(e) or {}).get("failure_reason")],
    ids=lambda e: e["meeting_id"],
)
def test_known_failures_stay_classified(entry):
    golden = corpus_lib.load_golden(entry)
    result = corpus_lib.run_cached(entry)
    assert result.failure_reason == golden["failure_reason"], (
        f"{entry['banana']} failure changed: {golden['failure_reason']} -> "
        f"{result.failure_reason or 'NOW SUCCEEDS — regenerate golden?'}"
    )
