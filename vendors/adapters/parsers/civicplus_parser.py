"""
CivicPlus HTML Agenda Parser - Extract structured agenda items from CivicPlus HTML agendas.

CivicPlus AgendaCenter provides HTML agendas at:
    /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-ID?html=true

HTML structure:
    div.item.level1  → top-level section headers (CONSENT AGENDA, PUBLIC HEARING, etc.)
    div.item.level2  → agenda items or sub-section headers
    div.item.level3  → sub-items (Resolution 1, Agreement 1, etc.)

Each item div contains:
    .bullet span     → agenda number (9.A., 10.B.1., etc.)
    .title           → item title (may be generic like "Consent A")
    .desc            → description text (often the real substance)
    .documents a.file → attachment links
"""

import re
from typing import Dict, Any, List
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import get_logger
from vendors.utils.attachments import classify_attachment_type as _attachment_type

logger = get_logger(__name__).bind(component="vendor")


# Generic title patterns — when matched, use description as the real title
GENERIC_TITLE_RE = re.compile(
    r'^(?:Consent|Resolution|Agreement|Purchase|Public Hearing|Presentation|Item)\s+[A-Z0-9]+$',
    re.IGNORECASE
)

# Procedural items that should be skipped as section headers
PROCEDURAL_TITLES = {
    'call to order', 'invocation', 'pledge of allegiance', 'roll call',
    'adjournment', 'adjourn', 'recess', 'reconvene',
}


def _get_item_level(item_div) -> int:
    """Extract nesting level from CSS class (level1, level2, level3)."""
    for cls in item_div.get('class', []):
        if cls.startswith('level') and cls[5:].isdigit():
            return int(cls[5:])
    return 1


def _has_substantive_desc(desc_div) -> bool:
    """Check if a .desc div has real content beyond just <br> tags."""
    if not desc_div:
        return False
    text = desc_div.get_text(strip=True)
    return bool(text)


def _first_paragraph_text(desc_div) -> str:
    """Extract the first <p> tag's text from a .desc div."""
    if not desc_div:
        return ""
    first_p = desc_div.find('p')
    if first_p:
        return first_p.get_text(strip=True)
    return desc_div.get_text(strip=True)


def _full_desc_text(desc_div) -> str:
    """Extract all text from a .desc div."""
    if not desc_div:
        return ""
    return desc_div.get_text(separator='\n', strip=True)


def parse_civicplus_html(html: str, base_url: str) -> Dict[str, Any]:
    """
    Parse CivicPlus HTML agenda into pipeline-compatible item dicts.

    Args:
        html: HTML content from /AgendaCenter/ViewFile/Agenda/...?html=true
        base_url: Base URL for resolving relative links (e.g., https://ok-ardmore.civicplus.com)

    Returns:
        {"items": [...]} matching AgendaItemSchema format.
    """
    soup = BeautifulSoup(html, 'html.parser')
    items_container = soup.find('div', id='divItems')
    if not items_container:
        # Fallback: search entire document
        items_container = soup

    pipeline_items: List[Dict[str, Any]] = []
    section_stack: List[str] = []  # [level1_section, level2_subsection]
    sequence = 0

    all_item_divs = items_container.find_all('div', class_='item', recursive=False if items_container.name == 'div' else True)

    # Detect flat layout: all items are level1, no level2+.
    # In flat layouts, level1 items ARE the substantive items (Dothan, etc.).
    levels_present = {_get_item_level(d) for d in all_item_divs}
    is_flat_layout = levels_present == {1}

    for item_div in all_item_divs:
        level = _get_item_level(item_div)

        # Extract components
        bullet_span = item_div.select_one('.bullet span')
        agenda_number = bullet_span.get_text(strip=True).rstrip('.') if bullet_span else ""

        title_el = item_div.select_one('.title')
        raw_title = title_el.get_text(strip=True) if title_el else ""

        desc_div = item_div.select_one('.desc')
        has_desc = _has_substantive_desc(desc_div)

        # Extract attachments
        attachments: List[Dict[str, Any]] = []
        for link in item_div.select('.documents a.file'):
            href_value = link.get('href')
            href = href_value if isinstance(href_value, str) else ''
            if not href:
                continue
            name = link.get_text(strip=True)
            attachments.append({
                'name': name or 'Attachment',
                'url': urljoin(base_url, href),
                'type': _attachment_type(href),
            })

        # Determine if this is a section header or a substantive item
        is_section_header = False

        if is_flat_layout:
            # Flat layout: all level1. Skip only procedural items.
            if raw_title.lower().rstrip(':') in PROCEDURAL_TITLES:
                continue
        elif level == 1:
            # Hierarchical layout: level1 items are section headers.
            is_section_header = True
        # Level 2 items without desc or attachments are sub-section headers
        # (e.g., "RESOLUTION(S)", "AGREEMENT(S)", "PURCHASE(S)")
        elif level == 2 and not has_desc and not attachments:
            is_section_header = True

        if is_section_header:
            if level == 1:
                section_stack = [raw_title]
            elif level == 2:
                # Keep level1 section, replace level2
                section_stack = section_stack[:1] + [raw_title]
            continue

        # --- Substantive item ---
        sequence += 1

        # Build effective title: if generic, use description instead
        if GENERIC_TITLE_RE.match(raw_title) and has_desc:
            title = _first_paragraph_text(desc_div)
        else:
            title = raw_title

        if not title:
            title = raw_title or f"Item {agenda_number}"

        # Build body_text from full description
        body_text = _full_desc_text(desc_div) if has_desc else None

        # Build section string from stack
        section = " > ".join(section_stack) if section_stack else ""

        pipeline_item: Dict[str, Any] = {
            'vendor_item_id': agenda_number,
            'title': title,
            'sequence': sequence,
            'agenda_number': agenda_number,
            'attachments': attachments,
        }

        if body_text:
            pipeline_item['body_text'] = body_text

        item_metadata: Dict[str, Any] = {}
        if section:
            item_metadata['section'] = section
        if item_metadata:
            pipeline_item['metadata'] = item_metadata

        pipeline_items.append(pipeline_item)

    logger.debug(
        "parsed civicplus html agenda",
        vendor="civicplus",
        item_count=len(pipeline_items),
    )

    return {
        'items': pipeline_items,
        'html_pattern': 'civicplus_flat' if is_flat_layout else 'civicplus_hierarchical',
    }
