"""
Agenda PDF Chunker v2 -- anchor-first ("leaf to root") extraction

Two paths, both starting from invariant anchors instead of regex item patterns:

1. URL path: Find hyperlinks first, cluster by vertical proximity,
   walk backwards to find the heading above each cluster.
2. TOC path: Use PDF bookmark page boundaries as slicing points.
   Each bookmark range is an item. No title format requirements.

The v1 chunker works root-to-leaf: find item headers by regex, then look for
content below them. This inverts that: find the anchors (links, page boundaries)
and discover structure from there.

Dependencies: PyMuPDF (fitz)
"""

import fitz
import re
from typing import Dict, Any, List, Optional, Tuple

from config import get_logger

from vendors.adapters.parsers.agenda_chunker import (
    _Attachment,
    _AgendaItem,
    _ParsedAgenda,
    _extract_page_text_with_positions,
    _extract_links,
    _extract_meeting_metadata,
    _extract_memo_content,
    _extract_matter_file,
    _is_attachment_url,
    _attachment_type,
    _is_section_header,
    _extract_section_name,
)

logger = get_logger(__name__).bind(component="vendor")


# ---------------------------------------------------------------------------
# Permissive item number extraction
# ---------------------------------------------------------------------------

_ITEM_PREFIX_RE = re.compile(
    r'^(?:Item|No\.?|#)\s*',
    re.IGNORECASE,
)

def _extract_item_number_permissive(title: str) -> Tuple[str, str]:
    """Best-effort extraction of an item number prefix from a title string.

    Returns (number, remainder). Number may be "" if none found.
    Accepts any leading token that contains a digit or is a single letter,
    without requiring a specific format like A.1 or D.4.
    """
    cleaned = _ITEM_PREFIX_RE.sub('', title.strip())

    # Tab-separated: "A.1\tSome Title" or "Item A.\tApprove Minutes"
    if '\t' in cleaned:
        parts = cleaned.split('\t', 1)
        candidate = parts[0].strip().rstrip('.')
        remainder = parts[1].strip()
        if candidate and len(candidate) <= 15:
            if re.search(r'\d', candidate) or (len(candidate) <= 2 and candidate[0].isalpha()):
                return candidate, remainder or title.strip()

    # Space/colon/period separated: "4.3 Site Plan Review", "A: Budget"
    m = re.match(r'^([A-Za-z0-9().\-/]{1,15})[:\s.\t]+(.+)', cleaned)
    if m:
        candidate = m.group(1).rstrip('.')
        remainder = m.group(2).strip()
        if re.search(r'\d', candidate) or (len(candidate) <= 2 and candidate[0].isalpha()):
            return candidate, remainder

    return "", title.strip()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_MAX_AGENDA_PAGES = 15
# Threshold above which a doc is treated as a "packet" (agenda + staff reports).
# Kept at 10 independent of the scan cap so auto-detection still prefers the
# URL path for 11-15 page agendas with bookmarks -- bumping the cap alone
# would flip those into the TOC path and lose items.
_LARGE_PACKET_THRESHOLD = 10


def _has_agenda_links(doc) -> bool:
    """Check if the first 10 pages contain attachment-like links."""
    for page in doc:
        if page.number >= _MAX_AGENDA_PAGES:
            break
        for link in page.get_links():
            if link.get("kind") == 2 and _is_attachment_url(link.get("uri", "")):
                return True
    return False


def _count_internal_page_links(doc) -> int:
    """Count kind=4 internal links on first pages that point deep into the doc.

    These are "Page XX" references in agenda text that jump to staff reports.
    Returns count of qualifying links (target page beyond agenda pages).
    """
    count = 0
    for page in doc:
        if page.number >= _MAX_AGENDA_PAGES:
            break
        for link in page.get_links():
            if link.get("kind") == 4:
                try:
                    target = int(link.get("page", -1))
                except (ValueError, TypeError):
                    continue
                if target >= _MAX_AGENDA_PAGES:
                    count += 1
    return count


