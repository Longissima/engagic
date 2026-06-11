"""One-pass morphology measurement for agenda PDFs.

The chunkers' detection heuristics key off implicit signals (link counts,
TOC shape, page thresholds) scattered across both engines with ~60 inline
threshold comparisons. This module makes the signals EXPLICIT and computes
them in one place, once per document, with no decisions attached:

    profile = profile_doc(doc)   # pure measurement

The profile rides the chunk audit into queue.processing_metadata, so every
processed meeting records what its PDF measurably *is* alongside what the
cascade decided to do about it. That turns heuristic tuning from folklore
("Lynwood broke once") into queries (profile vs winning rung vs failure).

A future classifier (profile -> named morphology) replaces the engines'
divergent detection logic; this module deliberately stops at measurement.

Scanning is bounded to the front pages — agenda structure lives there, and
a 3000-page packet must not cost a full-document scan per fetch.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import fitz

# Agenda structure lives in the front pages. Links beyond this range are
# attachment-internal (staff report citations, budget table references).
LINK_SCAN_PAGES = 15
# Text sampling for the scanned-vs-digital call; matches the long-standing
# router behavior (5 pages, 50 chars).
TEXT_SAMPLE_PAGES = 5
TEXT_LAYER_MIN_CHARS = 50

# Permissive agenda-item numbering: "1.", "12)", "4.a", "4.a.", "8.1",
# "IV.", "B." — a signal, not a parser; counts heading-shaped lines.
_ITEM_NUMBER_RE = re.compile(
    r"^\s*(?:"
    r"\d{1,3}\.(?:\d{1,2}|[a-zA-Z])[.)]?"  # 8.1 / 4.a / 4.a.
    r"|\d{1,3}[.)]"                        # 1. / 12)
    r"|[A-Z][.)]"                          # B.
    r"|[IVXLC]{1,6}[.)]"                   # IV.
    r")\s+\S"
)

# fitz link kinds: GOTO(1)/NAMED(4) jump within the document ("Page 47"
# anchors into the packet); URI(2) points at external attachments.
_INTERNAL_LINK_KINDS = (fitz.LINK_GOTO, fitz.LINK_NAMED)


@dataclass
class PdfProfile:
    page_count: int = 0
    scanned_pages: int = 0          # pages covered by the link/item scan
    text_chars: int = 0             # text chars across TEXT_SAMPLE_PAGES
    has_text_layer: bool = False
    external_links: int = 0         # URI links on scanned pages
    internal_links: int = 0         # within-document jumps on scanned pages
    link_pages: List[int] = field(default_factory=list)  # 1-indexed, distinct
    toc_entries: int = 0
    toc_real_entries: int = 0       # entries resolving to a real page
    toc_distinct_pages: int = 0
    toc_max_depth: int = 0
    toc_depth_counts: Dict[str, int] = field(default_factory=dict)
    item_number_lines: int = 0      # heading-shaped lines on scanned pages

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_count": self.page_count,
            "scanned_pages": self.scanned_pages,
            "text_chars": self.text_chars,
            "has_text_layer": self.has_text_layer,
            "external_links": self.external_links,
            "internal_links": self.internal_links,
            "link_pages": self.link_pages,
            "toc_entries": self.toc_entries,
            "toc_real_entries": self.toc_real_entries,
            "toc_distinct_pages": self.toc_distinct_pages,
            "toc_max_depth": self.toc_max_depth,
            "toc_depth_counts": self.toc_depth_counts,
            "item_number_lines": self.item_number_lines,
        }


def profile_doc(doc: fitz.Document) -> PdfProfile:
    """Measure an open document. Pure measurement — no routing decisions."""
    p = PdfProfile(page_count=doc.page_count)

    toc = doc.get_toc() or []
    p.toc_entries = len(toc)
    real = [e for e in toc if e[2] > 0]
    p.toc_real_entries = len(real)
    p.toc_distinct_pages = len({e[2] for e in real})
    depth_counts: Dict[str, int] = {}
    for level, _title, _page in toc:
        depth_counts[str(level)] = depth_counts.get(str(level), 0) + 1
        p.toc_max_depth = max(p.toc_max_depth, level)
    p.toc_depth_counts = depth_counts

    link_pages = set()
    scan_limit = min(LINK_SCAN_PAGES, doc.page_count)
    p.scanned_pages = scan_limit
    for i in range(scan_limit):
        page = doc[i]

        for link in page.get_links():
            kind = link.get("kind")
            if kind == fitz.LINK_URI:
                p.external_links += 1
                link_pages.add(i + 1)
            elif kind in _INTERNAL_LINK_KINDS:
                p.internal_links += 1
                link_pages.add(i + 1)

        text = str(page.get_text("text"))
        if i < TEXT_SAMPLE_PAGES:
            p.text_chars += len(text.strip())
        for line in text.splitlines():
            if _ITEM_NUMBER_RE.match(line):
                p.item_number_lines += 1

    p.link_pages = sorted(link_pages)
    p.has_text_layer = p.text_chars >= TEXT_LAYER_MIN_CHARS
    return p


def profile_pdf(pdf_path: str) -> PdfProfile:
    """Open, measure, close. Raises whatever fitz.open raises."""
    doc = fitz.open(pdf_path)
    try:
        return profile_doc(doc)
    finally:
        doc.close()
