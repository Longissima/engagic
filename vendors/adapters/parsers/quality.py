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

import fitz

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
    keep the original under metadata.original_title.

    Failures are isolated per item: one unreadable page skips that repair,
    not the rest. A failed document open disables page harvesting but
    filename repairs (no doc needed) still run."""
    doc = None
    doc_failed = False
    repaired = 0

    def page_text(page_start: int) -> Optional[str]:
        """Lazy-open the doc once; None when unopenable or out of range."""
        nonlocal doc, doc_failed
        if doc is None and not doc_failed:
            try:
                doc = fitz.open(pdf_path)
            except Exception as e:
                doc_failed = True
                logger.debug("title repair: pdf open failed", error=str(e))
        if doc is None or not (1 <= page_start <= doc.page_count):
            return None
        return str(doc[page_start - 1].get_text("text"))

    try:
        for idx in garbage_titles(items):
            item = items[idx]
            title = item.get("title") or ""
            try:
                cand = None
                if classify_title(title) == "filename":
                    cand = _title_from_filename(title)
                if cand is None:
                    page_start = (item.get("metadata") or {}).get("page_start")
                    text = page_text(page_start) if page_start else None
                    if text is not None:
                        cand = _title_from_subject_line(text)
                if cand:
                    item.setdefault("metadata", {})["original_title"] = title
                    item["title"] = cand
                    repaired += 1
            except Exception as e:
                logger.debug(
                    "title repair failed for item", index=idx, error=str(e)
                )
    finally:
        if doc is not None:
            doc.close()
    return repaired


# --- matter file numbers ------------------------------------------------------

# Leading legislative file tokens ("2026-412 Approve...", "24-0123 Ordinance
# amending..."). Conservative by design: 4-digit-year or 2-digit-year prefix
# only, so "03-25" (a date) can never match. \b stops partial captures from
# longer digit runs.
_MATTER_FILE_RE = re.compile(r"^\s*((?:19|20)\d{2}-\d{1,6}|\d{2}-\d{3,6})\b")


def extract_matter_file(title: Optional[str]) -> Optional[str]:
    """Leading legislative file number from an item title, or None.

    Chunker engines put whatever the document says into the title, and for
    many cities that starts with the matter file. Capturing it (separately
    from title repair, which strips it as noise) lets meeting_sync link the
    item into the matters graph — the same store_matter / appearance-count /
    summary-copy machinery API vendors use.
    """
    m = _MATTER_FILE_RE.match(title or "")
    if not m:
        return None
    token = m.group(1)
    # The remainder must carry a real word — bare numerics never link.
    if not _WORD_RE.search((title or "")[m.end():]):
        return None
    # A year + valid-MMDD shape ("2026-0615") is a probable date, not a file.
    first, _, second = token.partition("-")
    if len(first) == 4 and len(second) == 4:
        mm, dd = int(second[:2]), int(second[2:])
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return None
    return token


def extract_matter_files(items: List[Dict[str, Any]]) -> int:
    """Set item['matter_file'] from leading title tokens where absent.

    Runs before repair_titles — repair strips the very prefix this captures.
    Returns how many items gained a matter_file (rides the chunk audit).
    """
    captured = 0
    for it in items:
        if it.get("matter_file"):
            continue
        matter_file = extract_matter_file(it.get("title"))
        if matter_file:
            it["matter_file"] = matter_file
            captured += 1
    return captured


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