def _detect_parse_path(doc) -> str:
    """Determine extraction path: 'toc', 'url', 'pageref', or 'empty'.

    Mirrors v1 logic: large documents (>10 pages) with a TOC are packet
    PDFs where the TOC represents embedded staff reports, not agenda items.
    If the first 10 pages also carry attachment links, prefer URL path —
    those links are the real agenda structure.

    New: if the agenda pages have internal page links (kind=4) pointing deep
    into the document, those are "Page XX" references and define item
    boundaries just like URL links do. Prefer this over TOC since the TOC
    in these packets usually contains attachment-internal bookmarks (slide
    titles, memo sections), not agenda items.
    """
    toc = doc.get_toc()
    real_entries = [e for e in toc if e[2] > 0]
    distinct_pages = set(e[2] for e in real_entries)
    has_toc = len(real_entries) >= 3 and len(distinct_pages) >= 2

    has_links = _has_agenda_links(doc)

    # Internal page links: agenda text with "Page XX" jumps to staff reports
    internal_link_count = _count_internal_page_links(doc)
    has_pagerefs = internal_link_count >= 3

    if has_links and doc.page_count > _LARGE_PACKET_THRESHOLD:
        # Large packet with URL links on the agenda pages — try URL first,
        # fall back to TOC if it produces nothing.
        return "url_then_toc" if has_toc else "url"

    if has_pagerefs and doc.page_count > _LARGE_PACKET_THRESHOLD:
        # Large packet with internal page references — these define item
        # boundaries more accurately than attachment-level TOC bookmarks.
        return "pageref"

    if has_toc:
        return "toc"

    if has_links:
        return "url"

    return "empty"


# ---------------------------------------------------------------------------
# URL path: cluster links, walk backwards to headings
# ---------------------------------------------------------------------------

def _collect_all_links(doc, max_pages: int = 0) -> List[dict]:
    """Extract all attachment-like links from the document.

    Args:
        max_pages: If >0, only scan the first N pages. Prevents picking up
                   links from embedded staff reports deep in a packet PDF.
    """
    all_links = []
    for page in doc:
        if max_pages and page.number >= max_pages:
            break
        page_links = _extract_links(page)
        for link in page_links:
            if _is_attachment_url(link["url"]):
                all_links.append(link)
    return all_links


def _collect_all_lines(doc, max_pages: int = 0) -> List[dict]:
    """Extract all text lines with position metadata from the document.

    Args:
        max_pages: If >0, only scan the first N pages.
    """
    all_lines = []
    for page in doc:
        if max_pages and page.number >= max_pages:
            break
        all_lines.extend(_extract_page_text_with_positions(page))
    return all_lines


def _cluster_links_by_position(
    links: List[dict],
    median_line_height: float = 14.0,
) -> List[List[dict]]:
    """Group links that are vertically close on the same page.

    Each cluster represents the attachment block for one agenda item.
    A new cluster starts when the page changes or the vertical gap
    exceeds 2.5x the median line height.
    """
    if not links:
        return []

    sorted_links = sorted(
        links, key=lambda link: (link["page"], link["y_center"])
    )
    gap_threshold = median_line_height * 2.5

    clusters = [[sorted_links[0]]]
    for link in sorted_links[1:]:
        prev = clusters[-1][-1]
        if link["page"] != prev["page"] or (link["y_center"] - prev["y_center"]) > gap_threshold:
            clusters.append([link])
        else:
            clusters[-1].append(link)

    # Dedup links by URL within each cluster. Wrapped titles produce
    # multiple link elements (one per line) all pointing to the same URL.
    # Merge them into a single link with the concatenated label.
    deduped_clusters = []
    for cluster in clusters:
        seen: Dict[str, dict] = {}
        order: List[str] = []
        for link in cluster:
            url = link["url"]
            if url in seen:
                seen[url]["label"] += " " + link["label"]
            else:
                seen[url] = dict(link)
                order.append(url)
        deduped_clusters.append([seen[url] for url in order])

    return deduped_clusters


