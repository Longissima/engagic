"""
NovusAgenda HTML Parser - Extract agenda items from MeetingView pages

NovusAgenda structure:
- Items in table grid (can vary by implementation)
- Each item may have link to CoverSheet.aspx with ItemID
- Attachments may be embedded or linked
"""

import re
from typing import Dict, Any
from bs4 import BeautifulSoup

from config import get_logger

logger = get_logger(__name__).bind(component="vendor")



def parse_html_agenda(html: str) -> Dict[str, Any]:
    """
    Parse NovusAgenda MeetingView HTML to extract items and attachments.

    Args:
        html: HTML content from MeetingView.aspx or similar agenda page

    Returns:
        {
            'participation': {},  # NovusAgenda doesn't have structured participation
            'items': [
                {
                    'vendor_item_id': str,
                    'title': str,
                    'sequence': int,
                    'attachments': [{'name': str, 'url': str, 'type': str}]
                }
            ]
        }
    """
    soup = BeautifulSoup(html, 'html.parser')
    items = []

    # Try to find items grid (pattern varies, log what we find)
    # Look for common patterns: tables, divs with item classes, etc.

    # Log page structure for debugging
    logger.debug("parsing NovusAgenda HTML", parser="novusagenda", html_length=len(html))

    # Pattern 1: Look for links to CoverSheet.aspx (item detail pages)
    # Note: NovusAgenda uses "CoverSheet" with both C and S capitalized
    coversheet_links = soup.find_all('a', href=re.compile(r'CoverSheet\.aspx\?ItemID=', re.IGNORECASE))

    if coversheet_links:
        logger.info("found coversheet links", parser="novusagenda", link_count=len(coversheet_links))

        for sequence, link in enumerate(coversheet_links, 1):
            # Extract ItemID from href
            href_value = link.get('href')
            href = href_value if isinstance(href_value, str) else ''
            item_id_match = re.search(r'ItemID=(\d+)', href)
            if not item_id_match:
                continue

            item_id = item_id_match.group(1)

            # Get title from link text or parent context
            title = link.get_text(strip=True)
            if not title:
                # Try parent td or container
                parent_td = link.find_parent('td')
                if parent_td:
                    title = parent_td.get_text(strip=True)

            items.append({
                'vendor_item_id': item_id,
                'title': title,
                'sequence': sequence,
                'attachments': [],  # Will be populated if we fetch Coversheet page
            })

    # Pattern 2: Look for agenda item tables or divs
    # Try finding tables with agenda item data
    agenda_tables = soup.find_all('table', class_=re.compile(r'agenda', re.I))
    if agenda_tables:
        logger.info("found agenda tables", parser="novusagenda", table_count=len(agenda_tables))

    # Pattern 3: Look for PDF links that might be attachments
    pdf_links = soup.find_all('a', href=re.compile(r'\.pdf$|DisplayAgendaPDF', re.I))
    if pdf_links:
        logger.info("found PDF links", parser="novusagenda", pdf_count=len(pdf_links))

    # Pattern 4: Look for "Online Agenda" / "HTML Agenda" / "View Agenda" links
    agenda_view_links = soup.find_all('a', text=re.compile(r'(online|html|view).*agenda', re.I))
    if agenda_view_links:
        logger.info("found agenda view links", parser="novusagenda", link_count=len(agenda_view_links))
        for link in agenda_view_links:
            logger.debug("agenda view link", parser="novusagenda", link_action=link.get('onClick', link.get('href', 'no href')))

    # Pattern 5: Look for image-based agenda links (common pattern)
    img_links = soup.find_all('img', alt=re.compile(r'agenda|item', re.I))
    if img_links:
        logger.info("found agenda/item images", parser="novusagenda", image_count=len(img_links))
        for img in img_links[:5]:  # Log first 5
            logger.debug("image alt text", parser="novusagenda", alt_text=img.get('alt'))

    logger.info("extracted items from HTML", parser="novusagenda", item_count=len(items))

    return {
        'participation': {},
        'items': items,
    }


# Confidence: 5/10
# NovusAgenda HTML structure varies significantly by city
# This implementation is exploratory and logs various patterns
# Needs per-city testing and refinement
