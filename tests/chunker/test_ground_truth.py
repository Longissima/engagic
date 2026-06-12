"""Ground truth: extraction vs what the document actually says.

The only suite layer that validates *correctness* rather than stability.
truth/*.json files contain item lists read directly from the PDFs (by a
human or by Claude reading the rendered pages — provenance in `read_by`).
Recall = fraction of true substantive items an extracted item matches;
precision = fraction of extracted items matching some true item.

expected_recall / expected_precision are RATCHETS pinning the current
measured values: a chunker change may only move them up. When extraction
improves, bump the pins — that bump in the diff is the win, documented.
"""

import json
from pathlib import Path

import pytest

import corpus_lib
from corpus_lib import score_extraction as score  # shared with grow_truth.py

TRUTH_DIR = Path(__file__).parent / "truth"


def _truth_entries():
    out = []
    if not TRUTH_DIR.exists():
        return out
    fetched = {e["meeting_id"]: e for e in corpus_lib.FETCHED}
    for path in sorted(TRUTH_DIR.glob("*.json")):
        if path.stem in fetched:
            out.append((fetched[path.stem], json.loads(path.read_text())))
    return out


@pytest.mark.parametrize(
    "entry,truth", _truth_entries(), ids=lambda v: v["meeting_id"] if isinstance(v, dict) and "meeting_id" in v else ""
)
def test_extraction_against_ground_truth(entry, truth):
    result = corpus_lib.run_cached(entry)
    recall, precision = score(truth["items"], result.items)

    detail = (
        f"{entry['meeting_id']}: recall={recall:.2f} "
        f"(pinned {truth['expected_recall']:.2f}), precision={precision:.2f}, "
        f"true_items={len(truth['items'])}, extracted={len(result.items)}"
    )
    assert recall >= truth["expected_recall"], f"recall regressed — {detail}"
    if truth.get("expected_precision") is not None:
        assert precision >= truth["expected_precision"], f"precision regressed — {detail}"
