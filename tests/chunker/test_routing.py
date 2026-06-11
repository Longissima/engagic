"""Routing regression: each corpus PDF must keep taking the same branch.

"When provided with X packet format from {vendor}, does it properly go to
branch Y" — asserts the winning rung and terminal parse_method against the
golden snapshot. A failure here means a chunker change rerouted a real
city's documents; regenerate goldens (update_goldens.py) only if the
reroute is intentional, and review the diff.
"""

import pytest

import corpus_lib

pytestmark = pytest.mark.skipif(
    not corpus_lib.GOLDENED,
    reason="no chunker fixtures/goldens (run fetch_fixtures.py + update_goldens.py)",
)


@pytest.mark.parametrize(
    "entry", corpus_lib.GOLDENED, ids=lambda e: e["meeting_id"]
)
def test_routing_matches_golden(entry):
    golden = corpus_lib.load_golden(entry)
    result = corpus_lib.run_cached(entry)

    assert result.winning_rung == golden["winning_rung"], (
        f"{entry['banana']} rerouted: {golden['winning_rung']} -> "
        f"{result.winning_rung} (audit: {result.audit()})"
    )
    assert result.parse_method == golden["parse_method"]
    assert len(result.items) == golden["item_count"]
