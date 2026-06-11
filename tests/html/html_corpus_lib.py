"""Shared loader for the HTML parser corpus suite.

Each fixture runs through its vendor's parser entry point exactly as the
adapter would invoke it (Granicus dispatches on the recorded final_url).
Results cached per process.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from vendors.adapters.parsers.civicplus_parser import parse_civicplus_html
from vendors.adapters.parsers.granicus_parser import parse_granicus_html
from vendors.adapters.parsers.primegov_parser import parse_html_agenda

HERE = Path(__file__).parent
MANIFEST_PATH = HERE / "manifest.json"
FIXTURES_DIR = HERE / "fixtures"
GOLDEN_DIR = HERE / "golden"


def _load_entries() -> list:
    if not MANIFEST_PATH.exists():
        return []
    manifest = json.loads(MANIFEST_PATH.read_text())
    return [
        e for e in manifest["fixtures"]
        if e.get("fetch_status") == "ok" and (FIXTURES_DIR / e["filename"]).exists()
    ]


FETCHED = _load_entries()
GOLDENED = [e for e in FETCHED if (GOLDEN_DIR / f"{e['meeting_id']}.json").exists()]

_cache: Dict[str, Dict[str, Any]] = {}


def run_cached(entry: Dict[str, Any]) -> Dict[str, Any]:
    mid = entry["meeting_id"]
    if mid not in _cache:
        html = (FIXTURES_DIR / entry["filename"]).read_text(errors="replace")
        final_url = entry.get("final_url", entry["url"])
        vendor = entry["vendor"]
        if vendor == "civicplus":
            origin = "{0.scheme}://{0.netloc}".format(urlparse(final_url))
            _cache[mid] = parse_civicplus_html(html, origin)
        elif vendor == "granicus":
            _cache[mid] = parse_granicus_html(html, final_url)
        elif vendor == "primegov":
            _cache[mid] = parse_html_agenda(html)
        else:
            raise ValueError(f"no parser mapping for vendor {vendor!r}")
    return _cache[mid]


def golden_path(entry: Dict[str, Any]) -> Path:
    return GOLDEN_DIR / f"{entry['meeting_id']}.json"


def load_golden(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = golden_path(entry)
    return json.loads(path.read_text()) if path.exists() else None


def result_to_golden(entry: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    items = parsed.get("items") or []
    return {
        "html_pattern": parsed.get("html_pattern"),
        "item_count": len(items),
        "items": [
            {
                "number": it.get("agenda_number", ""),
                "title": (it.get("title") or "")[:120],
                "attachments": len(it.get("attachments") or []),
            }
            for it in items
        ],
    }
