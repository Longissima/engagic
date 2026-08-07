"""
Unified chunker for municipal agenda PDFs across vendors
(Granicus, Legistar, CivicPlus, CivicClerk, etc.)

Two extraction paths:
  1. TOC-based: When PDF has a bookmark/outline tree with embedded memos
     as subsequent pages. Chunks by page ranges, extracts memo content.
     Items get body_text from embedded memo full_text.
  2. URL-based: When agenda items have hyperlinked attachment URLs.
     Extracts items and assigns attachment links by position.

Dispatch: If TOC exists with multi-page structure, use TOC path.
Otherwise use URL path. Both paths share the same item detection logic.

Dependencies: PyMuPDF (fitz)

Returns pipeline-compatible dicts matching AgendaItemSchema / AttachmentSchema.

Usage:
    from vendors.adapters.parsers.agenda_chunker import parse_agenda_pdf
    result = parse_agenda_pdf("path/to/agenda.pdf")
    items = result["items"]  # List[dict] ready for pipeline
"""

import fitz
import re
import json
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, Any, List, Optional

from config import get_logger
from vendors.utils.attachments import classify_attachment_type as _attachment_type

logger = get_logger(__name__).bind(component="vendor")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class _Attachment:
    label: str
    url: str             # URL for link-based; empty for embedded
    page_start: int      # 1-indexed
    page_end: int        # 1-indexed
    bbox: list = field(default_factory=list)

@dataclass
class _MemoContent:
    """Extracted content from an embedded staff memo page range."""
    subject: str = ""
    summary: str = ""
    fiscal_info: str = ""
    recommended_action: str = ""
    submitted_by: str = ""
    full_text: str = ""
    page_start: int = 0   # 1-indexed
    page_end: int = 0     # 1-indexed

@dataclass
class _AgendaItem:
    number: str                             # "4.3", "6.1", "1", "A", "D.4", etc.
    title: str
    section: str                            # Parent section: "CONSENT CALENDAR", etc.
    body: str                               # Coversheet/agenda text
    recommended_action: str
    attachments: list = field(default_factory=list)   # List[_Attachment]
    memos: list = field(default_factory=list)          # List[_MemoContent]
    page_start: int = 0                     # 1-indexed
    page_end: int = 0

@dataclass
class _AgendaMetadata:
    title: str = ""
    body_name: str = ""                     # "City Council", "Planning Commission"
    meeting_date: str = ""
    meeting_type: str = ""                  # "Regular Meeting", "Special Meeting"
    page_count: int = 0
    parse_method: str = ""                  # "toc_hierarchical", "toc_flat", "url"
    pdf_metadata: dict = field(default_factory=dict)

@dataclass
class _ParsedAgenda:
    metadata: _AgendaMetadata = field(default_factory=_AgendaMetadata)
    sections: list = field(default_factory=list)
    items: list = field(default_factory=list)           # List[_AgendaItem]
    orphan_links: list = field(default_factory=list)    # List[_Attachment]
    orphan_memos: list = field(default_factory=list)    # List[_MemoContent]


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

ITEM_NUM_RE = re.compile(
    r'^[\s]*'
    r'('
    r'\d{4}-\d{1,4}'                # 2026-68 (resolution/ordinance numbers)
    r'|\d{1,3}(?:\.\d{1,2}){1,3}'   # 4.3, 6.1.2, 1.2.3.4, 300.1
    r'|\d{1,3}\.[a-z]'              # 2.a, 3.b, 300.a (Legistar sub-items)
    r'|[A-Z]\.\d{1,2}'              # H.1, F.1 (CivicPlus letter-dot-digit sub-items)
    r'|\(\d{1,3}\)'                  # (1) (2) (100) (parenthesized numbers)
    r'|\([a-z]\)'                    # (a) (b) (c) (parenthesized letters)
    r'|\d{1,3}\.'                    # 1. 2. 300. (Winter Springs FL uses
                                    # section-prefixed 3-digit numbers: 300,
                                    # 400, 500; Granicus PDFs render them as
                                    # item-header tokens)
    r'|[A-Z]\.'                      # A. B. C.
    r'|[a-z]\.'                      # a. b. c.
    r'|[IVXLC]+\.'                   # I. II. IV.
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
    r'PUBLIC\s+PARTICIPATION',
    r'(?:REGULAR\s+)?DISCUSSION(?:[/\s]+(?:VOTES?|ACTION|ITEMS?))?',
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
    # CivicPlus / Planning Commission patterns
    r'THESE\s+ITEMS\s+(?:WILL\s+)?REQUIRE',
    r'CONSENT\s*[-\u2013\u2014]\s*ITEMS\s+FOR\s+\w+',
    r'APPROVAL\s+OF\s+MINUTES',
    r'RULES\s+FOR\s+CONDUCTING',
    r'REGULAR\s+AGENDA',
    r'AGENDA\s+ITEMS?',
    r'TABLED\s+ITEMS?',
    r'COMMUNICATIONS?$',
    r'DIRECTOR.{0,5}S?\s+COMMENTS?',
    r'COMMISSIONERS?.{0,5}\s+COMMENTS?',
    r'ADJOURN$',
    r'RESOLUTIONS?$',
    r'AGREEMENTS?$',
    r'AUDITS?$',
    r'ORDINANCES?$',
    # Bare "AGENDA" as the only header on thin civicweb printPdf renders
    # (Bee Cave TX): no "CONSENT AGENDA" / "REGULAR AGENDA" precedes items,
    # so without this the standalone-number lookahead path never activates.
    r'AGENDA$',
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
    r'/showdocument\?',          # sanjoseca.gov, other .gov CMS hosting
    r'/DocumentCenter/View/',
    r'cloudfront\.net',
    r'\.sharepoint\.com/',
    # CivicWeb / Diligent One Platform — document URLs are
    # /document/{uuid-or-int} with no file extension; UUID form is
    # the per-item attachment link, integer form is the packet.
    r'diligentoneplatform\.com/document/',
    r'\.civicweb\.net/document/',
]
ATTACHMENT_URL_RE = re.compile('|'.join(ATTACHMENT_URL_PATTERNS), re.IGNORECASE)

