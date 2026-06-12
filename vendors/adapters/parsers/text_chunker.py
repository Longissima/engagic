"""Flat-text agenda extractor: split by numbered headings, no structure needed.

Recovers the morphology the profiler census exposed: short text agendas
with item-numbered heading lines but no hyperlinks and no usable outline.
9 of 29 corpus failures (boardbook, civicclerk, proudcity, municode, ...)
are this shape — 1-8 page PDFs where the numbering IS the structure.

Items are sliced between heading lines; bodies are the intervening text.
No attachments by definition. Same output contract as the v1/v2 chunkers.

Deliberately conservative: refuses long documents (a 200-page packet with
numbered front matter would swallow the packet into the last item's body),
refuses too-few or too-many headings, and dedupes repeated header/footer
lines. The corpus goldens police the rest.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import fitz

from config import get_logger

logger = get_logger(__name__)

# All nine corpus specimens are 1-8 pages; text agendas are short by nature.
TEXT_AGENDA_MAX_PAGES = 20
MIN_ITEMS = 3
MAX_HEADINGS = 80
MAX_BODY_CHARS = 15_000

# Same shapes the profiler counts, but capturing number + title for
# extraction. Title must carry a real word (kills "1. 03/25/2026" lines).
_HEADING_RE = re.compile(
    r"^\s*("
    r"\d{1,3}\.(?:\d{1,2}|[a-zA-Z])[.)]?"  # 8.1 / 4.a / 4.a.
    r"|\d{1,3}[.)]"                        # 1. / 12)
    r"|[A-Z][.)]"                          # B.
    r"|[IVXLC]{1,6}[.)]"                   # IV.
    r")\s+(\S.*)$"
)
_TITLE_WORD_RE = re.compile(r"[A-Za-z]{3}")


def _empty(page_count: int) -> Dict[str, Any]:
    return {
        "items": [],
        "metadata": {"page_count": page_count, "parse_method": ""},
    }


def parse_agenda_pdf_text(
    pdf_path: str, force_method: Optional[str] = None
) -> Dict[str, Any]:
    """Extract items from a text-only agenda. force_method accepted for
    engine-contract compatibility; there is only one method here."""
    doc = fitz.open(pdf_path)
    try:
        page_count = doc.page_count
        if page_count > TEXT_AGENDA_MAX_PAGES:
            return _empty(page_count)
        lines: List[Tuple[int, str]] = []
        for i in range(page_count):
            for raw in str(doc[i].get_text("text")).splitlines():
                lines.append((i, raw))
    finally:
        doc.close()

    headings = []  # (line_idx, page_idx, number, title)
    seen: set = set()
    for idx, (page_idx, raw) in enumerate(lines):
        m = _HEADING_RE.match(raw)
        if not m:
            continue
        title = m.group(2).strip()
        if not _TITLE_WORD_RE.search(title):
            continue
        number = m.group(1).rstrip(".)")
        # Repeated (number, title) pairs are page headers/footers, not items
        if (number, title) in seen:
            continue
        seen.add((number, title))
        headings.append((idx, page_idx, number, title))

    if not MIN_ITEMS <= len(headings) <= MAX_HEADINGS:
        return _empty(page_count)

    items: List[Dict[str, Any]] = []
    for seq, (idx, page_idx, number, title) in enumerate(headings):
        end_idx = headings[seq + 1][0] if seq + 1 < len(headings) else len(lines)
        body = "\n".join(t for _, t in lines[idx + 1 : end_idx]).strip()
        end_page = lines[end_idx - 1][0] if end_idx > idx + 1 else page_idx

        # No vendor_item_id: heading numbers restart per section ("1." in
        # consent AND regular business), and generate_item_id would collapse
        # duplicates into one row. The sequence fallback is the real identity.
        item: Dict[str, Any] = {
            "title": title,
            "sequence": seq + 1,
            "agenda_number": number,
            "attachments": [],
            "metadata": {
                "page_start": page_idx + 1,
                "page_end": end_page + 1,
                "parse_method": "text_items",
            },
        }
        if body:
            item["body_text"] = body[:MAX_BODY_CHARS]
        items.append(item)

    logger.debug(
        "text chunker extracted items",
        item_count=len(items),
        page_count=page_count,
    )
    return {
        "items": items,
        "metadata": {"page_count": page_count, "parse_method": "text_items"},
    }
