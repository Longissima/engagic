"""
Municode HTML Parser - Extract agenda items and participation info

Municode's HTML agenda packets have structured sections:
- <section class="agenda-section"> for each agenda section
- <h2 class="section-header"> with section title
- <ul class="agenda-items"> with <li> items
- <ul class="agenda_item_attachments"> for item attachments

Structure example:
    <section class="agenda-section">
        <h2 class="section-header">
            <div class="Section0"><p>ORDINANCES</p></div>
        </h2>
        <ul class="agenda-items">
            <li>
                <div class="Section0"><p>
                    <num>1.</num>
                    <span>Item title text...</span>
                </p></div>
            </li>
            <ul class="agenda_item_attachments">
                <li><a href="...pdf">filename.pdf</a> (0.02 MB)</li>
            </ul>
        </ul>
    </section>
"""

import re
from typing import Dict, Any, List, Optional

from bs4 import BeautifulSoup, Tag

from config import get_logger
from vendors.utils.attachments import classify_attachment_type
from parsing.participation import parse_participation_info

logger = get_logger(__name__).bind(component="vendor")


def parse_html_agenda(html: str) -> Dict[str, Any]:
    """
    Parse Municode HTML agenda packet to extract items and participation info.

    Args:
        html: HTML content from /adaHtmlDocument/index page

    Returns:
        {
            'participation': {...},  # Contact info (email, phone, zoom, etc.)
            'items': [               # Agenda items
                {
                    'vendor_item_id': str,
                    'title': str,
                    'sequence': int,
                    'agenda_number': str,
                    'section': str,
                    'attachments': [{'name': str, 'url': str, 'type': str}]
                }
            ]
        }
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Extract participation info from page text
    page_text = soup.get_text(separator=' ', strip=True)
    participation_info = parse_participation_info(page_text)

    # Extract agenda items from sections
    items = _extract_agenda_items(soup)

    participation = participation_info.model_dump() if participation_info else {}

    logger.debug(
        "parsed municode agenda",
        parser="municode",
        items=len(items),
        participation_fields=list(participation.keys())
    )

    return {
        'participation': participation,
        'items': items,
    }


def _extract_agenda_items(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Extract agenda items from all sections."""
    items = []
    sequence = 0

    sections = soup.find_all('section', class_='agenda-section')
    logger.debug("found agenda sections", parser="municode", count=len(sections))

    for section in sections:
        section_name = _extract_section_name(section)

        # Find agenda items list (some sites use <ol>, e.g. Stonecrest GA)
        agenda_ul = section.find(['ul', 'ol'], class_='agenda-items')
        if not agenda_ul or not isinstance(agenda_ul, Tag):
            continue

        current_item: Optional[Dict[str, Any]] = None

        for child in agenda_ul.children:
            if not isinstance(child, Tag):
                continue

            if child.name == 'li':
                # Start new item
                sequence += 1
                item_data = _extract_item_from_li(child, sequence, section_name)
                if item_data:
                    items.append(item_data)
                    current_item = item_data

            elif child.name == 'ul' and current_item:
                child_class = child.get('class') or []
                if 'agenda_item_attachments' in child_class:
                    attachments = _extract_attachments(child)
                    current_item['attachments'].extend(attachments)

    return items


def _extract_section_name(section: Tag) -> str:
    """Extract section name from header."""
    header = section.find('h2', class_='section-header')
    if header:
        # Get text, clean up whitespace
        text = header.get_text(separator=' ', strip=True)
        # Remove common suffixes and clean
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    return ""


def _extract_item_from_li(li: Tag, sequence: int, section_name: str) -> Optional[Dict[str, Any]]:
    """Extract item data from a <li> element."""
    # Get the full text content
    text = li.get_text(separator=' ', strip=True)

    if not text:
        return None

    # Extract agenda number from <num> element if present
    agenda_number = str(sequence)
    num_elem = li.find('num')
    if num_elem:
        num_text = num_elem.get_text(strip=True)
        # Remove trailing period: "1." -> "1"
        agenda_number = num_text.rstrip('.')

    # Clean title: remove the agenda number prefix if it appears
    title = text
    # Pattern: starts with number and period, e.g., "1. Title..."
    num_prefix_match = re.match(r'^(\d+)\.\s*', title)
    if num_prefix_match:
        title = title[num_prefix_match.end():]

    # Clean up excessive whitespace
    title = re.sub(r'\s+', ' ', title).strip()

    if not title:
        return None

    return {
        'vendor_item_id': f"item_{sequence}",
        'title': title,
        'sequence': sequence,
        'agenda_number': agenda_number,
        'section': section_name,
        'attachments': [],
    }


def _extract_attachments(attachment_ul: Tag) -> List[Dict[str, Any]]:
    """Extract attachments from <ul class="agenda_item_attachments">."""
    attachments = []

    for li in attachment_ul.find_all('li'):
        link = li.find('a', href=True)
        if not link:
            continue

        href_value = link.get('href')
        href = href_value if isinstance(href_value, str) else ''
        name = link.get_text(strip=True)
        if not href or not name:
            continue

        file_type = classify_attachment_type(href, name)

        attachments.append({
            'name': name,
            'url': href,
            'type': file_type,
        })

    return attachments


# Confidence: 7/10
# Tested against Columbus-GA and Tomball-TX HTML samples.
# Structure confirmed: section > agenda-items > li + agenda_item_attachments
# May need adjustments for cities with variant HTML structures.
