"""Shared loader for the chunker corpus suite.

The corpus is prod-derived (see fetch_fixtures.py). Each fixture runs
through the ladder matching how prod would route it: packet URLs through
the "packet" ladder, agenda URLs through "agenda". Results are cached per
process so the three test modules don't re-chunk the same PDF.
"""

import hashlib
import json
import re
import warnings
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from vendors.adapters.parsers.router import ChunkResult, chunk_pdf

HERE = Path(__file__).parent
MANIFEST_PATH = HERE / "manifest.json"
FIXTURES_DIR = HERE / "fixtures"
GOLDEN_DIR = HERE / "golden"

KIND_LADDER = {"packet": "packet", "agenda": "agenda"}


STALE_FIXTURES: list[str] = []


def _matches_manifest(path: Path, entry: Dict[str, Any]) -> bool:
    if path.stat().st_size != entry.get("size"):
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == entry.get("sha256")


def _load_entries() -> list:
    if not MANIFEST_PATH.exists():
        return []
    manifest = json.loads(MANIFEST_PATH.read_text())
    out = []
    for e in manifest["fixtures"]:
        if e.get("fetch_status") != "ok":
            continue
        path = FIXTURES_DIR / e["filename"]
        if not path.exists():
            continue
        if not _matches_manifest(path, e):
            STALE_FIXTURES.append(e["filename"])
            continue
        out.append(e)
    return out


FETCHED = _load_entries()
GOLDENED = [e for e in FETCHED if (GOLDEN_DIR / f"{e['meeting_id']}.json").exists()]
if STALE_FIXTURES:
    warnings.warn(
        f"ignored {len(STALE_FIXTURES)} fixture(s) that do not match manifest: "
        + ", ".join(STALE_FIXTURES),
        stacklevel=2,
    )

_cache: Dict[str, ChunkResult] = {}


def ladder_for(entry: Dict[str, Any]) -> str:
    return KIND_LADDER[entry["url_kind"]]


def run_cached(entry: Dict[str, Any]) -> ChunkResult:
    mid = entry["meeting_id"]
    if mid not in _cache:
        _cache[mid] = chunk_pdf(
            str(FIXTURES_DIR / entry["filename"]), ladder_for(entry)
        )
    return _cache[mid]


def golden_path(entry: Dict[str, Any]) -> Path:
    return GOLDEN_DIR / f"{entry['meeting_id']}.json"


def load_golden(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = golden_path(entry)
    if not path.exists():
        return None
    return json.loads(path.read_text())


# --- ground-truth scoring (shared by test_ground_truth and grow_truth) --------

def normalize_title(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def titles_match(truth_title: str, extracted_title: str) -> bool:
    t, e = normalize_title(truth_title), normalize_title(extracted_title)
    if not t or not e:
        return False
    if len(t) >= 12 and (t in e or e in t):
        return True
    if SequenceMatcher(None, t, e).ratio() >= 0.6:
        return True
    t_tok, e_tok = set(t.split()), set(e.split())
    overlap = len(t_tok & e_tok)
    return overlap >= 4 and overlap / max(min(len(t_tok), len(e_tok)), 1) >= 0.5


def score_extraction(truth_items, extracted_items) -> Tuple[float, float]:
    """(recall, precision) of extracted items against hand/LLM-read truth."""
    extracted_titles = [it.get("title") or "" for it in extracted_items]
    matched_truth = sum(
        1 for t in truth_items
        if any(titles_match(t["title"], et) for et in extracted_titles)
    )
    matched_extracted = sum(
        1 for et in extracted_titles
        if any(titles_match(t["title"], et) for t in truth_items)
    )
    recall = matched_truth / len(truth_items) if truth_items else 1.0
    precision = matched_extracted / len(extracted_titles) if extracted_titles else 0.0
    return recall, precision


def result_to_golden(entry: Dict[str, Any], result: ChunkResult) -> Dict[str, Any]:
    return {
        "ladder": ladder_for(entry),
        "morphology": result.morphology,
        "quality": result.quality or None,
        "winning_rung": result.winning_rung,
        "parse_method": result.parse_method,
        "failure_reason": result.failure_reason,
        "page_count": result.metadata.get("page_count"),
        "item_count": len(result.items),
        "items": [
            {
                "number": it.get("agenda_number", ""),
                "title": it.get("title", ""),
                "page_start": (it.get("metadata") or {}).get("page_start"),
                "attachments": len(it.get("attachments") or []),
            }
            for it in result.items
        ],
    }