# Matter file patterns: "2024-001", "BL2025-1005", "RS2024-12"
MATTER_FILE_RE = re.compile(
    r'(?:File\s+(?:ID|No\.?|Number|#)\s*:?\s*)'
    r'([A-Z]{0,3}\d{4}-\d{2,6})'
)

MATTER_FILE_STANDALONE_RE = re.compile(
    r'\b([A-Z]{1,3}\d{4}-\d{2,6})\b'
    r'|\bFile\s+ID:\s*(\S+)'
)


# ---------------------------------------------------------------------------
# Shared text extraction
# ---------------------------------------------------------------------------

def _extract_page_text_with_positions(page):
    """Extract text as line-level entries with font metadata."""
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


_HTTPS_ONLY_HOSTS = (
    "community.diligentoneplatform.com",
    "community.highbond.com",
    ".civicweb.net",
)


def _normalize_link_url(uri: str) -> str:
    """Upgrade http:// → https:// for hosts that drop plaintext.

    CivicWeb / Diligent One Platform PDFs embed http:// links, but the
    servers ignore plaintext connections (15s+ hangs with no response).
    The stored link works fine in browsers via redirect, but our aiohttp
    fetches in `_resolve_sub_attachments` time out one by one.
    """
    if uri.startswith("http://") and any(h in uri for h in _HTTPS_ONLY_HOSTS):
        return "https://" + uri[len("http://"):]
    return uri


def _extract_links(page):
    """Extract all URI links from a page."""
    links = []
    for link in page.get_links():
        if link.get("kind") != 2:
            continue
        uri = link.get("uri", "")
        if not uri:
            continue
        uri = _normalize_link_url(uri)
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
    """Extract display text for a link by intersecting span bboxes."""
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
    """
    Heuristic: a line is an item header if it has an item number AND
    the remainder looks like an agenda item title (not procedural text).

    Returns (is_item, number, title_text, lines_consumed).
    """
    num, remainder = _match_item_number(line["text"])
    if num is None:
        return False, None, None, 0

    # Sub-items (4.1, 6.1, 2.a) and parenthesized items ((a), (1)) are
    # almost always agenda items -- accept with minimal filtering.
    is_sub_item = '.' in num or (num.startswith('(') and num.endswith(')'))
    if is_sub_item:
        if remainder:
            return True, num, remainder, 0
        # Standalone sub-item number (e.g. "2.a" or "(a)" on its own line) — look ahead for title
        if lines_context is not None:
            line_idx, all_lines, _ = lines_context
            for j in range(line_idx + 1, min(line_idx + 3, len(all_lines))):
                next_text = all_lines[j]["text"].strip()
                if not next_text:
                    continue
                if _match_item_number(next_text)[0] is not None:
                    break
                if _is_section_header(next_text):
                    break
                return True, num, next_text, j - line_idx
        return False, None, None, 0

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

    # Standalone number line — look ahead for title.
    # We used to gate this on current_section being a recognized agenda
    # section header ("CONSENT AGENDA", "REGULAR AGENDA", etc.), but thin
    # civicweb printPdf agendas (Bee Cave TX) render without any section
    # banner — just "AGENDA" on line 0 and bare numbered items after. The
    # caller already scopes us to the first _MAX_PAGES_FOR_TEXT_ITEMS pages
    # and the lookahead below is self-limiting (stops at next number, stops
    # at bold/upper section header, caps at 2 title lines), so body-text
    # false positives remain bounded without the gate.
    if lines_context is not None:
        line_idx, all_lines, _ = lines_context

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


def _extract_matter_file(title: str, body: str) -> Optional[str]:
    """Try to extract a matter file number from title or body text."""
    for text in [title, body[:500] if body else ""]:
        m = MATTER_FILE_RE.search(text)
        if m:
            return m.group(1)
    m = MATTER_FILE_STANDALONE_RE.search(title)
    if m:
        return m.group(1) or m.group(2)
    return None