def _estimate_median_line_height(lines: List[dict]) -> float:
    """Estimate median line height from first page text lines."""
    if not lines:
        return 14.0
    heights = [
        line["y1"] - line["y0"]
        for line in lines[:50]
        if line["y1"] - line["y0"] > 2
    ]
    if not heights:
        return 14.0
    heights.sort()
    return heights[len(heights) // 2]


def _line_overlaps_any_link(line: dict, links: List[dict], tolerance: float = 8.0) -> bool:
    """Check if a text line vertically overlaps with any link in the list."""
    for link in links:
        if link["page"] == line["page"] and abs(link["y_center"] - (line["y0"] + line["y1"]) / 2) < tolerance:
            return True
    return False


def _find_heading_above_cluster(
    cluster: List[dict],
    all_lines: List[dict],
    search_floor_page: int,
    search_floor_y: float,
) -> Optional[dict]:
    """Walk backwards from a URL cluster to find the item heading above it.

    Searches between (search_floor_page, search_floor_y) and the top of the
    first link in the cluster.

    Returns dict with title, number, page, y0 or None.
    """
    first_link = min(
        cluster, key=lambda link: (link["page"], link["y_center"])
    )
    ceiling_page = first_link["page"]
    ceiling_y = first_link["y_center"]

    # Collect candidate lines in the search zone
    candidates = []
    for line in all_lines:
        # Must be on or between floor and ceiling pages
        if line["page"] < search_floor_page or line["page"] > ceiling_page:
            continue
        if line["page"] == search_floor_page and line["y0"] < search_floor_y:
            continue
        if line["page"] == ceiling_page and line["y0"] >= ceiling_y:
            continue
        # Skip lines that overlap with links in this cluster
        if _line_overlaps_any_link(line, cluster):
            continue
        candidates.append(line)

    if not candidates:
        return None

    # Walk backwards from the bottom of the zone (closest to links)
    candidates.sort(
        key=lambda line: (line["page"], line["y0"]), reverse=True
    )

    heading_lines = []
    section = ""

    for line in candidates:
        text = line["text"].strip()
        if not text:
            continue

        # Check if this is a section header (CONSENT CALENDAR, PUBLIC HEARINGS, etc.)
        if _is_section_header(text) and not heading_lines:
            section = _extract_section_name(text)
            continue

        # Long lines (>200 chars) are body paragraphs, not headings
        if len(text) > 200:
            if heading_lines:
                break
            continue

        heading_lines.append(line)

        # Stop after accumulating 3 lines or hitting a gap
        if len(heading_lines) >= 3:
            break
        if len(heading_lines) >= 2:
            prev = heading_lines[-2]
            curr = heading_lines[-1]
            # If lines are on different pages or far apart, stop
            if prev["page"] != curr["page"] or abs(prev["y0"] - curr["y0"]) > 40:
                break

    if not heading_lines:
        return None

    # Heading lines were collected bottom-up, reverse for natural order
    heading_lines.reverse()
    full_title = " ".join(line["text"].strip() for line in heading_lines)
    number, title_text = _extract_item_number_permissive(full_title)

    # Collect body text: everything between heading and links that isn't the heading itself
    heading_y_top = min(line["y0"] for line in heading_lines)
    heading_page = min(line["page"] for line in heading_lines)
    body_parts = []
    for line in all_lines:
        if line["page"] < heading_page or line["page"] > ceiling_page:
            continue
        if line["page"] == heading_page and line["y0"] <= heading_y_top:
            continue
        if line["page"] == ceiling_page and line["y0"] >= ceiling_y:
            continue
        if _line_overlaps_any_link(line, cluster):
            continue
        if line in heading_lines:
            continue
        body_parts.append(line["text"].strip())

    return {
        "title": title_text,
        "number": number,
        "section": section,
        "page": heading_lines[0]["page"] + 1,
        "y0": heading_lines[0]["y0"],
        "body": "\n".join(body_parts) if body_parts else "",
    }


def _parse_url_v2(doc, result: _ParsedAgenda):
    """URL-based extraction: cluster links, walk backwards to headings."""
    result.metadata.parse_method = "v2_url"

    # For large packets, restrict to agenda pages only.
    max_pages = _MAX_AGENDA_PAGES if doc.page_count > _MAX_AGENDA_PAGES else 0
    all_links = _collect_all_links(doc, max_pages=max_pages)
    all_lines = _collect_all_lines(doc, max_pages=max_pages)

    if not all_links:
        return

    median_lh = _estimate_median_line_height(all_lines)
    clusters = _cluster_links_by_position(all_links, median_lh)

    # Track the bottom of the previous cluster for search floor
    prev_floor_page = 0
    prev_floor_y = 0.0
    current_section = ""

    for cluster in clusters:
        heading = _find_heading_above_cluster(
            cluster, all_lines, prev_floor_page, prev_floor_y,
        )

        if heading:
            if heading["section"]:
                current_section = heading["section"]

            attachments = [
                _Attachment(
                    label=link["label"],
                    url=link["url"],
                    page_start=link["page"] + 1,
                    page_end=link["page"] + 1,
                    bbox=link["bbox"],
                )
                for link in cluster
            ]

            item = _AgendaItem(
                number=heading["number"],
                title=heading["title"],
                section=heading.get("section") or current_section,
                body=heading.get("body", ""),
                recommended_action="",
                attachments=attachments,
                page_start=heading["page"],
                page_end=cluster[-1]["page"] + 1,
            )
            result.items.append(item)
        else:
            # Orphan links -- couldn't find a heading
            for link in cluster:
                result.orphan_links.append(_Attachment(
                    label=link["label"],
                    url=link["url"],
                    page_start=link["page"] + 1,
                    page_end=link["page"] + 1,
                    bbox=link["bbox"],
                ))

        # Update floor for next cluster
        last_link = max(
            cluster, key=lambda link: (link["page"], link["y_center"])
        )
        prev_floor_page = last_link["page"]
        prev_floor_y = last_link["y_center"]


# ---------------------------------------------------------------------------
# Page-reference path: internal links as item boundaries
# ---------------------------------------------------------------------------

def _collect_internal_page_links(doc) -> List[dict]:
    """Collect kind=4 internal links from agenda pages that point deep into the doc.

    Returns list of dicts with keys: source_page (0-indexed), target_page (0-indexed),
    rect (fitz.Rect), text (extracted from the link rect on the source page).
    """
    links = []
    for page in doc:
        if page.number >= _MAX_AGENDA_PAGES:
            break
        for link in page.get_links():
            if link.get("kind") != 4:
                continue
            try:
                target = int(link.get("page", -1))
            except (ValueError, TypeError):
                continue
            # Accept any forward-pointing link beyond the current agenda page.
            # The detection gate (_count_internal_page_links) is strict (target >= 10),
            # but once we're on this path, collect all forward refs including
            # early attachments (e.g. warrants on page 5).
            if target <= page.number:
                continue
            rect = link.get("from")
            if not rect:
                continue
            rect = fitz.Rect(rect)
            text = page.get_text("text", clip=rect).strip()
            if not text:
                continue
            links.append({
                "source_page": page.number,
                "target_page": int(target),
                "rect": rect,
                "text": " ".join(text.split()),
            })
    # Sort by position on the agenda pages
    links.sort(
        key=lambda link: (link["source_page"], link["rect"].y0)
    )
    return links


def _parse_pageref_v2(doc, result: _ParsedAgenda):
    """Page-reference extraction: use internal page links as item anchors.

    CivicPlus packet PDFs have agenda text on the first few pages with
    "Page XX" references that are internal links (kind=4) pointing to the
    staff report pages. The link rects cover the full item description text.
    The TOC in these PDFs only contains attachment-internal bookmarks
    (slide titles, memo headings) and is useless for item extraction.
    """
    result.metadata.parse_method = "v2_pageref"

    links = _collect_internal_page_links(doc)
    if not links:
        return

    current_section = ""

    for i, link in enumerate(links):
        # The link text is the full item description from the agenda page
        title_text = link["text"]

        # Strip trailing "Page XX" or "- Page XX" reference
        title_text = re.sub(r'\s*[-\u2013\u2014]?\s*Page\s+\d+\s*$', '', title_text)

        number, title_text = _extract_item_number_permissive(title_text)

        # Detect section headers (rare in page-ref links but possible)
        if _is_section_header(title_text):
            current_section = _extract_section_name(title_text)
            continue

        # Page range: from this link's target to the next link's target (or end of doc)
        page_start_0 = link["target_page"]
        if i + 1 < len(links):
            page_end_0 = links[i + 1]["target_page"] - 1
        else:
            page_end_0 = doc.page_count - 1
        page_end_0 = max(page_end_0, page_start_0)

        # Extract body text from the staff report pages
        memo = _extract_memo_content(doc, page_start_0, page_end_0)

        memos = []
        if memo.full_text.strip():
            memos.append(memo)

        item = _AgendaItem(
            number=number,
            title=title_text,
            section=current_section,
            body="",
            recommended_action="",
            memos=memos,
            page_start=page_start_0 + 1,
            page_end=page_end_0 + 1,
        )

        for m in memos:
            if m.recommended_action:
                item.recommended_action = m.recommended_action
                break

        result.items.append(item)

    logger.debug(
        "v2 pageref parsed",
        item_count=len(result.items),
        link_count=len(links),
    )


# ---------------------------------------------------------------------------
# TOC path: page-range slicing
# ---------------------------------------------------------------------------

_SKIP_TOC_TITLES = {"top", "bottom", "end", "back"}


def _parse_toc_v2(doc, result: _ParsedAgenda):
    """TOC-based extraction: slice by page boundaries, no title format requirements."""
    result.metadata.parse_method = "v2_toc"

    toc = doc.get_toc()
    if not toc:
        return

    # Filter noise entries
    entries = [
        (level, title.strip(), page)
        for level, title, page in toc
        if page > 0 and title.strip().lower() not in _SKIP_TOC_TITLES
    ]
    if len(entries) < 2:
        return

    # Determine the item level.
    # Strategy: prefer the shallowest level that has 3+ entries.
    # If the shallowest level has only 1-2 entries (document/section containers),
    # descend to the next level for items.
    level_counts = {}
    for level, title, page in entries:
        level_counts.setdefault(level, []).append(page)

    sorted_levels = sorted(level_counts.keys())
    item_level = sorted_levels[0]

    # Walk from shallowest to deepest, pick the first level with 3+ entries
    for level in sorted_levels:
        if len(level_counts[level]) >= 3:
            item_level = level
            break

    # If shallowest has 3+ entries AND has children, it's the item level
    # (e.g. Burleson: 5 L1 items with L2 exhibits)
    # If shallowest has 1-2 entries, those are document/section containers.
    # Check if their children look like individual pages/slides (many L2 entries)
    # or like actual items (moderate number of L2 entries).
    # Document bundles (Alhambra: 2 L1 docs with 32 L2 slides) should keep L1 as items.
    # Walk from shallowest: if a level has few entries (<=5) but the next
    # level has significantly more, the current level is a section container
    # and the next level holds the actual items.
    # Exception: if the next level has 8x+ entries per parent entry,
    # those are likely granular (slides/pages), not items -- stay at parent.
    shallowest = sorted_levels[0]
    item_level = shallowest

    for i, level in enumerate(sorted_levels):
        count = len(level_counts[level])
        if i + 1 < len(sorted_levels):
            next_level = sorted_levels[i + 1]
            next_pages = level_counts[next_level]
            next_count = len(next_pages)

            if count <= 5 and next_count > count:
                # Current level has few entries, next has more.
                # Decide: are the next-level entries real items (multi-page)
                # or slides/pages (one per page)?
                next_span = max(next_pages) - min(next_pages) if next_pages else 0
                avg_gap = next_span / max(next_count, 1)

                if avg_gap >= 2.0:
                    # Next level entries span multiple pages each --
                    # these are real items with content, not slides
                    # (e.g. Martinez: 72 L2 items, avg 4.8 pages each)
                    continue
                else:
                    # Next level is one-per-page (slides, presentation pages)
                    # Current level is the right grouping
                    # (e.g. Alhambra: 32 L2 slides, ~1 page each)
                    item_level = level
                    break
            else:
                # Current level has enough entries -- use it
                item_level = level
                break
        else:
            # Deepest level -- use it
            item_level = level

    child_levels = [level for level in level_counts if level > item_level]

    # Build items from entries at the item level
    raw_item_entries = [
        (title, page)
        for level, title, page in entries
        if level == item_level
    ]
    child_entries = [
        (level, title, page)
        for level, title, page in entries
        if level in child_levels
    ]

    # Group flat TOC entries that share the same item number prefix.
    # e.g. Portola Valley: multiple L1 entries "Item 7.a - Cover Page",
    # "Item 7.a - Minutes" should become one item with child memos.
    # First entry becomes the item, subsequent entries become synthetic children.
    #
    # Also handles "Item N_..." / "Att. N_..." pattern (Hillsborough ADRB):
    # items and their attachments are at the same TOC level, with attachment
    # entries following the item entry they belong to.
    _ATT_PREFIX_RE = re.compile(r'^Att(?:achment)?\.?\s*\d', re.IGNORECASE)

    item_entries = []
    _synthetic_children: Dict[int, List[Tuple[str, int]]] = {}  # index -> [(title, page)]
    if raw_item_entries:
        seen_numbers: Dict[str, int] = {}  # number -> index in item_entries
        last_real_item_idx: Optional[int] = None

        for title, page in raw_item_entries:
            # Attachment entries ("Att. 1_...", "Attachment 2_...") get folded
            # into the most recent real item as synthetic children.
            if _ATT_PREFIX_RE.match(title) and last_real_item_idx is not None:
                _synthetic_children.setdefault(last_real_item_idx, []).append((title, page))
                continue

            number, _ = _extract_item_number_permissive(title)
            if number and number in seen_numbers:
                idx = seen_numbers[number]
                _synthetic_children.setdefault(idx, []).append((title, page))
            else:
                idx = len(item_entries)
                item_entries.append((title, page))
                if number:
                    seen_numbers[number] = idx
                last_real_item_idx = idx

    # Determine agenda vs content pages:
    # Agenda pages are where multiple item-level entries cluster on the same page
    page_density = {}
    for title, page in item_entries:
        page_density[page] = page_density.get(page, 0) + 1
    agenda_pages = {p for p, count in page_density.items() if count >= 2}

    # If no clear agenda page cluster, treat first entry's page as agenda
    if not agenda_pages and item_entries:
        agenda_pages = {item_entries[0][1]}

    current_section = ""

    for i, (title, page) in enumerate(item_entries):
        # Compute page range for this item (account for synthetic children)
        # The page range extends to cover all synthetic children too
        synth = _synthetic_children.get(i, [])
        page_0 = page - 1  # 0-indexed
        if i + 1 < len(item_entries):
            next_page_0 = item_entries[i + 1][1] - 1
            page_end_0 = next_page_0 - 1
        else:
            page_end_0 = doc.page_count - 1

        # Ensure non-negative range
        page_end_0 = max(page_end_0, page_0)

        # Extract item number and title
        number, title_text = _extract_item_number_permissive(title)

        # Check if this entry looks like a section header rather than an item
        if _is_section_header(title_text) or _is_section_header(title):
            current_section = _extract_section_name(title)
            continue

        # Find child entries within this item's page range
        item_children = [
            (level, child_title, child_page)
            for level, child_title, child_page in child_entries
            if child_page >= page
            and (
                i + 1 >= len(item_entries)
                or child_page < item_entries[i + 1][1]
            )
        ]

        # Merge synthetic children (same-numbered flat TOC entries) into children
        if synth:
            for st, sp in synth:
                item_children.append((item_level + 1, st, sp))
            item_children.sort(key=lambda x: x[2])

        # Build memos from child entries (these are embedded attachments)
        memos = []
        for j, (cl, ct, cp) in enumerate(item_children):
            cp_0 = cp - 1
            if j + 1 < len(item_children):
                child_end_0 = item_children[j + 1][2] - 2
            else:
                child_end_0 = page_end_0
            child_end_0 = max(child_end_0, cp_0)

            memo = _extract_memo_content(doc, cp_0, child_end_0)
            if not memo.full_text.strip():
                continue
            memos.append(memo)

        # Extract body text from the item's own page (if it's an agenda page)
        body = ""
        if page in agenda_pages:
            body = _extract_body_from_agenda_page(doc, page_0, title)

        # If no child memos but item spans multiple pages, extract as single memo
        if not memos and page_end_0 > page_0 and page not in agenda_pages:
            memo = _extract_memo_content(doc, page_0, page_end_0)
            if memo.full_text.strip():
                memos.append(memo)

        item = _AgendaItem(
            number=number,
            title=title_text,
            section=current_section,
            body=body,
            recommended_action="",
            memos=memos,
            page_start=page,
            page_end=page_end_0 + 1,
        )

        # Extract recommended action from memo if available
        for memo in memos:
            if memo.recommended_action:
                item.recommended_action = memo.recommended_action
                break

        result.items.append(item)

    # Title recovery for packet PDFs whose TOC entries are just
    # "Item 8.1 - Cover Page" / "Item 8.1 - foo.docx". The real human-readable
    # titles live on the agenda summary pages, where the item number prefix
    # (e.g. "8.1", "9.3") is followed by the title text, often wrapped across
    # lines. We scan pages 0..N for numbered prefixes matching our items and
    # splice in the recovered title when the TOC title is missing or just a
    # bookmark artifact ("- Cover Page", attachment filenames).
    if any(_title_is_packet_artifact(it.title) for it in result.items):
        recovered = _recover_item_titles_from_agenda(doc, result.items)
        for it in result.items:
            if it.number and it.number in recovered and _title_is_packet_artifact(it.title):
                it.title = recovered[it.number]

    logger.debug(
        "v2 toc parsed",
        item_count=len(result.items),
        item_level=item_level,
        total_memos=sum(len(it.memos) for it in result.items),
    )


_PACKET_ARTIFACT_RE = re.compile(
    r'^\s*[-\u2013\u2014]?\s*(cover\s*page|'
    r'.*\.(?:docx?|pdf|pptx?|xlsx?))\s*$',
    re.IGNORECASE,
)


def _title_is_packet_artifact(title: str) -> bool:
    """True if this title is a TOC artifact (cover page, attachment filename)."""
    if not title or not title.strip():
        return True
    t = title.strip().lstrip('-\u2013\u2014').strip()
    if not t:
        return True
    return bool(_PACKET_ARTIFACT_RE.match(title))


def _recover_item_titles_from_agenda(doc, items: List[_AgendaItem]) -> Dict[str, str]:
    """Scan agenda summary pages for item-number prefixes and capture titles.

    Returns {number -> title}. Titles are the text immediately after the
    number prefix, wrapped across lines until the next item number or blank
    line.
    """
    wanted = {it.number for it in items if it.number}
    if not wanted:
        return {}

    first_item_page = min(
        (it.page_start for it in items if it.number and it.page_start),
        default=_MAX_AGENDA_PAGES + 1,
    )
    scan_end = min(first_item_page, _MAX_AGENDA_PAGES + 1, doc.page_count)

    agenda_text = []
    for p in range(scan_end):
        agenda_text.append(doc[p].get_text("text"))
    all_lines = "\n".join(agenda_text).split("\n")

    # Numbers matching our item set. Two shapes to handle:
    #   "9.2 REVIEW OF ..."        inline number + title
    #   "9.4\nAWARD OF CONTRACT..."  number alone, title on next line(s)
    num_re_parts = [re.escape(n) for n in sorted(wanted, key=len, reverse=True)]
    prefix_inline_re = re.compile(r'^\s*(' + '|'.join(num_re_parts) + r')\.?\s+(.+)')
    prefix_solo_re = re.compile(r'^\s*(' + '|'.join(num_re_parts) + r')\.?\s*$')
    stop_re = re.compile(r'^\s*\d+(?:\.\d+)?\.?\s*(?:\S.*)?$')

    recovered: Dict[str, str] = {}
    i = 0
    while i < len(all_lines):
        line = all_lines[i]
        m_inline = prefix_inline_re.match(line)
        if m_inline:
            number = m_inline.group(1)
            title_parts = [m_inline.group(2).strip()]
            j = i + 1
        else:
            m_solo = prefix_solo_re.match(line)
            if not m_solo:
                i += 1
                continue
            number = m_solo.group(1)
            title_parts = []
            j = i + 1

        while j < len(all_lines):
            nxt = all_lines[j].strip()
            if not nxt:
                if title_parts:
                    break
                j += 1
                continue
            if stop_re.match(nxt):
                break
            title_parts.append(nxt)
            j += 1
            if sum(len(p) for p in title_parts) > 300:
                break

        title = ' '.join(title_parts).strip()
        title = re.sub(r'\s+', ' ', title).rstrip(' ,.;')
        if title and number not in recovered:
            recovered[number] = title[:250]
        i = max(j, i + 1)

    return recovered


def _extract_body_from_agenda_page(doc, page_0: int, toc_title: str) -> str:
    """Extract body text for an item from its agenda page.

    Looks for the item's title on the page and captures text below it
    until the next item-like break.
    """
    text = doc[page_0].get_text("text")
    lines = text.split("\n")

    # Find the line that best matches the TOC title
    title_lower = toc_title.lower().strip()
    best_idx = -1
    for idx, line in enumerate(lines):
        if title_lower[:30] in line.lower():
            best_idx = idx
            break

    if best_idx < 0:
        return ""

    # Capture lines after the title until a gap or next section
    body_lines = []
    for line in lines[best_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            if body_lines:
                body_lines.append("")
            continue
        body_lines.append(stripped)

    return "\n".join(body_lines).strip()[:2000]


# ---------------------------------------------------------------------------
# Internal parse orchestrator
# ---------------------------------------------------------------------------

def _parse_v2_internal(pdf_path: str, force_method: Optional[str] = None) -> _ParsedAgenda:
    """Core v2 parse logic. Returns internal _ParsedAgenda structure."""
    doc = fitz.open(pdf_path)
    result = _ParsedAgenda()
    result.metadata.page_count = doc.page_count

    # Extract metadata from first page
    if doc.page_count > 0:
        first_page_lines = _extract_page_text_with_positions(doc[0])
        _extract_meeting_metadata(first_page_lines, result.metadata)

    # Detect and dispatch
    if force_method == "toc":
        _parse_toc_v2(doc, result)
    elif force_method == "url":
        _parse_url_v2(doc, result)
    elif force_method == "pageref":
        _parse_pageref_v2(doc, result)
    else:
        path = _detect_parse_path(doc)
        if path == "toc":
            _parse_toc_v2(doc, result)
        elif path == "url":
            _parse_url_v2(doc, result)
        elif path == "url_then_toc":
            _parse_url_v2(doc, result)
            # Packet PDFs where the agenda summary pages have no links but a
            # staff report cover page carries attachment links get misrouted:
            # URL path finds a single cluster ("item 8.1 and its PDFs") and
            # declares victory. If the TOC groups into noticeably more items,
            # prefer it. (Lynwood CA: URL=1 item, TOC grouped=16 items.)
            url_items = list(result.items)
            url_orphans = list(result.orphan_links)
            if len(url_items) < 3:
                result.items = []
                result.orphan_links.clear()
                _parse_toc_v2(doc, result)
                if len(result.items) <= len(url_items):
                    result.items = url_items
                    result.orphan_links = url_orphans
                    result.metadata.parse_method = "v2_url"
        elif path == "pageref":
            _parse_pageref_v2(doc, result)
        # "empty" -> no items, monolithic fallback

    doc.close()
    return result


# ---------------------------------------------------------------------------
# Public API -- same contract as parse_agenda_pdf
# ---------------------------------------------------------------------------

def parse_agenda_pdf_v2(
    pdf_path: str,
    force_method: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a municipal agenda PDF using anchor-first strategy.

    Same input/output contract as parse_agenda_pdf (v1).
    """
    parsed = _parse_v2_internal(pdf_path, force_method=force_method)

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
                if att.url or att.label
            ],
        }

        body_text_parts = []
        if item.memos:
            for memo in item.memos:
                if memo.full_text:
                    body_text_parts.append(memo.full_text)
        if item.body and not body_text_parts:
            body_text_parts.append(item.body)
        elif item.body and body_text_parts:
            body_text_parts.insert(0, item.body)

        if body_text_parts:
            pipeline_item["body_text"] = "\n\n".join(body_text_parts)

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
        "v2 parsed agenda pdf",
        parse_method=parsed.metadata.parse_method,
        item_count=len(pipeline_items),
        orphan_links=len(parsed.orphan_links),
        orphan_memos=len(parsed.orphan_memos),
        page_count=parsed.metadata.page_count,
    )

    return result


# ---------------------------------------------------------------------------
# CLI for comparison testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python -m vendors.adapters.parsers.agenda_chunker_v2 <path_to_pdf> [--json] [--force-toc] [--force-url] [--compare]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    as_json = "--json" in sys.argv
    compare = "--compare" in sys.argv
    force = None
    if "--force-toc" in sys.argv:
        force = "toc"
    elif "--force-url" in sys.argv:
        force = "url"

    result_v2 = parse_agenda_pdf_v2(pdf_path, force_method=force)
    items_v2 = result_v2["items"]
    meta_v2 = result_v2["metadata"]

    if compare:
        from vendors.adapters.parsers.agenda_chunker import parse_agenda_pdf
        result_v1 = parse_agenda_pdf(pdf_path, force_method=force)
        items_v1 = result_v1["items"]
        meta_v1 = result_v1["metadata"]

        print(f"{'=' * 70}")
        print(f"COMPARISON: {pdf_path}")
        print(f"{'=' * 70}")
        print(f"  v1: {len(items_v1):3d} items | method: {meta_v1.get('parse_method', '?')}")
        print(f"  v2: {len(items_v2):3d} items | method: {meta_v2.get('parse_method', '?')}")
        print()

        if items_v1:
            print("  v1 items:")
            for item in items_v1[:10]:
                atts = len(item.get("attachments", []))
                bt = len(item.get("body_text", ""))
                print(f"    {item.get('agenda_number', '?'):<8} {item['title'][:60]}")
                if atts or bt:
                    print(f"             atts={atts} body={bt}ch")
        else:
            print("  v1: (no items)")

        print()

        if items_v2:
            print("  v2 items:")
            for item in items_v2[:10]:
                atts = len(item.get("attachments", []))
                bt = len(item.get("body_text", ""))
                print(f"    {item.get('agenda_number', '?'):<8} {item['title'][:60]}")
                if atts or bt:
                    print(f"             atts={atts} body={bt}ch")
        else:
            print("  v2: (no items)")

    elif as_json:
        print(json.dumps(result_v2, indent=2, ensure_ascii=False))
    else:
        print(f"{'=' * 60}")
        print(f"{meta_v2.get('body_name', '')} | {meta_v2.get('meeting_type', '')} | {meta_v2.get('meeting_date', '')}")
        print(f"{meta_v2.get('page_count', 0)} pages | {len(items_v2)} items | method: {meta_v2.get('parse_method', '?')}")
        print(f"{'=' * 60}")

        for item in items_v2:
            print(f"\n{'_' * 60}")
            section = (item.get("metadata") or {}).get("section", "")
            print(f"[{section}] {item.get('agenda_number', '')}  {item['title']}")
            if item.get("matter_file"):
                print(f"  Matter: {item['matter_file']}")
            rec = (item.get("metadata") or {}).get("recommended_action", "")
            if rec:
                print(f"  Rec. Action: {rec[:120]}{'...' if len(rec) > 120 else ''}")
            memo_count = (item.get("metadata") or {}).get("memo_count", 0)
            if memo_count:
                memo_pages = (item.get("metadata") or {}).get("memo_pages", 0)
                print(f"  Embedded Memos: {memo_count} ({memo_pages} pages)")
            body_text = item.get("body_text", "")
            if body_text:
                preview = body_text[:150].replace('\n', ' ')
                print(f"  Body text: {len(body_text)} chars - {preview}{'...' if len(body_text) > 150 else ''}")
            atts = item.get("attachments", [])
            if atts:
                print(f"  Attachments ({len(atts)}):")
                for att in atts:
                    print(f"    - {att['name']}")
                    print(f"      {att['url']}")

        orphans = result_v2.get("orphan_links", [])
        if orphans:
            print(f"\nORPHAN LINKS ({len(orphans)}):")
            for att in orphans:
                print(f"  {att['name']} -> {att['url']}")
