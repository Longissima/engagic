"""
agenda_chunker.py

Unified chunker for municipal agenda PDFs across vendors
(Granicus, Legistar, CivicPlus, CivicClerk, etc.)

Two extraction paths:
  1. TOC-based: When PDF has a bookmark/outline tree with embedded memos
     as subsequent pages. Chunks by page ranges, extracts memo content.
  2. URL-based: When agenda items have hyperlinked attachment URLs.
     Extracts items and assigns attachment links by position.

Dispatch: If TOC exists with multi-page structure, use TOC path.
Otherwise use URL path. Both paths share the same item detection logic.

Dependencies: PyMuPDF (fitz)

Usage:
    from agenda_chunker import parse_agenda
    result = parse_agenda("path/to/agenda.pdf")
    print(result.to_json())
"""

import fitz
import re
import json
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Attachment:
    label: str
    url: str            # URL for link-based; empty for embedded
    page_start: int     # 1-indexed; for embedded attachments
    page_end: int       # 1-indexed; for embedded attachments
    bbox: list = field(default_factory=list)

@dataclass
class MemoContent:
    """Extracted content from an embedded staff memo page range."""
    subject: str = ""
    summary: str = ""
    fiscal_info: str = ""
    recommended_action: str = ""
    submitted_by: str = ""
    full_text: str = ""
    page_start: int = 0
    page_end: int = 0

@dataclass
class AgendaItem:
    number: str
    title: str
    section: str
    body: str
    recommended_action: str
    attachments: list = field(default_factory=list)   # List[Attachment]
    memos: list = field(default_factory=list)          # List[MemoContent]
    page_start: int = 0
    page_end: int = 0

@dataclass
class AgendaMetadata:
    title: str = ""
    body_name: str = ""
    meeting_date: str = ""
    meeting_type: str = ""
    page_count: int = 0
    parse_method: str = ""   # "toc" or "url"
    pdf_metadata: dict = field(default_factory=dict)

@dataclass
class ParsedAgenda:
    metadata: AgendaMetadata = field(default_factory=AgendaMetadata)
    sections: list = field(default_factory=list)
    items: list = field(default_factory=list)
    orphan_links: list = field(default_factory=list)
    orphan_memos: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

ITEM_NUM_RE = re.compile(
    r'^[\s]*'
    r'('
    r'\d{1,2}(?:\.\d{1,2}){1,3}'
    r'|\d{1,2}\.'
    r'|[A-Z]\.'
    r'|[a-z]\.'
    r'|[IVXLC]+\.'
    r')'
    r'\s*'
)

SECTION_PATTERNS = [
    r'CALL\s+TO\s+ORDER',
    r'PUBLIC\s+COMMENTS?',
    r'CLOSED\s+SESSION',
    r'ROLL\s+CALL',
    r'PLEDGE\s+OF\s+ALLEGIANCE',
    r'INVOCATION(?:\s+AND\s+PLEDGE)?',
    r'REPORT\s+FROM\s+CLOSED\s+SESSION',
    r'SPECIAL\s+PRESENTATIONS?',
    r'ADDITIONS?,?\s+DELETIONS?,?\s+(?:AND\s+)?REORDERING(?:\s+TO\s+THE\s+AGENDA)?',
    r'COMMUNITY\s+INPUT',
    r'PUBLIC\s+INPUT',
    r'CONSENT\s+(?:CALENDAR|AGENDA)',
    r'PUBLIC\s+HEARING[S]?',
    r'(?:REGULAR\s+)?DISCUSSION(?:\s+ITEMS?)?',
    r'(?:REGULAR\s+)?ACTION(?:\s+ITEMS?)?',
    r'(?:REGULAR\s+)?BUSINESS(?:\s+ITEMS?)?',
    r'NEW\s+BUSINESS',
    r'OLD\s+BUSINESS',
    r'UNFINISHED\s+BUSINESS',
    r'CITY\s+MANAGER.{0,5}S?\s+REPORT',
    r'CITY\s+ATTORNEY.{0,5}S?\s+REPORT',
    r'ANNOUNCEMENTS?',
    r'COUNCIL\s+COMMENTS?',
    r'COMMITTEE\s+UPDATES?',
    r'BOARD\s+(?:MEMBER\s+)?COMMENTS?',
    r'STAFF\s+COMMUNICATIONS?',
    r'CORRESPONDENCE',
    r'ADJOURNMENT',
    r'RECESS',
    r'RECONVENE',
    r'INFORMAL\s+COMMUNICATIONS?\s+FROM\s+THE\s+FLOOR',
    r'REPORTS?$',
    r'FINANCE\s+ITEMS?',
    # CivicPlus / Planning Commission
    r'THESE\s+ITEMS\s+(?:WILL\s+)?REQUIRE',
    r'CONSENT\s*[-\u2013\u2014]\s*ITEMS\s+FOR\s+\w+',
    r'APPROVAL\s+OF\s+MINUTES',
    r'RULES\s+FOR\s+CONDUCTING',
    r'REGULAR\s+AGENDA',
    r'COMMUNICATIONS?$',
    r'DIRECTOR.{0,5}S?\s+COMMENTS?',
    r'COMMISSIONERS?.{0,5}\s+COMMENTS?',
    r'ADJOURN$',
]