def _text_similarity(a, b):
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _extract_meeting_metadata(lines, meta):
    """Extract meeting metadata from the first ~30 lines."""
    first_lines = [line["text"] for line in lines[:30]]
    joined = "\n".join(first_lines)

    body_patterns = [
        r'(CITY\s+COUNCIL)',
        r'(CITY\s+COMMISSION)',
        r'(PLANNING\s+(?:AND\s+ZONING\s+)?COMMISSION)',
        r'(BOARD\s+OF\s+SUPERVISORS)',
        r'(TOWN\s+COUNCIL)',
        r'(VILLAGE\s+BOARD)',
        r'(BOARD\s+OF\s+(?:DIRECTORS|TRUSTEES))',
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
        (r'EMERGENCY\s+MEETING', "Emergency Meeting"),
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


def _infer_section(item_number):
    return "GENERAL"


def _infer_section_from_prefix(item_num):
    """Infer section from letter prefix (e.g. D.4 -> CONSENT AGENDA)."""
    letter = item_num[0].upper() if item_num else ""
    section_map = {
        'A': 'CALL TO ORDER', 'B': 'EXECUTIVE SESSION',
        'C': 'PRESENTATIONS', 'D': 'CONSENT AGENDA',
        'E': 'PUBLIC HEARINGS', 'F': 'RESOLUTIONS',
        'G': 'ORDINANCES', 'H': 'DISCUSSION/ACTION',
    }
    return section_map.get(letter, "GENERAL")


def _parse_flat_toc_title(title):
    """Parse item number and title from flat TOC entries.

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


# ---------------------------------------------------------------------------
# TOC dispatch and detection
# ---------------------------------------------------------------------------

def _has_meaningful_toc(doc):
    toc = doc.get_toc()
    if len(toc) < 2:
        return False
    pages_referenced = set(entry[2] for entry in toc)
    return len(pages_referenced) >= 2 and max(pages_referenced) > 1


def _has_structural_toc(doc):
    """Stricter TOC check for large documents.

    A packet TOC has dense item-level bookmarks (Legistar, CivicClerk).
    Thin agendas sometimes carry 2-3 navigation bookmarks that pass
    _has_meaningful_toc but contain no structural information.  Require
    at least 5 entries so we don't mistake navigation for structure.
    """
    toc = doc.get_toc()
    if len(toc) < 5:
        return False
    pages_referenced = set(entry[2] for entry in toc)
    return len(pages_referenced) >= 3 and max(pages_referenced) > 1


def _has_attachment_links(doc):
    for page in doc:
        for link in page.get_links():
            if link.get("kind") == 2 and _is_attachment_url(link.get("uri", "")):
                return True
    return False


def _detect_toc_pattern(toc):
    """
    Detect whether TOC is hierarchical (L1=items on agenda pages, L2=attachments)
    or flat (L1 entries point to distinct memo pages).
    """
    if not toc:
        return "flat"

    skip = {'top', 'bottom'}
    l1_pages = [entry[2] for entry in toc if entry[0] == 1 and entry[1].strip().lower() not in skip]
    l2_pages = [entry[2] for entry in toc if entry[0] == 2]

    if not l1_pages:
        return "flat"

    page_counts = Counter(l1_pages)
    distinct_l1_pages = len(page_counts)

    # Find the "agenda page cluster": pages where most L1 entries point.
    sorted_pages = page_counts.most_common()
    cluster_pages = set()
    running = 0
    for page, count in sorted_pages:
        cluster_pages.add(page)
        running += count
        if running >= len(l1_pages) * 0.7:
            break
    cluster_max = max(cluster_pages)

    # Document bundle: L1=sections (Agenda/Attachments), L2=embedded document
    # filenames with virtual pages (page <= 0), L3+=content within documents.
    # CivicClerk packet pattern.  Must check before deep_hierarchical since
    # both share few-L1 + many-L3 characteristics.
    l2_entries = [e for e in toc if e[0] == 2]
    l3_entries = [e for e in toc if e[0] == 3]
    l4_entries = [e for e in toc if e[0] == 4]
    l2_virtual = [e for e in l2_entries if e[2] <= 0]
    if len(l1_pages) <= 3 and l2_virtual and len(l3_entries) >= 2:
        return "document_bundle"

    # Deep hierarchical: L1 is a root wrapper (1-2 entries), real items at L3+.
    # Escribe packet pattern: L1=root, L2=sections, L3=items, L4=attachments.
    if len(l1_pages) <= 2 and len(l3_entries) >= 3 and len(l4_entries) >= 1:
        return "deep_hierarchical"

    # Hierarchical: L2 entries exist and point well beyond the L1 cluster.
    # Require at least 2 pages of separation — a single page beyond the
    # cluster is typically still agenda, not an embedded memo section.
    # Legistar-generated agendas have navigation TOCs on agenda pages that
    # look hierarchical but aren't (all entries on pages 1-2).
    deep_l2 = [p for p in l2_pages if p > cluster_max + 1]
    if deep_l2 and distinct_l1_pages <= max(5, len(l1_pages) * 0.3):
        return "hierarchical"

    if distinct_l1_pages > len(l1_pages) * 0.5:
        return "flat"

    return "flat"


def _find_agenda_page_range(doc, toc):
    """Determine which pages are the agenda (vs. attachments). Returns 0-indexed (start, end)."""
    l1_pages = [entry[2] - 1 for entry in toc if entry[0] == 1]
    if not l1_pages:
        return 0, 0

    distinct_sorted = sorted(set(l1_pages))
    if len(distinct_sorted) >= 2:
        return 0, distinct_sorted[1] - 1
    return 0, distinct_sorted[0]


# ---------------------------------------------------------------------------
# TOC-based parsing
# ---------------------------------------------------------------------------

def _extract_memo_content(doc, page_start, page_end):
    """Extract structured content from embedded memo pages (0-indexed input)."""
    memo = _MemoContent(page_start=page_start + 1, page_end=page_end + 1)

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


def _parse_toc_item_title(toc_title):
    """Parse item number and title from TOC entry like 'D.4\\t03-26-2026 Type B...'"""
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


def _parse_toc_based(doc, result):
    toc = doc.get_toc()
    pattern = _detect_toc_pattern(toc)

    if pattern == "document_bundle":
        _parse_toc_document_bundle(doc, toc, result)
    elif pattern == "deep_hierarchical":
        _parse_toc_deep_hierarchical(doc, toc, result)
    elif pattern == "hierarchical":
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
        if not item_num or not item_title:
            continue

        section = _infer_section_from_prefix(item_num)

        item = _AgendaItem(
            number=item_num,
            title=item_title,
            section=section,
            body="",
            recommended_action="",
            page_start=page_num,
            page_end=page_num,
        )

        # Collect L2 children (embedded memos)
        for j in range(i + 1, len(toc)):
            if toc[j][0] <= 1:
                break
            if toc[j][0] == 2:
                att_title = toc[j][1].strip()
                att_page_start = toc[j][2] - 1  # 0-indexed

                try:
                    idx = all_l2_starts.index(att_page_start)
                    att_page_end = all_l2_starts[idx + 1] - 1 if idx + 1 < len(all_l2_starts) else bottom_page
                except ValueError:
                    att_page_end = bottom_page

                memo = _extract_memo_content(doc, att_page_start, att_page_end)
                memo.subject = memo.subject or att_title

                item.memos.append(memo)
                item.attachments.append(_Attachment(
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

    logger.debug("parsed toc hierarchical",
                 item_count=len(result.items),
                 total_memos=sum(len(it.memos) for it in result.items))


def _document_bundle_match_score(item_text, doc_title):
    """Score how well an embedded document title matches an agenda item.

    Returns a float 0-1.  Higher means better match.  Uses overlapping
    content words and date fragments so it generalises across vendors
    without hard-coding any particular naming convention.
    """
    # Normalise: lowercase, strip extensions, collapse whitespace
    a = re.sub(r'\.\w{2,4}$', '', item_text.lower())
    b = re.sub(r'\.\w{2,4}$', '', doc_title.lower())
    a = re.sub(r'[^a-z0-9 ]', ' ', a)
    b = re.sub(r'[^a-z0-9 ]', ' ', b)

    stop = {'the', 'of', 'and', 'for', 'a', 'an', 'in', 'on', 'to',
            'min', 'attachment', 'from', 'with', 'review', 'materials',
            'discussion', 'presentation', 'presentations'}
    a_words = {w for w in a.split() if w not in stop and len(w) > 1}
    b_words = {w for w in b.split() if w not in stop and len(w) > 1}

    if not a_words or not b_words:
        return 0.0

    overlap = a_words & b_words
    # Jaccard-ish: reward overlap relative to the smaller set
    return len(overlap) / min(len(a_words), len(b_words))


def _parse_toc_document_bundle(doc, toc, result):
    """Document bundle TOC: L1=sections, L2=embedded filenames, L3+=pages.

    Packet PDFs where the TOC groups content by document rather than by
    agenda item.  The first L1 entry covers the agenda text (typically
    1-3 pages with numbered items).  Subsequent L1 entries mark sections
    of embedded attachment documents whose L2 entries are virtual
    (page <= 0) filename bookmarks with L3+ descendants spanning the
    actual content pages.

    Strategy:
      1. Parse items from the agenda pages using the shared item parser.
      2. Collect L2 attachment documents and extract body text from their
         page ranges via _extract_memo_content.
      3. Match attachments to items by content similarity.  Unmatched
         documents are stored as orphan memos.
    """
    result.metadata.parse_method = "toc_document_bundle"

    # ---- 1. Find agenda pages ------------------------------------------------
    # Use only the first L1 section.  Later L1 sections (e.g. "Attachments")
    # may contain loose documents (minutes, notices) with their own numbered
    # items that would confuse the parser.
    l1_entries = [(e[1], e[2] - 1) for e in toc if e[0] == 1 and e[2] > 0]
    first_l1_page = l1_entries[0][1] if l1_entries else 0

    if len(l1_entries) >= 2:
        agenda_end = l1_entries[1][1] - 1
    else:
        # Single L1 — stop before the first L3+ page
        l3_plus_pages = sorted(
            e[2] - 1 for e in toc if e[0] >= 3 and e[2] > 0
        )
        agenda_end = (l3_plus_pages[0] - 1) if l3_plus_pages else first_l1_page

    agenda_end = max(agenda_end, first_l1_page)

    all_lines = []
    all_links = []
    for pi in range(first_l1_page, min(agenda_end + 1, doc.page_count)):
        page = doc[pi]
        all_lines.extend(_extract_page_text_with_positions(page))
        all_links.extend(_extract_links(page))

    _extract_meeting_metadata(all_lines, result.metadata)
    items, _item_boundaries = _parse_agenda_items(all_lines, all_links, result)

    # ---- 2. Collect embedded attachment documents ----------------------------
    # Each L2 entry is an embedded file.  Its page range spans from its first
    # child with a real page to the page before the next L2 entry's content
    # (or end of document).
    l2_docs = []
    l2_indices = [i for i, e in enumerate(toc) if e[0] == 2]

    for pos, toc_idx in enumerate(l2_indices):
        doc_title = toc[toc_idx][1].strip()

        # Collect real pages from all descendants until the next L2
        child_pages = []
        end_scan = l2_indices[pos + 1] if pos + 1 < len(l2_indices) else len(toc)
        for j in range(toc_idx + 1, end_scan):
            if toc[j][2] > 0:
                child_pages.append(toc[j][2] - 1)  # 0-indexed

        if not child_pages:
            continue

        page_start = min(child_pages)
        # End boundary: page before the next L2 doc's first real page,
        # or end of document for the last L2 entry.
        if pos + 1 < len(l2_indices):
            next_child_pages = []
            next_end = (l2_indices[pos + 2]
                        if pos + 2 < len(l2_indices) else len(toc))
            for j in range(l2_indices[pos + 1] + 1, next_end):
                if toc[j][2] > 0:
                    next_child_pages.append(toc[j][2] - 1)
            page_end = (min(next_child_pages) - 1
                        if next_child_pages else doc.page_count - 1)
        else:
            page_end = doc.page_count - 1

        page_end = max(page_end, page_start)

        memo = _extract_memo_content(doc, page_start, page_end)
        memo.subject = memo.subject or doc_title
        l2_docs.append((doc_title, page_start, page_end, memo))

    # ---- 3. Match attachments to items by content similarity -----------------
    # For each embedded document, find the best-matching agenda item.
    # Require a minimum score to avoid false matches (e.g. minutes that
    # reference a different meeting entirely).
    MATCH_THRESHOLD = 0.25  # confidence 6/10 — may need tuning

    for doc_idx, (doc_title, ps, pe, memo) in enumerate(l2_docs):
        best_score = 0.0
        best_item = None
        for item in items:
            # Match against item title + body combined
            item_text = item.title + " " + (item.body or "")
            score = _document_bundle_match_score(item_text, doc_title)
            if score > best_score:
                best_score = score
                best_item = item

        if best_item and best_score >= MATCH_THRESHOLD:
            best_item.memos.append(memo)
            best_item.attachments.append(_Attachment(
                label=doc_title, url="",
                page_start=ps + 1, page_end=pe + 1,
            ))
            if memo.recommended_action and not best_item.recommended_action:
                best_item.recommended_action = memo.recommended_action
        else:
            result.orphan_memos.append(memo)

    result.items = items

    matched_count = sum(len(item.memos) for item in items)
    logger.debug("parsed toc document bundle",
                 item_count=len(result.items),
                 attachment_docs=len(l2_docs),
                 matched=matched_count,
                 orphan_memos=len(result.orphan_memos))


def _parse_toc_deep_hierarchical(doc, toc, result):
    """Deep hierarchical TOC: L1=root, L2=sections, L3=items, L4=attachments.

    Found in Escribe-generated agenda packets. The agenda text lives on the
    first few pages (where L2/L3 entries cluster), and L4 entries point to
    attachment pages deeper in the document.
    """
    result.metadata.parse_method = "toc_deep_hierarchical"

    # Find where the agenda TOC ends: stop at second L1 entry (e.g., "Appendix")
    # which marks the start of the document-navigation TOC
    agenda_toc_end = len(toc)
    l1_count = 0
    for idx, entry in enumerate(toc):
        if entry[0] == 1:
            l1_count += 1
            if l1_count == 2:
                agenda_toc_end = idx
                break

    # Only use entries from the agenda TOC section
    agenda_toc = toc[:agenda_toc_end]

    # Find agenda page range from first-section L2/L3 entries only
    item_pages = sorted(set(
        entry[2] - 1 for entry in agenda_toc
        if entry[0] in (2, 3) and entry[2] > 0
    ))
    agenda_end = max(item_pages) if item_pages else 0

    # Extract agenda text for body extraction
    all_lines = []
    for pi in range(0, min(agenda_end + 1, doc.page_count)):
        all_lines.extend(_extract_page_text_with_positions(doc[pi]))
    _extract_meeting_metadata(all_lines, result.metadata)

    # Build sorted list of L4 page starts for attachment boundary detection
    all_l4_starts = sorted(set(
        entry[2] - 1 for entry in agenda_toc
        if entry[0] == 4 and entry[2] > 0
    ))

    # Find bottom boundary: either the start of the appendix TOC or end of doc
    bottom_page = doc.page_count - 1
    if agenda_toc_end < len(toc):
        # Second L1 entry page marks boundary
        appendix_page = toc[agenda_toc_end][2] - 2
        if appendix_page > 0:
            bottom_page = appendix_page
    for entry in agenda_toc:
        if entry[1].strip().lower() == 'bottom':
            bottom_page = entry[2] - 2

    current_section = None

    for i, (level, title, page_num) in enumerate(agenda_toc):
        # L2 = section headers
        if level == 2:
            section_title = title.strip()
            # Strip leading letter prefix: "A. CALL TO ORDER" -> "CALL TO ORDER"
            section_clean = re.sub(r'^[A-Z]\.\s*', '', section_title)
            if section_clean:
                current_section = section_clean
            continue

        # L3 = agenda items
        if level != 3:
            continue

        item_title = title.strip()
        if not item_title:
            continue

        # Extract item number from title: "1. Roll Call" -> ("1", "Roll Call")
        num_match = re.match(r'^(\d+)\.\s*(.*)', item_title)
        if num_match:
            item_num = num_match.group(1)
            item_title_clean = num_match.group(2).strip()
        else:
            item_num = ""
            item_title_clean = item_title

        # Skip trivial procedural items
        if item_title_clean.lower() in ('roll call', 'adjournment'):
            continue

        item = _AgendaItem(
            number=item_num,
            title=item_title_clean or item_title,
            section=current_section or "",
            body="",
            recommended_action="",
            page_start=page_num,
            page_end=page_num,
        )

        # Collect L4 children as attachments
        for j in range(i + 1, len(agenda_toc)):
            child_level = agenda_toc[j][0]
            if child_level <= 3:
                break
            if child_level == 4:
                att_title = agenda_toc[j][1].strip()
                att_page_start = agenda_toc[j][2] - 1  # 0-indexed

                # Skip weblinks (page -1 or -2)
                if att_page_start < 0:
                    continue

                # Determine attachment page range
                try:
                    idx = all_l4_starts.index(att_page_start)
                    att_page_end = all_l4_starts[idx + 1] - 1 if idx + 1 < len(all_l4_starts) else bottom_page
                except ValueError:
                    att_page_end = bottom_page

                # Extract memo content from attachment pages
                memo = _extract_memo_content(doc, att_page_start, att_page_end)
                memo.subject = memo.subject or att_title

                item.memos.append(memo)
                item.attachments.append(_Attachment(
                    label=att_title, url="",
                    page_start=att_page_start + 1,
                    page_end=att_page_end + 1,
                ))

                if memo.recommended_action and not item.recommended_action:
                    item.recommended_action = memo.recommended_action

        result.items.append(item)
        if current_section and current_section not in result.sections:
            result.sections.append(current_section)

    logger.debug("parsed toc deep hierarchical",
                 item_count=len(result.items),
                 section_count=len(result.sections),
                 total_attachments=sum(len(it.attachments) for it in result.items))


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
        for (idx, _level, title, page_num) in toc_entries_beyond:
            page_0 = page_num - 1
            if idx + 1 < len(toc):
                next_p = toc[idx + 1][2] - 2
            else:
                next_p = doc.page_count - 1
            end_page = max(page_0, next_p)

            memo = _extract_memo_content(doc, page_0, end_page)
            memo.subject = memo.subject or title.strip()
            memos.append(memo)

        # Fuzzy-match memos to items
        for memo in memos:
            best_score = 0.0
            best_item = None
            for item in items:
                item_text = (item.title + " " + item.body).lower()
                score = _text_similarity(memo.subject, item.title)
                body_score = _text_similarity(memo.subject, item.body[:300])
                score = max(score, body_score)
                subject_words = set(w for w in re.findall(r'[a-z]{4,}', memo.subject.lower()))
                stopwords = {'with', 'from', 'that', 'this', 'will', 'have', 'been', 'their', 'they',
                             'city', 'shall', 'agreement', 'would', 'upon', 'which', 'between'}
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

        _assign_links_to_items(all_links, items, item_boundaries, result)
        result.items = items
    else:
        # Text parsing failed: build items directly from TOC L1 entries
        _build_items_from_toc_entries(doc, toc, toc_entries_beyond, agenda_end, result)

    logger.debug("parsed toc flat",
                 item_count=len(result.items),
                 matched_memos=sum(len(it.memos) for it in result.items),
                 orphan_memos=len(result.orphan_memos))


def _build_items_from_toc_entries(doc, toc, toc_entries_beyond, agenda_end, result):
    """Build items directly from TOC entries when text parsing yields nothing.

    Each L1 entry beyond the agenda becomes an item.
    Sub-entries (L2+) become attachments within their parent item.
    """
    # Collect all valid page starts for range calculation
    all_content_starts = sorted(set(
        entry[2] - 1 for entry in toc
        if entry[2] - 1 > agenda_end and entry[2] > 0
    ))

    for (idx, level, title, page_num) in toc_entries_beyond:
        if level != 1:
            continue

        page_0 = page_num - 1
        parsed_num, parsed_title = _parse_flat_toc_title(title)
        item_num = parsed_num if parsed_num is not None else ""
        item_title = parsed_title if parsed_title is not None else title.strip()

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

        item = _AgendaItem(
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
                continue

            # Compute sub-entry end page
            try:
                si = all_content_starts.index(sub_page)
                sub_end = all_content_starts[si + 1] - 1 if si + 1 < len(all_content_starts) else item_end_page
            except ValueError:
                sub_end = item_end_page

            sub_end = min(sub_end, item_end_page)

            item.attachments.append(_Attachment(
                label=sub_title, url="",
                page_start=sub_page + 1,
                page_end=sub_end + 1,
            ))

        result.items.append(item)

        section = item.section
        if section and section not in result.sections:
            result.sections.append(section)

    logger.debug("built items from toc entries",
                 item_count=len(result.items))


# ---------------------------------------------------------------------------
# URL-based parsing
# ---------------------------------------------------------------------------

# Text-based item detection is reliable on short agenda documents (1-20 pages)
# where numbered items are genuine agenda entries. On compiled packet PDFs
# (100+ pages of staff reports, legal text, exhibits), statute citations,
# section numbers, and exhibit headers all match ITEM_NUM_RE, producing
# hundreds of garbage items. Cap text parsing to the agenda pages.
_MAX_PAGES_FOR_TEXT_ITEMS = 20


def _parse_url_based(doc, result):
    result.metadata.parse_method = "url"

    all_lines = []
    all_links = []
    agenda_lines = []  # lines from first N pages only (for item detection)
    for page in doc:
        page_lines = _extract_page_text_with_positions(page)
        all_lines.extend(page_lines)
        all_links.extend(_extract_links(page))
        if page.number < _MAX_PAGES_FOR_TEXT_ITEMS:
            agenda_lines.extend(page_lines)

    _extract_meeting_metadata(all_lines, result.metadata)
    # Parse item boundaries from agenda pages only, not the full packet.
    # Links are extracted from ALL pages so attachments deep in the packet
    # still get assigned to items found on the agenda pages.
    items, item_boundaries = _parse_agenda_items(agenda_lines, all_links, result)
    _assign_links_to_items(all_links, items, item_boundaries, result)
    result.items = items


# ---------------------------------------------------------------------------
# Shared item parsing (used by both TOC-flat and URL paths)
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
        has_num = bool(re.match(r'^\s*(?:\d{4}-\d{1,4}|\d{1,3}\.[a-z]|[A-Z]\.\d|\(\d{1,3}\)|\([a-z]\)|\d{1,3}\.|[A-Z]\.)', text))

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
                item = _AgendaItem(
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

        # Section header (only if not already matched as an item)
        if _is_section_header(text) and (line["is_bold"] or _is_mostly_upper(text)):
            section_name = _extract_section_name(text)
            current_section = section_name
            if section_name not in result.sections:
                result.sections.append(section_name)
            continue

    # Collect body text for each item
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


# ---------------------------------------------------------------------------
# Link-to-item assignment
# ---------------------------------------------------------------------------

def _assign_links_to_items(all_links, items, item_boundaries, result):
    for link in all_links:
        if not _is_attachment_url(link["url"]):
            continue
        best = _find_owning_item(link, item_boundaries)
        if best is not None:
            items[best].attachments.append(_Attachment(
                label=link["label"], url=link["url"],
                page_start=link["page"] + 1, page_end=link["page"] + 1,
                bbox=link["bbox"],
            ))
        else:
            result.orphan_links.append(_Attachment(
                label=link["label"], url=link["url"],
                page_start=link["page"] + 1, page_end=link["page"] + 1,
                bbox=link["bbox"],
            ))


def _find_owning_item(link, item_boundaries):
    """Assign a link to the nearest preceding item on the same or earlier page."""
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
    """Fallback: find which item range the link falls within."""
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
# Main internal parser
# ---------------------------------------------------------------------------

def _parse_agenda_internal(pdf_path: str, force_method: Optional[str] = None) -> _ParsedAgenda:
    """Parse a PDF, dispatching by content signals.

    Priority: attachment URLs > TOC structure > text extraction.
    Hyperlinked attachment URLs (Legistar S3, CivicPlus, etc.) are the strongest
    signal — when present, the links ARE the data and any TOC is just navigation
    bookmarks. When no links exist but a deep TOC is present, the embedded memos
    are the data. These two patterns don't co-exist in practice.
    """
    doc = fitz.open(pdf_path)
    result = _ParsedAgenda()

    meta = doc.metadata or {}
    result.metadata.page_count = doc.page_count
    result.metadata.pdf_metadata = {k: v for k, v in meta.items() if v}

    if force_method == "toc":
        _parse_toc_based(doc, result)
    elif force_method == "url":
        _parse_url_based(doc, result)
    elif _has_structural_toc(doc) and doc.page_count > 10:
        # Large PDFs with dense TOC are packet documents -- TOC is the real
        # structure. Any hyperlinks are incidental (budget tables, etc).
        # Thin navigation TOCs (2-3 entries) fall through to attachment check.
        _parse_toc_based(doc, result)
    elif _has_attachment_links(doc):
        _parse_url_based(doc, result)
    elif _has_meaningful_toc(doc):
        # Reaching here means page_count <= 10 (large TOC docs handled above).
        # Small documents are thin agendas where the TOC is typically navigation
        # bookmarks, not structural data.  Try url first -- it picks up
        # sub-items (4.1, 8.2) that TOC parsers miss.  Fall back to TOC
        # if url finds nothing.
        _parse_url_based(doc, result)
        if not result.items:
            _parse_toc_based(doc, result)
    else:
        _parse_url_based(doc, result)

    doc.close()
    return result


# ---------------------------------------------------------------------------
# Public API - returns pipeline-compatible dicts
# ---------------------------------------------------------------------------

def parse_agenda_pdf(pdf_path: str, force_method: Optional[str] = None) -> Dict[str, Any]:
    """
    Parse a municipal agenda PDF into pipeline-compatible item dicts.

    Dispatches to TOC-based or URL-based parsing depending on PDF structure.

    For TOC-based items with embedded memos, memo full_text is combined into
    body_text so the processor can summarize directly without URL downloads.

    Returns dict matching the format expected by the adapter/orchestrator:
    {
        "items": [
            {
                "vendor_item_id": "4.3",
                "title": "...",
                "sequence": 1,
                "agenda_number": "4.3",
                "body_text": "...",          # From memos (TOC) or coversheet
                "attachments": [{"name": "...", "url": "...", "type": "pdf"}],
                "matter_file": "2024-001" or None,
                "metadata": {
                    "section": "CONSENT CALENDAR",
                    "parse_method": "toc_hierarchical",
                    ...
                },
            },
            ...
        ],
        "metadata": {"body_name": "...", "meeting_date": "...", "parse_method": "...", ...},
    }
    """
    parsed = _parse_agenda_internal(pdf_path, force_method=force_method)

    pipeline_items: List[Dict[str, Any]] = []
    for idx, item in enumerate(parsed.items):
        pipeline_item: Dict[str, Any] = {
            "vendor_item_id": item.number,
            "title": item.title,
            "sequence": idx + 1,
            "agenda_number": item.number,
            "attachments": [
                {
                    "name": att.label or "Attachment",
                    "url": att.url,
                    "type": _attachment_type(att.url) if att.url else "embedded",
                }
                for att in item.attachments
                if att.url or att.label  # URL-based or embedded (labeled) attachments
            ],
        }

        # Build body_text: prefer memo content (richer), fall back to agenda body
        body_text_parts = []
        if item.memos:
            for memo in item.memos:
                if memo.full_text:
                    body_text_parts.append(memo.full_text)
        if item.body and not body_text_parts:
            # Only use agenda body as fallback when no memo text available
            body_text_parts.append(item.body)
        elif item.body and body_text_parts:
            # Prepend short agenda body as context when memos also present
            body_text_parts.insert(0, item.body)

        if body_text_parts:
            pipeline_item["body_text"] = "\n\n".join(body_text_parts)

        # Search both title and all available text for matter file
        all_text = item.body
        if item.memos:
            all_text += " " + " ".join(m.subject for m in item.memos if m.subject)
        matter_file = _extract_matter_file(item.title, all_text)
        if matter_file:
            pipeline_item["matter_file"] = matter_file

        item_metadata: Dict[str, Any] = {}
        if item.section:
            item_metadata["section"] = item.section
        if item.recommended_action:
            item_metadata["recommended_action"] = item.recommended_action
        if item.page_start:
            item_metadata["page_start"] = item.page_start
            item_metadata["page_end"] = item.page_end
        if parsed.metadata.parse_method:
            item_metadata["parse_method"] = parsed.metadata.parse_method
        if item.memos:
            item_metadata["memo_count"] = len(item.memos)
            item_metadata["memo_pages"] = sum(
                (m.page_end - m.page_start + 1) for m in item.memos
            )
        if item_metadata:
            pipeline_item["metadata"] = item_metadata

        pipeline_items.append(pipeline_item)

    result = {
        "items": pipeline_items,
        "metadata": {
            "body_name": parsed.metadata.body_name,
            "meeting_date": parsed.metadata.meeting_date,
            "meeting_type": parsed.metadata.meeting_type,
            "page_count": parsed.metadata.page_count,
            "parse_method": parsed.metadata.parse_method,
        },
    }

    if parsed.orphan_links:
        result["orphan_links"] = [
            {"name": att.label, "url": att.url, "type": _attachment_type(att.url)}
            for att in parsed.orphan_links
        ]

    if parsed.orphan_memos:
        result["orphan_memos"] = [
            {"subject": m.subject, "page_start": m.page_start, "page_end": m.page_end}
            for m in parsed.orphan_memos
        ]

    logger.debug(
        "parsed agenda pdf",
        parse_method=parsed.metadata.parse_method,
        item_count=len(pipeline_items),
        section_count=len(parsed.sections),
        orphan_links=len(parsed.orphan_links),
        orphan_memos=len(parsed.orphan_memos),
        page_count=parsed.metadata.page_count,
    )

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m vendors.adapters.parsers.agenda_chunker <path_to_pdf> [--json] [--items-only] [--force-toc] [--force-url]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    as_json = "--json" in sys.argv
    items_only = "--items-only" in sys.argv
    force = None
    if "--force-toc" in sys.argv:
        force = "toc"
    elif "--force-url" in sys.argv:
        force = "url"

    result = parse_agenda_pdf(pdf_path, force_method=force)
    items = result["items"]
    meta = result["metadata"]

    if as_json:
        if items_only:
            print(json.dumps(items, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"{'=' * 60}")
        print(f"{meta.get('body_name', '')} | {meta.get('meeting_type', '')} | {meta.get('meeting_date', '')}")
        print(f"{meta.get('page_count', 0)} pages | {len(items)} items | method: {meta.get('parse_method', '?')}")
        print(f"{'=' * 60}")

        for item in items:
            print(f"\n{'─' * 60}")
            section = (item.get("metadata") or {}).get("section", "")
            method = (item.get("metadata") or {}).get("parse_method", "")
            print(f"[{section}] {item.get('agenda_number', '')}  {item['title']}")
            if item.get("matter_file"):
                print(f"  Matter: {item['matter_file']}")
            rec = (item.get("metadata") or {}).get("recommended_action", "")
            if rec:
                preview = rec[:120] + ("..." if len(rec) > 120 else "")
                print(f"  Rec. Action: {preview}")
            memo_count = (item.get("metadata") or {}).get("memo_count", 0)
            if memo_count:
                memo_pages = (item.get("metadata") or {}).get("memo_pages", 0)
                print(f"  Embedded Memos: {memo_count} ({memo_pages} pages)")
            body_text = item.get("body_text", "")
            if body_text:
                preview = body_text[:150].replace('\n', ' ')
                if len(body_text) > 150:
                    preview += "..."
                print(f"  Body text: {len(body_text)} chars - {preview}")
            atts = item.get("attachments", [])
            if atts:
                print(f"  Attachments ({len(atts)}):")
                for att in atts:
                    print(f"    - {att['name']}")
                    print(f"      {att['url']}")
            elif not memo_count:
                print("  Attachments: none")

        orphans = result.get("orphan_links", [])
        if orphans:
            print(f"\n{'─' * 60}")
            print(f"ORPHAN LINKS ({len(orphans)}):")
            for att in orphans:
                print(f"  {att['name']} -> {att['url']}")

        orphan_memos = result.get("orphan_memos", [])
        if orphan_memos:
            print(f"\n{'─' * 60}")
            print(f"ORPHAN MEMOS ({len(orphan_memos)}):")
            for m in orphan_memos:
                print(f"  p{m['page_start']}-{m['page_end']}: {m['subject'][:80]}")
