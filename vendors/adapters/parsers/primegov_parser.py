"""
PrimeGov HTML Parser - Extract agenda items and participation info

PrimeGov's Portal/Meeting pages have structured HTML with:
- Participation info in the page header
- <div class="agenda-item"> for each item
- <div class="item_contents"> with attachments

Handles two patterns:
1. LA/newer: meeting-item wrapper with matter tracking metadata
2. Palo Alto/older: direct agenda-item divs without matter tracking
"""

import re
from typing import Dict, Any, List, Optional
from bs4 import BeautifulSoup
from parsing.participation import parse_participation_info

from config import get_logger

logger = get_logger(__name__).bind(component="vendor")



def parse_html_agenda(html: str) -> Dict[str, Any]:
    """Parse PrimeGov HTML agenda to extract items and participation info."""
    soup = BeautifulSoup(html, 'html.parser')

    # Extract participation info from page text (before agenda items)
    page_text = soup.get_text()
    participation_info = parse_participation_info(page_text)

    # Extract agenda items
    items, html_pattern = _extract_agenda_items(soup)

    # Get field names for logging
    participation_fields = list(participation_info.model_dump(exclude_none=True).keys()) if participation_info else []
    logger.debug(
        "extracted agenda items",
        parser="primegov",
        item_count=len(items),
        participation_fields=participation_fields
    )

    # Convert to dict for return, defaulting to empty dict if None
    participation = participation_info.model_dump() if participation_info else {}

    return {
        'participation': participation,
        'items': items,
        'html_pattern': html_pattern,
    }


