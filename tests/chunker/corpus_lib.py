"""Shared loader for the chunker corpus suite.

The corpus is prod-derived (see fetch_fixtures.py). Each fixture runs
through the ladder matching how prod would route it: packet URLs through
the "packet" ladder, agenda URLs through "agenda". Results are cached per
process so the three test modules don't re-chunk the same PDF.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from vendors.adapters.parsers.router import ChunkResult, chunk_pdf

HERE = Path(__file__).parent
MANIFEST_PATH = HERE / "manifest.json"
FIXTURES_DIR = HERE / "fixtures"
GOLDEN_DIR = HERE / "golden"

KIND_LADDER = {"packet": "packet", "agenda": "agenda"}


def _load_entries() -> list:
    if not MANIFEST_PATH.exists():
        return []
    manifest = json.loads(MANIFEST_PATH.read_text())
    out = []
    for e in manifest["fixtures"]:
        if e.get("fetch_status") != "ok":
            continue
        if not (FIXTURES_DIR / e["filename"]).exists():
            continue
        out.append(e)
    return out


FETCHED = _load_entries()
GOLDENED = [e for e in FETCHED if (GOLDEN_DIR / f"{e['meeting_id']}.json").exists()]

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


def result_to_golden(entry: Dict[str, Any], result: ChunkResult) -> Dict[str, Any]:
    return {
        "ladder": ladder_for(entry),
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