SECTION_RE = re.compile(
    r'^\s*(?:\d{1,2}\.?\s+)?(' + '|'.join(SECTION_PATTERNS) + r')[:\s]*$',
    re.IGNORECASE
)

REC_ACTION_RE = re.compile(r'Recommended\s+Action\s*:', re.IGNORECASE)

CASE_NUM_RE = re.compile(
    r'(?:Case|CUP|CS|TA|ZA|SUP|VAR|CPA|GPA|SP|DR|PM|TTM|TPM|ZC|PP|CDP|EIR)'
    r'[\s\-]*\d',
    re.IGNORECASE
)

CONSENT_PREFIX_RE = re.compile(
    r'^CONSENT\s+(?:FOR\s+)?(?:APPROVAL|DEFERRAL|WITHDRAWAL|DENIAL)',
    re.IGNORECASE
)

ATTACHMENT_URL_PATTERNS = [
    r'legistarweb.*\.s3\.amazonaws\.com',
    r'granicus.*\.s3\.amazonaws\.com',
    r'granicus_production',
    r'hdlegisuite\.',
    r'civicplus\.',
    r'\.pdf$',
    r'\.docx?$',
    r'\.xlsx?$',
    r'/uploads?/attachment',
    r'/attachments/',
    r'/ViewFile/',
    r'/LinkClick\.aspx',
]
ATTACHMENT_URL_RE = re.compile('|'.join(ATTACHMENT_URL_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Shared text extraction
# ---------------------------------------------------------------------------

def _extract_page_text_with_positions(page):
    lines = []
    td = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in td.get("blocks", []):
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            spans = line.get("spans", [])
            if not spans:
                continue
            full_text = "".join(s["text"] for s in spans).strip()
            if not full_text:
                continue
            dominant = max(spans, key=lambda s: len(s["text"]))
            is_bold = bool(dominant["flags"] & 16)
            lines.append({
                "text": full_text,
                "bbox": list(line["bbox"]),
                "y0": line["bbox"][1],
                "y1": line["bbox"][3],
                "x0": line["bbox"][0],
                "is_bold": is_bold,
                "font_size": dominant["size"],
                "font_name": dominant["font"],
                "page": page.number,
            })
    lines.sort(key=lambda line: (line["y0"], line["x0"]))
    return lines


def _extract_links(page):
    links = []
    for link in page.get_links():
        if link.get("kind") != 2:
            continue
        uri = link.get("uri", "")
        if not uri:
            continue
        bbox = link.get("from", fitz.Rect())
        display_text = _get_link_display_text(page, fitz.Rect(bbox))
        links.append({
            "url": uri,
            "label": display_text,
            "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
            "y_center": (bbox.y0 + bbox.y1) / 2,
            "page": page.number,
        })
    return links


def _get_link_display_text(page, link_rect):
    td = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    parts = []
    for block in td.get("blocks", []):
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                span_rect = fitz.Rect(span["bbox"])
                intersection = span_rect & link_rect
                if intersection.is_empty or intersection.width < 1:
                    continue
                span_y_center = (span_rect.y0 + span_rect.y1) / 2
                if link_rect.y0 <= span_y_center <= link_rect.y1:
                    text = span["text"].strip()
                    if text:
                        parts.append(text)
    return " ".join(parts) if parts else ""


def _is_section_header(text):
    return bool(SECTION_RE.match(text.strip()))


def _is_mostly_upper(text):
    alpha = [c for c in text if c.isalpha()]
    if not alpha:
        return True
    return sum(1 for c in alpha if c.isupper()) / len(alpha) > 0.7


def _extract_section_name(text):
    text = text.strip()
    text = re.sub(r'^\d+\.?\s*', '', text)
    text = re.sub(r'[:\s]+$', '', text)
    return text.upper()


def _match_item_number(text):
    m = ITEM_NUM_RE.match(text)
    if m:
        num = m.group(1).rstrip('.')
        remainder = text[m.end():].strip()
        return num, remainder
    return None, text


def _is_likely_item_header(line, lines_context=None):
    """Returns (is_item, number, title_text, lines_consumed)."""
    num, remainder = _match_item_number(line["text"])
    if num is None:
        return False, None, None, 0

    if '.' in num:
        return True, num, remainder, 0

    if remainder:
        alpha_chars = [c for c in remainder if c.isalpha()]
        if not alpha_chars:
            return False, None, None, 0
        if CASE_NUM_RE.search(remainder):
            return True, num, remainder, 0
        if CONSENT_PREFIX_RE.match(remainder):
            return True, num, remainder, 0
        upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
        if upper_ratio > 0.7 or line["is_bold"]:
            return True, num, remainder, 0
        if remainder[0].islower() or len(remainder) > 100:
            return False, None, None, 0
        return False, None, None, 0

    # Standalone number line
    if lines_context is not None:
        line_idx, all_lines, current_section = lines_context
        agenda_sections = {
            'CONSENT AGENDA', 'CONSENT CALENDAR', 'REGULAR AGENDA',
            'PUBLIC HEARINGS', 'DISCUSSION', 'ACTION', 'NEW BUSINESS',
            'OLD BUSINESS', 'UNFINISHED BUSINESS', 'SPECIAL PRESENTATIONS',
            'THESE ITEMS WILL REQUIRE APPROVAL BY COUNCIL',
            'THESE ITEMS REQUIRE ONLY PLANNING COMMISSION APPROVAL',
            'APPROVAL OF MINUTES',
        }
        in_agenda_section = any(
            s in (current_section or '').upper() for s in agenda_sections
        ) or (current_section or '').upper() in agenda_sections
        if not in_agenda_section:
            return False, None, None, 0

        title_parts = []
        consumed = 0
        for j in range(line_idx + 1, min(line_idx + 5, len(all_lines))):
            next_text = all_lines[j]["text"].strip()
            if not next_text:
                consumed += 1
                continue
            next_num, next_rem = _match_item_number(next_text)
            if next_num is not None and not next_rem:
                break
            if _is_section_header(next_text) and (all_lines[j]["is_bold"] or _is_mostly_upper(next_text)):
                break
            title_parts.append(next_text)
            consumed = j - line_idx
            if CASE_NUM_RE.search(next_text) or CONSENT_PREFIX_RE.match(next_text):
                break
            if len(title_parts) >= 2:
                break
        if title_parts:
            return True, num, " ".join(title_parts), consumed

    return False, None, None, 0


def _looks_like_attachment_label(text, all_links, page, y0):
    for link in all_links:
        if link["page"] == page and abs(link["y_center"] - y0) < 15:
            return True
    return False


def _is_attachment_url(url):
    if not url:
        return False
    if ATTACHMENT_URL_RE.search(url):
        return True
    if re.search(r'\.(pdf|docx?|xlsx?|pptx?|csv|txt)(\?.*)?$', url, re.IGNORECASE):
        return True
    return False


def _extract_meeting_metadata(lines, meta):
    first_lines = [line["text"] for line in lines[:30]]
    joined = "\n".join(first_lines)

    body_patterns = [
        r'(CITY\s+COUNCIL)', r'(CITY\s+COMMISSION)',
        r'(PLANNING\s+(?:AND\s+ZONING\s+)?COMMISSION)',
        r'(BOARD\s+OF\s+SUPERVISORS)', r'(TOWN\s+COUNCIL)',
        r'(VILLAGE\s+BOARD)', r'(BOARD\s+OF\s+(?:DIRECTORS|TRUSTEES))',
    ]
    for pat in body_patterns:
        m = re.search(pat, joined, re.IGNORECASE)
        if m:
            meta.body_name = m.group(1).title()
            break

    type_patterns = [
        (r'REGULAR\s+MEETING', "Regular Meeting"),
        (r'SPECIAL\s+MEETING', "Special Meeting"),
        (r'ADJOURNED\s+(?:REGULAR\s+)?MEETING', "Adjourned Meeting"),
        (r'STUDY\s+SESSION', "Study Session"),
        (r'WORKSHOP', "Workshop"),
    ]
    for pat, label in type_patterns:
        if re.search(pat, joined, re.IGNORECASE):
            meta.meeting_type = label
            break

    date_patterns = [
        r'(\w+day,?\s+\w+\s+\d{1,2},?\s+\d{4})',
        r'(\w+\s+\d{1,2},?\s+\d{4})',
        r'(\d{1,2}/\d{1,2}/\d{4})',
    ]
    for pat in date_patterns:
        m = re.search(pat, joined, re.IGNORECASE)
        if m:
            meta.meeting_date = m.group(1).strip()
            break

    for line in lines[:10]:
        if line["is_bold"] and line["font_size"] >= 14:
            meta.title = line["text"].strip()
            break
    if not meta.title:
        meta.title = "Agenda"


def _infer_section(num):
    return "GENERAL"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _has_meaningful_toc(doc):
    toc = doc.get_toc()
    if len(toc) < 2:
        return False
    pages_referenced = set(entry[2] for entry in toc)
    # Meaningful = entries point to pages beyond page 1
    return len(pages_referenced) >= 2 and max(pages_referenced) > 1


def _has_attachment_links(doc):
    for page in doc:
        for link in page.get_links():
            if link.get("kind") == 2 and _is_attachment_url(link.get("uri", "")):
                return True
    return False


# ---------------------------------------------------------------------------
# TOC-based parsing
# ---------------------------------------------------------------------------

def _extract_memo_content(doc, page_start, page_end):
    """Extract structured content from embedded memo pages (0-indexed)."""
    memo = MemoContent(page_start=page_start + 1, page_end=page_end + 1)

    parts = []
    for pi in range(page_start, page_end + 1):
        parts.append(doc[pi].get_text("text"))
    full_text = "\n".join(parts)
    memo.full_text = full_text

    m = re.search(r'SUBJECT:\s*(.+?)(?=\n\s*(?:SUMMARY|$))', full_text, re.DOTALL)
    if m:
        memo.subject = re.sub(r'\s+', ' ', m.group(1)).strip()

    m = re.search(
        r'SUMMARY\s+EXPLANATION\s*(?:&|AND)\s*BACKGROUND:\s*(.+?)(?=\n\s*FISCAL\s+INFORMATION)',
        full_text, re.DOTALL | re.IGNORECASE
    )
    if m:
        memo.summary = re.sub(r'\s+', ' ', m.group(1)).strip()

    m = re.search(r'FISCAL\s+INFORMATION:\s*(.+?)(?=\n\s*RECOMMENDED\s+ACTION)', full_text, re.DOTALL | re.IGNORECASE)
    if m:
        memo.fiscal_info = re.sub(r'\s+', ' ', m.group(1)).strip()

    m = re.search(r'RECOMMENDED\s+ACTION:\s*(.+?)(?=\n\s*Initiated\s+by|$)', full_text, re.DOTALL | re.IGNORECASE)
    if m:
        memo.recommended_action = re.sub(r'\s+', ' ', m.group(1)).strip()

    m = re.search(r'Submitted\s+by:\s*(.+?)(?=\n)', full_text, re.IGNORECASE)
    if m:
        memo.submitted_by = m.group(1).strip()

    return memo


def _text_similarity(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _find_agenda_page_range(doc, toc):
    """Determine which pages are the agenda (vs. attachments). Returns 0-indexed (start, end)."""
    l1_pages = [entry[2] - 1 for entry in toc if entry[0] == 1]
    if not l1_pages:
        return 0, 0

    # For flat TOC: agenda = first L1 page; memos start at the second distinct L1 page
    distinct_sorted = sorted(set(l1_pages))

    if len(distinct_sorted) >= 2:
        # Agenda is the first page, memos start at second distinct page
        return 0, distinct_sorted[1] - 1

    return 0, distinct_sorted[0]


def _detect_toc_pattern(toc):
    """
    Detect whether TOC is hierarchical (L1=items on agenda pages, L2=attachments)
    or flat (L1 entries point to distinct memo pages).
    Returns "hierarchical" or "flat".
    """
    if not toc:
        return "flat"

    # Filter out synthetic entries (Top, Bottom, etc.)
    skip = {'top', 'bottom'}
    l1_pages = [entry[2] for entry in toc if entry[0] == 1 and entry[1].strip().lower() not in skip]
    l2_pages = [entry[2] for entry in toc if entry[0] == 2]

    if not l1_pages:
        return "flat"

    from collections import Counter
    page_counts = Counter(l1_pages)
    distinct_l1_pages = len(page_counts)

    # Find the "agenda page cluster": pages where most L1 entries point.
    # Use the most common pages that account for >70% of L1 entries.
    sorted_pages = page_counts.most_common()
    cluster_pages = set()
    running = 0
    for page, count in sorted_pages:
        cluster_pages.add(page)
        running += count
        if running >= len(l1_pages) * 0.7:
            break
    cluster_max = max(cluster_pages)

    # Hierarchical requires:
    # 1. L2 entries exist
    # 2. L1 entries cluster on a few pages
    # 3. L2 entries point BEYOND the L1 cluster (to deep content)
    deep_l2 = [p for p in l2_pages if p > cluster_max]

    if deep_l2 and distinct_l1_pages <= max(5, len(l1_pages) * 0.3):
        return "hierarchical"

    if distinct_l1_pages > len(l1_pages) * 0.5:
        return "flat"

    return "flat"


def _parse_toc_based(doc, result):
    result.metadata.parse_method = "toc"
    toc = doc.get_toc()
    pattern = _detect_toc_pattern(toc)

    if pattern == "hierarchical":
        _parse_toc_hierarchical(doc, toc, result)
    else:
        _parse_toc_flat(doc, toc, result)


def _parse_toc_hierarchical(doc, toc, result):
    """
    Hierarchical TOC: L1 = agenda items on agenda pages,
    L2 = embedded attachments on content pages.
    """
    result.metadata.parse_method = "toc_hierarchical"

    l2_pages = [entry[2] - 1 for entry in toc if entry[0] == 2]
    agenda_end = min(l2_pages) - 1 if l2_pages else 0

    all_lines = []
    for pi in range(0, agenda_end + 1):
        all_lines.extend(_extract_page_text_with_positions(doc[pi]))
    _extract_meeting_metadata(all_lines, result.metadata)

    skip_titles = {'top', 'bottom'}
    all_l2_starts = sorted(set(entry[2] - 1 for entry in toc if entry[0] == 2))

    # Find "Bottom" page for end-of-doc boundary
    bottom_page = doc.page_count - 1
    for entry in toc:
        if entry[1].strip().lower() == 'bottom':
            bottom_page = entry[2] - 2

    for i, (level, title, page_num) in enumerate(toc):
        if level != 1:
            continue
        if title.strip().lower() in skip_titles:
            continue

        item_num, item_title = _parse_toc_item_title(title)
        if not item_num:
            continue
        assert item_title is not None

        section = _infer_section_from_prefix(item_num)

        item = AgendaItem(
            number=item_num,
            title=item_title,
            section=section,
            body="",
            recommended_action="",
            page_start=page_num,
            page_end=page_num,
        )

        # Collect L2 children
        for j in range(i + 1, len(toc)):
            if toc[j][0] <= 1:
                break
            if toc[j][0] == 2:
                att_title = toc[j][1].strip()
                att_page_start = toc[j][2] - 1  # 0-indexed

                # Determine end page from next L2 entry in global sorted list
                try:
                    idx = all_l2_starts.index(att_page_start)
                    att_page_end = all_l2_starts[idx + 1] - 1 if idx + 1 < len(all_l2_starts) else bottom_page
                except ValueError:
                    att_page_end = bottom_page

                memo = _extract_memo_content(doc, att_page_start, att_page_end)
                memo.subject = memo.subject or att_title

                item.memos.append(memo)
                item.attachments.append(Attachment(
                    label=att_title, url="",
                    page_start=att_page_start + 1,
                    page_end=att_page_end + 1,
                ))

                if memo.recommended_action and not item.recommended_action:
                    item.recommended_action = memo.recommended_action

        item.body = _extract_item_body_from_agenda(doc, item_num, 0, agenda_end)

        result.items.append(item)
        if section and section not in result.sections:
            result.sections.append(section)


def _parse_toc_flat(doc, toc, result):
    """Flat TOC: L1 entries point to distinct pages (agenda + memos/items)."""
    result.metadata.parse_method = "toc_flat"

    agenda_start, agenda_end = _find_agenda_page_range(doc, toc)

    all_lines = []
    all_links = []
    for pi in range(agenda_start, agenda_end + 1):
        page = doc[pi]
        all_lines.extend(_extract_page_text_with_positions(page))
        all_links.extend(_extract_links(page))

    _extract_meeting_metadata(all_lines, result.metadata)
    items, item_boundaries = _parse_agenda_items(all_lines, all_links, result)

    # Collect TOC entries beyond agenda pages
    skip_titles = {'top', 'bottom'}
    toc_entries_beyond = []
    for i, (level, title, page_num) in enumerate(toc):
        page_0 = page_num - 1
        if page_0 < 0 or page_0 <= agenda_end:
            continue
        if title.strip().lower() in skip_titles:
            continue
        toc_entries_beyond.append((i, level, title, page_num))

    if items:
        # Text parsing succeeded: match TOC entries as memos
        memos = []
        for (idx, level, title, page_num) in toc_entries_beyond:
            page_0 = page_num - 1
            # Find end page
            if idx + 1 < len(toc):
                next_p = toc[idx + 1][2] - 2
            else:
                next_p = doc.page_count - 1
            end_page = max(page_0, next_p)

            memo = _extract_memo_content(doc, page_0, end_page)
            memos.append(memo)

        # Match memos to items by text similarity
        for memo in memos:
            best_score = 0.0
            best_item = None
            for item in items:
                item_text = (item.title + " " + item.body).lower()
                score = _text_similarity(memo.subject, item.title)
                body_score = _text_similarity(memo.subject, item.body[:300])
                score = max(score, body_score)
                subject_words = set(w for w in re.findall(r'[a-z]{4,}', memo.subject.lower()))
                stopwords = {'with', 'from', 'that', 'this', 'will', 'have', 'been', 'their',
                             'they', 'city', 'shall', 'agreement', 'would', 'upon', 'which', 'between'}
                subject_words -= stopwords
                if subject_words:
                    overlap = sum(1 for w in subject_words if w in item_text) / len(subject_words)
                    score = max(score, overlap)
                if score > best_score:
                    best_score = score
                    best_item = item
            if best_item and best_score > 0.25:
                best_item.memos.append(memo)
                if memo.recommended_action and not best_item.recommended_action:
                    best_item.recommended_action = memo.recommended_action
            else:
                result.orphan_memos.append(memo)

        _assign_links_to_items(all_links, items, item_boundaries, all_lines, result)
        result.items = items
    else:
        # Text parsing failed: build items directly from TOC L1 entries
        _build_items_from_toc_entries(doc, toc, toc_entries_beyond, agenda_end, result)


def _build_items_from_toc_entries(doc, toc, toc_entries_beyond, agenda_end, result):
    """
    Build items directly from TOC entries when text parsing yields nothing.
    Each L1 entry beyond the agenda becomes an item.
    Sub-entries (L2+) become attachments within their parent item.
    """
    # Collect all valid page starts for range calculation (skip page -1 and agenda pages)
    all_content_starts = sorted(set(
        entry[2] - 1 for entry in toc
        if entry[2] - 1 > agenda_end and entry[2] > 0
    ))

    # Build L1 items with sub-entry attachments
    for (idx, level, title, page_num) in toc_entries_beyond:
        if level != 1:
            continue

        page_0 = page_num - 1
        item_num, item_title = _parse_flat_toc_title(title)
        if item_num is None:
            item_num = ""
            item_title = title.strip()
        assert item_title is not None

        # Find this item's page range: from its page to the next L1's page - 1
        next_l1_page = None
        for j in range(idx + 1, len(toc)):
            if toc[j][0] == 1 and toc[j][2] - 1 > page_0:
                next_l1_page = toc[j][2] - 1
                break
        if next_l1_page:
            item_end_page = next_l1_page - 1
        else:
            item_end_page = doc.page_count - 1

        # Extract memo content from the item's full page range
        memo = _extract_memo_content(doc, page_0, item_end_page)
        memo.subject = memo.subject or item_title

        item = AgendaItem(
            number=item_num,
            title=item_title,
            section=_infer_section_from_toc_number(item_num),
            body=memo.summary or "",
            recommended_action=memo.recommended_action,
            page_start=page_num,
            page_end=item_end_page + 1,
        )
        item.memos.append(memo)

        # Collect sub-entries (L2, L3, L4) as additional attachments
        for j in range(idx + 1, len(toc)):
            if toc[j][0] <= 1:
                break
            sub_title = toc[j][1].strip()
            sub_page = toc[j][2] - 1
            if sub_page < 0 or sub_page <= agenda_end:
                continue  # bogus page reference

            # Compute sub-entry end page
            try:
                si = all_content_starts.index(sub_page)
                sub_end = all_content_starts[si + 1] - 1 if si + 1 < len(all_content_starts) else item_end_page
            except ValueError:
                sub_end = item_end_page

            sub_end = min(sub_end, item_end_page)

            item.attachments.append(Attachment(
                label=sub_title, url="",
                page_start=sub_page + 1,
                page_end=sub_end + 1,
            ))

        result.items.append(item)

        section = item.section
        if section and section not in result.sections:
            result.sections.append(section)


def _parse_flat_toc_title(title):
    """
    Parse item number and title from flat TOC entries.
    Handles: "01a Claims and Payroll", "03 AB Cross Connection Control",
    "00 AB SeeClickFix", "99 Agenda Forecast", etc.
    Returns (item_number, clean_title) or (None, None).
    """
    title = title.strip()

    # Pattern: "01a Title" or "03 AB Title" or "06 AB Title"
    m = re.match(r'^(\d{1,3}[a-z]?)\s+(?:AB\s+)?(.+)', title, re.IGNORECASE)
    if m:
        num = m.group(1)
        rest = m.group(2).strip()
        return num, rest

    # Pattern: tab-separated (already handled by _parse_toc_item_title for letter.number)
    parts = title.split('\t', 1)
    if len(parts) == 2 and re.match(r'^[A-Z]\.\d+$', parts[0].strip()):
        return parts[0].strip(), parts[1].strip()

    return None, None


def _infer_section_from_toc_number(item_num):
    """Infer section from numeric TOC item number like '01a', '03', '99'."""
    if not item_num:
        return "GENERAL"
    # Strip trailing letter
    num_only = re.match(r'^(\d+)', item_num)
    if not num_only:
        # Try letter-dot-number (Cedar Park style)
        return _infer_section_from_prefix(item_num)
    n = int(num_only.group(1))
    if n == 0:
        return "PRESENTATIONS"
    elif n == 1 or item_num.startswith('01'):
        return "CONSENT AGENDA"
    elif n >= 90:
        return "INFORMATION"
    else:
        return "GENERAL"


def _parse_toc_item_title(toc_title):
    """Parse item number and title from TOC entry like 'D.4\t03-26-2026 Type B...'"""
    parts = toc_title.split('\t', 1)
    if len(parts) == 2:
        num_part = parts[0].strip()
        title_part = parts[1].strip()
    else:
        m = re.match(r'^([A-Z]\.\d+)\s+(.*)', toc_title.strip())
        if m:
            num_part = m.group(1)
            title_part = m.group(2).strip()
        else:
            return None, None

    if not re.match(r'^[A-Za-z]\.\d+$', num_part):
        return None, None

    # Strip date prefixes
    title_part = re.sub(r'^[\d]{2,4}[-./][\d]{2}[-./][\d]{2,4}\s*:?\s*', '', title_part)
    title_part = re.sub(r'^[\d]{2}/[\d]{2}/[\d]{2,4}\s*[-\u2013\u2014]+\s*', '', title_part)

    return num_part, title_part.strip() if title_part.strip() else toc_title.strip()


def _infer_section_from_prefix(item_num):
    """Infer section from letter prefix."""
    letter = item_num[0].upper() if item_num else ""
    section_map = {
        'A': 'CALL TO ORDER', 'B': 'EXECUTIVE SESSION',
        'C': 'PRESENTATIONS', 'D': 'CONSENT AGENDA',
        'E': 'PUBLIC HEARINGS', 'F': 'RESOLUTIONS',
        'G': 'ORDINANCES', 'H': 'DISCUSSION/ACTION',
    }
    return section_map.get(letter, "GENERAL")


def _extract_item_body_from_agenda(doc, item_num, agenda_start, agenda_end):
    """Extract body text for an item from the agenda pages."""
    for pi in range(agenda_start, agenda_end + 1):
        text = doc[pi].get_text("text")
        lines = text.split("\n")
        capturing = False
        body_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(item_num):
                capturing = True
                after = stripped[len(item_num):].strip()
                if after:
                    body_lines.append(after)
                continue
            if capturing:
                if re.match(r'^[A-Z]\.\d+\s', stripped):
                    break
                if re.match(r'^(Page \d|City Council Agenda|An unscheduled)', stripped):
                    break
                if stripped:
                    body_lines.append(stripped)
        if body_lines:
            return "\n".join(body_lines).strip()
    return ""



# ---------------------------------------------------------------------------
# URL-based parsing
# ---------------------------------------------------------------------------

def _parse_url_based(doc, result):
    result.metadata.parse_method = "url"

    all_lines = []
    all_links = []
    for page in doc:
        all_lines.extend(_extract_page_text_with_positions(page))
        all_links.extend(_extract_links(page))

    _extract_meeting_metadata(all_lines, result.metadata)
    items, item_boundaries = _parse_agenda_items(all_lines, all_links, result)
    _assign_links_to_items(all_links, items, item_boundaries, all_lines, result)
    result.items = items


# ---------------------------------------------------------------------------
# Shared item parsing
# ---------------------------------------------------------------------------

def _parse_agenda_items(all_lines, all_links, result):
    current_section = ""
    items = []
    item_boundaries = []
    skip_until = -1

    for i, line in enumerate(all_lines):
        if i <= skip_until:
            continue
        text = line["text"]

        # Try item detection first for numbered lines to prevent
        # section regex from eating items like "4. PUBLIC HEARING"
        has_num = bool(re.match(r'^\s*(?:\d{1,2}\.|[A-Z]\.)', text))

        if has_num:
            is_item, num, title_text, lines_consumed = _is_likely_item_header(
                line, lines_context=(i, all_lines, current_section)
            )
            if is_item and num and title_text:
                full_title = title_text
                title_end_index = i + lines_consumed

                if lines_consumed == 0:
                    for j in range(i + 1, min(i + 4, len(all_lines))):
                        next_line = all_lines[j]
                        nt = next_line["text"].strip()
                        if _match_item_number(nt)[0] is not None:
                            break
                        if _is_section_header(nt) and next_line["is_bold"]:
                            break
                        if REC_ACTION_RE.search(nt):
                            break
                        if _looks_like_attachment_label(nt, all_links, next_line["page"], next_line["y0"]):
                            break
                        if (next_line["is_bold"] == line["is_bold"]
                            and abs(next_line["font_size"] - line["font_size"]) < 0.5
                            and _is_mostly_upper(nt)):
                            full_title += " " + nt
                            title_end_index = j
                        else:
                            break

                skip_until = title_end_index
                section = current_section or _infer_section(num)
                item = AgendaItem(
                    number=num,
                    title=full_title.rstrip(':').strip(),
                    section=section,
                    body="",
                    recommended_action="",
                    page_start=line["page"] + 1,
                )
                items.append(item)
                item_boundaries.append({
                    "index": len(items) - 1,
                    "page": line["page"],
                    "y0": line["y0"],
                    "line_index": title_end_index,
                })
                continue

        # Section header (only if not an item)
        if _is_section_header(text) and (line["is_bold"] or _is_mostly_upper(text)):
            section_name = _extract_section_name(text)
            current_section = section_name
            if section_name not in result.sections:
                result.sections.append(section_name)
            continue

    # Collect body text
    for bi in range(len(item_boundaries)):
        start_li = item_boundaries[bi]["line_index"] + 1
        end_li = item_boundaries[bi + 1]["line_index"] if bi + 1 < len(item_boundaries) else len(all_lines)

        body_parts = []
        rec_action_parts = []
        in_rec_action = False

        for li in range(start_li, end_li):
            lt = all_lines[li]["text"]
            if _is_section_header(lt) and (all_lines[li]["is_bold"] or _is_mostly_upper(lt)):
                break
            if REC_ACTION_RE.search(lt):
                in_rec_action = True
                after = REC_ACTION_RE.split(lt, 1)[-1].strip()
                if after:
                    rec_action_parts.append(after)
                body_parts.append(lt)
                continue
            if in_rec_action:
                if _looks_like_attachment_label(lt, all_links, all_lines[li]["page"], all_lines[li]["y0"]):
                    in_rec_action = False
                else:
                    rec_action_parts.append(lt)
            body_parts.append(lt)
            items[bi].page_end = all_lines[li]["page"] + 1

        items[bi].body = "\n".join(body_parts).strip()
        items[bi].recommended_action = " ".join(rec_action_parts).strip()
        if not items[bi].page_end:
            items[bi].page_end = items[bi].page_start

    return items, item_boundaries


def _assign_links_to_items(all_links, items, item_boundaries, all_lines, result):
    for link in all_links:
        if not _is_attachment_url(link["url"]):
            continue
        best = _find_owning_item(link, item_boundaries)
        if best is not None:
            items[best].attachments.append(Attachment(
                label=link["label"], url=link["url"],
                page_start=link["page"] + 1, page_end=link["page"] + 1,
                bbox=link["bbox"],
            ))
        else:
            result.orphan_links.append(Attachment(
                label=link["label"], url=link["url"],
                page_start=link["page"] + 1, page_end=link["page"] + 1,
                bbox=link["bbox"],
            ))


def _find_owning_item(link, item_boundaries):
    link_page = link["page"]
    link_y = link["y_center"]
    best = None
    for bi in range(len(item_boundaries)):
        bp = item_boundaries[bi]["page"]
        by = item_boundaries[bi]["y0"]
        if bp < link_page:
            best = bi
        elif bp == link_page and by <= link_y:
            best = bi
    if best is not None and best + 1 < len(item_boundaries):
        nbp = item_boundaries[best + 1]["page"]
        nby = item_boundaries[best + 1]["y0"]
        if link_page > nbp or (link_page == nbp and link_y >= nby):
            return _find_owning_item_strict(link, item_boundaries)
    return best


def _find_owning_item_strict(link, item_boundaries):
    lp = link["page"]
    ly = link["y_center"]
    for bi in range(len(item_boundaries)):
        bp = item_boundaries[bi]["page"]
        by = item_boundaries[bi]["y0"]
        ep = item_boundaries[bi + 1]["page"] if bi + 1 < len(item_boundaries) else lp + 1
        ey = item_boundaries[bi + 1]["y0"] if bi + 1 < len(item_boundaries) else 9999
        if ((lp > bp) or (lp == bp and ly >= by)) and ((lp < ep) or (lp == ep and ly < ey)):
            return bi
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_agenda(
    pdf_path: str, force_method: str | None = None
) -> ParsedAgenda:
    doc = fitz.open(pdf_path)
    result = ParsedAgenda()

    meta = doc.metadata or {}
    result.metadata.page_count = doc.page_count
    result.metadata.pdf_metadata = {k: v for k, v in meta.items() if v}

    if force_method == "toc":
        _parse_toc_based(doc, result)
    elif force_method == "url":
        _parse_url_based(doc, result)
    elif _has_meaningful_toc(doc):
        _parse_toc_based(doc, result)
    else:
        _parse_url_based(doc, result)

    doc.close()
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python agenda_chunker.py <pdf> [--json] [--items-only] [--force-toc] [--force-url]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    as_json = "--json" in sys.argv
    items_only = "--items-only" in sys.argv
    force = None
    if "--force-toc" in sys.argv:
        force = "toc"
    elif "--force-url" in sys.argv:
        force = "url"

    result = parse_agenda(pdf_path, force_method=force)

    if as_json:
        if items_only:
            print(json.dumps([asdict(item) for item in result.items], indent=2, ensure_ascii=False))
        else:
            print(result.to_json())
    else:
        m = result.metadata
        print(f"{'=' * 60}")
        print(f"{m.title}")
        print(f"{m.body_name} | {m.meeting_type} | {m.meeting_date}")
        print(f"{m.page_count} pages | method: {m.parse_method}")
        print(f"{'=' * 60}")
        print(f"\nSections: {', '.join(result.sections)}")
        print(f"Items: {len(result.items)}")
        print(f"Orphan links: {len(result.orphan_links)}")
        print(f"Orphan memos: {len(result.orphan_memos)}")

        for item in result.items:
            print(f"\n{'=' * 60}")
            print(f"[{item.section}] {item.number}  {item.title}")
            print(f"  Pages: {item.page_start}-{item.page_end}")
            if item.recommended_action:
                ra = item.recommended_action[:120]
                if len(item.recommended_action) > 120:
                    ra += "..."
                print(f"  Rec. Action: {ra}")
            if item.attachments:
                print(f"  Attachments ({len(item.attachments)}):")
                for att in item.attachments:
                    print(f"    - {att.label}")
                    print(f"      {att.url}")
            if item.memos:
                print(f"  Embedded Memos ({len(item.memos)}):")
                for memo in item.memos:
                    subj = memo.subject[:80] if memo.subject else "(no subject)"
                    print(f"    - p{memo.page_start}-{memo.page_end}: {subj}")
                    if memo.submitted_by:
                        print(f"      By: {memo.submitted_by}")
                    if memo.recommended_action:
                        print(f"      Rec: {memo.recommended_action[:100]}...")
            if not item.attachments and not item.memos:
                print("  Attachments: none")

        if result.orphan_links:
            print(f"\n{'=' * 60}")
            print("ORPHAN LINKS:")
            for att in result.orphan_links:
                print(f"  p{att.page_start}: {att.label} -> {att.url}")

        if result.orphan_memos:
            print(f"\n{'=' * 60}")
            print("ORPHAN MEMOS:")
            for memo in result.orphan_memos:
                print(f"  p{memo.page_start}-{memo.page_end}: {memo.subject[:80]}")