def _extract_agenda_items(soup: BeautifulSoup) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Extract agenda items from HTML (LA, Palo Alto, or Boulder patterns).

    Returns (items, html_pattern) — the pattern name rides the extract
    audit into processing_metadata so dialect drift is queryable in prod.
    """
    items = []

    # Try newer pattern first (LA): meeting-item wrappers
    meeting_items = soup.find_all('div', class_='meeting-item')

    if meeting_items:
        logger.debug("found meeting-item divs (LA pattern)", parser="primegov", item_count=len(meeting_items))
        for sequence, meeting_item_div in enumerate(meeting_items, 1):
            item_dict = _extract_la_pattern_item(meeting_item_div, soup, sequence)
            if item_dict:
                items.append(item_dict)
        return items, "primegov_la"

    # Try Palo Alto pattern: direct agenda-item divs
    agenda_items = soup.find_all('div', class_='agenda-item')
    if agenda_items:
        logger.debug("found agenda-item divs (Palo Alto pattern)", parser="primegov", item_count=len(agenda_items))
        for sequence, item_div in enumerate(agenda_items, 1):
            item_dict = _extract_palo_alto_pattern_item(item_div, soup, sequence)
            if item_dict:
                items.append(item_dict)
        return items, "primegov_palo_alto"

    # Fallback to Boulder pattern: table elements with data-itemid
    table_items = soup.find_all('table', attrs={'data-itemid': True})
    if table_items:
        logger.debug("found table items (Boulder pattern)", parser="primegov", item_count=len(table_items))
        for sequence, table in enumerate(table_items, 1):
            item_dict = _extract_boulder_pattern_item(table, soup, sequence)
            if item_dict:
                items.append(item_dict)
        return items, "primegov_boulder"

    return items, None


def _extract_la_pattern_item(meeting_item_div, soup: BeautifulSoup, sequence: int) -> Optional[Dict[str, Any]]:
    """Extract item from LA pattern (meeting-item wrapper with matter tracking)."""
    # Extract metadata from meeting-item div
    item_id = meeting_item_div.get('data-itemid')
    matter_id = meeting_item_div.get('data-mig')  # Matter GUID

    if not item_id:
        logger.debug("meeting-item missing data-itemid, skipping", parser="primegov", pattern="LA")
        return None

    # Find agenda-item div inside
    agenda_item_div = meeting_item_div.find('div', class_='agenda-item')
    if not agenda_item_div:
        logger.debug("item missing agenda-item div, skipping", parser="primegov", pattern="LA", item_id=item_id)
        return None

    # Extract matter metadata from forcepopulate table
    matter_file = None
    matter_type = None
    title = None

    forcepopulate_table = agenda_item_div.find('table', class_='forcepopulate')
    if forcepopulate_table:
        rows = forcepopulate_table.find_all('tr')

        # First row: matter_file (colspan=2)
        if rows:
            first_cell = rows[0].find('td', colspan='2')
            if first_cell:
                matter_file = first_cell.get_text(strip=True) or None

        # Second row: matter_type (first cell) and title (second cell)
        if len(rows) > 1:
            cells = rows[1].find_all('td')
            if len(cells) >= 2:
                matter_type_text = cells[0].get_text(strip=True)
                if matter_type_text:
                    matter_type = matter_type_text

                # Title is in second cell (may have nested divs)
                title_cell = cells[1]
                title = title_cell.get_text(separator=' ', strip=True)

    # Fallback title: get all text from agenda-item
    if not title:
        title = agenda_item_div.get_text(separator=' ', strip=True)

    # Extract agenda number from number-cell
    agenda_number = None
    parent_table = meeting_item_div.find('table', class_='item-table')
    if parent_table:
        number_cell = parent_table.find('td', class_='number-cell')
        if number_cell:
            agenda_number = number_cell.get_text(strip=True).strip('()')

    # Extract attachments from content area
    contents_id = f"agenda_item_area_{item_id}"
    contents_div = soup.find('div', id=contents_id)
    attachments = []
    if contents_div:
        attachments = _extract_attachments(contents_div, item_id)

    item_dict = {
        'vendor_item_id': str(item_id),
        'title': title,
        'sequence': sequence,
        'attachments': attachments,
    }

    # Add matter tracking fields if available
    if matter_id:
        item_dict['matter_id'] = matter_id
    if matter_file:
        item_dict['matter_file'] = matter_file
    if matter_type:
        item_dict['matter_type'] = matter_type
    if agenda_number:
        item_dict['agenda_number'] = agenda_number

    logger.debug(
        "parsed LA pattern item",
        parser="primegov",
        pattern="LA",
        item_id=item_id,
        matter_file=matter_file,
        matter_type=matter_type,
        title_preview=title[:60] if title else ''
    )

    return item_dict


def _extract_palo_alto_pattern_item(item_div, soup: BeautifulSoup, sequence: int) -> Optional[Dict[str, Any]]:
    """Extract item from Palo Alto pattern (direct agenda-item divs)."""
    # Get item ID from div (format: "AgendaItem_12345")
    item_full_id = item_div.get('id', '')
    if not item_full_id:
        logger.warning("agenda item has no ID, skipping", parser="primegov", pattern="PaloAlto", sequence=sequence)
        return None

    # Extract numeric ID
    item_id = item_full_id.replace('AgendaItem_', '')

    # Get title from text content
    title = item_div.get_text(strip=True)

    # Find corresponding item_contents div
    contents_id = f"agenda_item_area_{item_id}"
    contents_div = soup.find('div', id=contents_id)

    # Extract attachments
    attachments = []
    if contents_div:
        attachments = _extract_attachments(contents_div, item_id)

    item_dict = {
        'vendor_item_id': item_id,
        'title': title,
        'sequence': sequence,
        'attachments': attachments,
    }

    logger.debug(
        "parsed PaloAlto pattern item",
        parser="primegov",
        pattern="PaloAlto",
        sequence=sequence,
        title_preview=title[:60] if title else '',
        attachments=len(attachments)
    )

    return item_dict


def _extract_boulder_pattern_item(table, soup: BeautifulSoup, sequence: int) -> Optional[Dict[str, Any]]:
    """Extract item from Boulder pattern (table elements with data-itemid)."""
    item_id = table.get('data-itemid')
    if not item_id:
        return None

    # Extract title from the third cell (index 2) which usually has class agenda-item
    # or is the main content cell
    cells = table.find_all('td')
    title = None
    agenda_number = None

    for i, cell in enumerate(cells):
        cell_classes = cell.get('class', [])
        cell_text = cell.get_text(strip=True)

        # Skip empty cells and attachment/button cells
        if not cell_text or 'attachmentCell' in cell_classes or 'optionalButtonsCell' in cell_classes:
            continue

        # Short text like "A.", "B.", "1." is likely the agenda number
        if len(cell_text) <= 3 and (cell_text.endswith('.') or cell_text.isdigit()):
            agenda_number = cell_text.rstrip('.')
            continue

        # Otherwise it's the title
        if not title:
            title = cell_text

    if not title:
        logger.debug("table item has no title, skipping", parser="primegov", pattern="Boulder", item_id=item_id)
        return None

    # Find corresponding item_contents div for attachments
    contents_id = f"agenda_item_area_{item_id}"
    contents_div = soup.find('div', id=contents_id)
    attachments = []
    if contents_div:
        attachments = _extract_attachments(contents_div, item_id)

    item_dict = {
        'vendor_item_id': str(item_id),
        'title': title,
        'sequence': sequence,
        'attachments': attachments,
    }

    if agenda_number:
        item_dict['agenda_number'] = agenda_number

    logger.debug(
        "parsed Boulder pattern item",
        parser="primegov",
        pattern="Boulder",
        item_id=item_id,
        title_preview=title[:60] if title else '',
        attachments=len(attachments)
    )

    return item_dict


def _extract_attachments(contents_div, item_id: str) -> List[Dict[str, Any]]:
    """Extract attachment links from item_contents div."""
    attachments = []

    # Find all links in the contents
    links = contents_div.find_all('a', href=True)

    for link in links:
        href = link['href']

        # Look for attachment API endpoint
        if 'historyattachment' in href.lower():
            # Extract historyId from URL
            history_id_match = re.search(r'historyId=([a-f0-9\-]+)', href, re.IGNORECASE)
            if history_id_match:
                history_id = history_id_match.group(1)

                # Get link text as attachment name
                name = link.get_text(strip=True)
                if not name:
                    name = f"Attachment {len(attachments) + 1}"

                attachments.append({
                    'name': name,
                    'history_id': history_id,
                    'url': href,
                })

    return attachments
