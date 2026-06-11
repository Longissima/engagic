"""Chunk quality signals: which layer is failing, extraction or segmentation?

Two distinct failure layers produce bad items, and the audit should say
which one to blame per meeting:

- *Extraction-layer*: segmentation was right but the title harvested for an
  item is garbage (TOC bookmark labels like "- Cover Page", attachment
  filenames, phone-number-looking strings). Detected by pattern
  (`garbage_titles`), and usually repairable from the item's own page
  (`repair_titles` — staff-report cover pages carry the real title in
  their SUBJECT:/RE: line).

- *Chunking-layer*: the boundaries themselves are wrong, so titles can be
  plausible yet describe the wrong slices. Pattern-matching can't see
  this; the cheap smell is divergence between the document's own numbered
  heading lines (profile.item_number_lines) and the extracted item count
  (`segmentation_smell`). A 25-numbered-line agenda that chunked into 3
  items is under-split no matter how nice the 3 titles look.

Both signals ride the chunk audit into queue.processing_metadata. Ground
truth (hand-labeled fixtures) remains the only full validation of the
chunking layer; these are the automatable approximations.
"""

import re
from typing import Any, Dict, List, Optional

from config import get_logger

logger = get_logger(__name__)

# --- extraction-layer: garbage title patterns --------------------------------

GARBAGE_TITLE_PATTERNS = [
    ("filename", re.compile(r"\.(pdf|docx?|pptx?|xlsx?)\s*$", re.I)),
    ("cover_page", re.compile(r"^[-–\s]*cover\s*page\s*$", re.I)),
    ("generic_label", re.compile(
        r"^[-–\s]*(agenda|attachment|staff\s*report|item|memo(randum)?|exhibit)\s*$", re.I)),
    ("numeric_or_date", re.compile(r"^[\d\s./:–-]+$")),
    ("empty_or_tiny", re.compile(r"^.{0,2}$")),
]

MAX_TITLE_LEN = 150

# The real item title on staff-report cover pages. Memo headers commonly
# put the label and value on separate text lines ("RE:" / "<title>"), so
# the harvester accepts a same-line remainder OR the next non-empty line.
_SUBJECT_RE = re.compile(r"^\s*(?:subject|re|regarding)\s*:\s*(.*)$", re.I)
_WORD_RE = re.compile(r"[A-Za-z]{4}")

# Filename-title cleanup: the filename usually CONTAINS the real title
# ("2026-412 Agenda Item - Water Shortage Update 2026-0615.pdf").
_FN_EXT_RE = re.compile(r"\.(pdf|docx?|pptx?|xlsx?)\s*$", re.I)
_FN_COPY_RE = re.compile(r"\s*\(\d+\)\s*$|[-_](?:compressed|compr|final|draft|v\d+)\s*$", re.I)
_FN_DATE_TAIL_RE = re.compile(r"[\s_-]*\(?(?:\d{6,8}|\d{4}[-.]\d{2,4}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4})\)?\s*$")
_FN_PREFIX_RE = re.compile(r"^\s*(?:\d{2,4}-\d{2,5}\s*)?(?:agenda\s+item\s*[-–:]?\s*)?", re.I)


def classify_title(title: Optional[str]) -> Optional[str]:
    """Return the garbage-pattern label for a title, or None if it looks real."""
    t = (title or "").strip()
    for label, rx in GARBAGE_TITLE_PATTERNS:
        if rx.search(t):
            return label
    return None


def garbage_titles(items: List[Dict[str, Any]]) -> List[int]:
    """Indices of items whose titles match a garbage pattern."""
    return [i for i, it in enumerate(items) if classify_title(it.get("title"))]


def _accept(cand: str) -> Optional[str]:
    cand = cand.strip(" -–_")
    if _WORD_RE.search(cand) and 4 <= len(cand) and not classify_title(cand):
        return cand[:MAX_TITLE_LEN]
    return None


def _title_from_filename(title: str) -> Optional[str]:
    """Clean a filename-shaped title into the title it contains."""
    t = _FN_EXT_RE.sub("", title.strip())
    for _ in range(3):  # chained suffixes: "... (1)-compressed"
        stripped = _FN_COPY_RE.sub("", t)
        stripped = _FN_DATE_TAIL_RE.sub("", stripped)
        if stripped == t:
            break
        t = stripped
    t = _FN_PREFIX_RE.sub("", t, count=1)
    return _accept(re.sub(r"_+", " ", t))


def _title_from_subject_line(page_text: str) -> Optional[str]:
    """SUBJECT:/RE: harvest only — no generic first-line fallback, which
    just scrapes letterhead ("City of Menlo Park  701 Laurel St...")."""
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        m = _SUBJECT_RE.match(ln)
        if not m:
            continue
        cand = m.group(1).strip() or (lines[i + 1] if i + 1 < len(lines) else "")
        accepted = _accept(cand)
        if accepted:
            return accepted
    return None


def repair_titles(items: List[Dict[str, Any]], pdf_path: str) -> int:
    """Replace garbage-pattern titles where a trustworthy source exists:
    filename-shaped titles are cleaned in place; other garbage harvests
    the SUBJECT:/RE: line from the item's own first page. No acceptable
    candidate -> title kept (the lint keeps flagging it). Repaired items
    keep the original under metadata.original_title."""
    doc = None
    repaired = 0
    try:
        for idx in garbage_titles(items):
            item = items[idx]
            title = item.get("title") or ""
            cand = None

            if classify_title(title) == "filename":
                cand = _title_from_filename(title)

            if cand is None:
                page_start = (item.get("metadata") or {}).get("page_start")
                if page_start:
                    if doc is None:
                        import fitz
                        doc = fitz.open(pdf_path)
                    if 1 <= page_start <= doc.page_count:
                        cand = _title_from_subject_line(
                            str(doc[page_start - 1].get_text("text"))
                        )

            if cand:
                item.setdefault("metadata", {})["original_title"] = title
                item["title"] = cand
                repaired += 1
    except Exception as e:
        logger.debug("title repair failed", error=str(e))
    finally:
        if doc is not None:
            doc.close()
    return repaired


# --- chunking-layer: segmentation smell ---------------------------------------

UNDER_SPLIT_MIN_LINES = 6   # need real numbering signal before suspecting
UNDER_SPLIT_RATIO = 2       # numbered lines >= 2x extracted items
OVER_SPLIT_MIN_ITEMS = 10
OVER_SPLIT_RATIO = 3        # items >= 3x numbered lines (weak: TOC bookmarks
                            # legitimately outnumber front-page text lines)


def segmentation_smell(item_number_lines: int, item_count: int) -> Optional[str]:
    """Divergence between the document's own numbering and what we extracted.

    A smell, not a verdict — item_number_lines only covers the front pages,
    and TOC-driven items may have no textual numbering at all. Reliable in
    one direction: lots of numbered headings + few items = under-split.
    """
    if item_count and item_number_lines >= max(
        UNDER_SPLIT_MIN_LINES, UNDER_SPLIT_RATIO * item_count
    ):
        return "under_split"
    if item_number_lines >= 3 and item_count >= max(
        OVER_SPLIT_MIN_ITEMS, OVER_SPLIT_RATIO * item_number_lines
    ):
        return "over_split"
    return None
