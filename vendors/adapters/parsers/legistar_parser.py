"""
Legistar HTML Parser - Extract attachments and agenda items from Legistar HTML pages

Three main functions:
1. parse_legislation_attachments() - Extract attachments from LegislationDetail.aspx
2. parse_html_agenda() - Extract agenda items from MeetingDetail.aspx (HTML fallback)
3. parse_aada_html() - Extract agenda items from AADA (Accessible Agenda) HTML pages
"""

import re
from typing import Dict, Any, List
from urllib.parse import urljoin
from vendors.utils.attachments import classify_attachment_type
from bs4 import BeautifulSoup

from config import get_logger

logger = get_logger(__name__).bind(component="vendor")



def parse_legislation_attachments(html: str, base_url: str) -> List[Dict[str, Any]]:
    """
    Parse attachments from Legistar LegislationDetail.aspx page.

    Uses multiple fallback strategies for resilience:
    1. Primary: Exact ASP.NET control IDs (current Legistar implementation)
    2. Fallback: Structural patterns (class-based, text-based)
    3. Last resort: Any PDF/doc links on the page

    Args:
        html: HTML content from LegislationDetail.aspx
        base_url: Base URL for building absolute URLs

    Returns:
        List of attachment dictionaries: [{'name': str, 'url': str, 'type': str}]
    """


    soup = BeautifulSoup(html, 'html.parser')
    attachments = []
    links = None

    # Strategy 1: Primary - exact ASP.NET control IDs
    attachments_table = soup.find('table', id='ctl00_ContentPlaceHolder1_tblAttachments')
    if attachments_table:
        attachments_span = attachments_table.find('span', id='ctl00_ContentPlaceHolder1_lblAttachments2')
        if attachments_span:
            links = attachments_span.find_all('a', href=True)
            logger.debug("found attachments via primary selector", parser="legistar")

    # Strategy 2: Fallback - look for "Attachments" label and nearby View.ashx links
    if not links:
        attachments_label = soup.find(string=re.compile(r'Attachments?', re.IGNORECASE))
        if attachments_label:
            parent = attachments_label.find_parent(['td', 'div', 'span', 'tr'])
            if parent:
                container = parent.find_parent(['table', 'div'])
                if container:
                    # Only match Legistar View.ashx pattern, not arbitrary PDFs
                    links = container.find_all('a', href=re.compile(r'View\.ashx\?', re.IGNORECASE))
                    if links:
                        logger.warning("using fallback selector for attachments", parser="legistar")

    if not links:
        logger.debug("no attachments found", parser="legistar")
        return attachments

    for link in links:
        href_value = link.get('href')
        href = href_value if isinstance(href_value, str) else ''
        name = link.get_text(strip=True)

        if not href or not name:
            continue

        # Build absolute URL
        attachment_url = urljoin(base_url, href)

        file_type = classify_attachment_type(attachment_url, name)
        # Legistar View.ashx links are almost always PDFs
        if file_type == 'unknown':
            file_type = 'pdf'

        attachments.append({
            'name': name,
            'url': attachment_url,
            'type': file_type,
        })

    logger.debug("found attachments", parser="legistar", attachment_count=len(attachments))

    return attachments


def parse_novusagenda_html_agenda(html: str) -> Dict[str, Any]:
    """
    Parse NovusAgenda MeetingView HTML to extract items and attachments.

    NovusAgenda structure:
    - Items in table grid (can vary by implementation)
    - Each item may have link to CoverSheet.aspx with ItemID
    - Attachments may be embedded or linked

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

    logger.info(
        f"[HTMLParser:NovusAgenda] Extracted {len(items)} items from HTML"
    )

    return {
        'participation': {},
        'items': items,
    }


def parse_html_agenda(html: str, meeting_id: str, base_url: str) -> Dict[str, Any]:
    """
    Parse Legistar MeetingDetail HTML to extract items.

    Legistar structure:
    - Items in Telerik RadGrid (table.rgMasterTable)
    - Rows with class rgRow or rgAltRow
    - Columns: File #, Ver., Agenda #, Name, Type, Status, Title, Action, Result, Action Details, Video
    - File # links to LegislationDetail.aspx with ID parameter for potential attachment fetching

    Args:
        html: HTML content from MeetingDetail.aspx page
        meeting_id: Meeting ID for generating item IDs
        base_url: Base URL for building absolute URLs

    Returns:
        {
            'participation': {},  # Legistar HTML doesn't have structured participation in detail page
            'items': [
                {
                    'vendor_item_id': str,  # Legislation ID from File # link
                    'title': str,           # Full title from Title column
                    'sequence': int,        # Row number
                    'item_type': str,       # Type column (Ordinance, Resolution, etc.)
                    'status': str,          # Status column
                    'file_number': str,     # File # text
                    'attachments': []       # Empty for now, could fetch from LegislationDetail later
                }
            ]
        }
    """


    soup = BeautifulSoup(html, 'html.parser')
    items = []

    # Find the RadGrid master table
    master_table = soup.find('table', class_='rgMasterTable')

    if not master_table:
        logger.debug("[HTMLParser:Legistar] No rgMasterTable found in HTML")
        return {'participation': {}, 'items': []}

    # Build column map from header row (different Legistar sites have different columns)
    # Header row may have class='rgHeader' or no class (just first tr with th elements)
    header_row = master_table.find('tr', class_='rgHeader')
    if not header_row:
        # Try finding first tr with th elements
        header_row = master_table.find('tr')
        if header_row and not header_row.find_all('th'):
            header_row = None

    column_map = {}
    if header_row:
        headers = header_row.find_all('th')
        for i, th in enumerate(headers):
            # Normalize header text (remove nbsp, lowercase)
            header_text = th.get_text(strip=True).replace('\xa0', ' ').lower()
            column_map[header_text] = i
        logger.debug("column map created", parser="legistar", column_map=column_map)
    else:
        # Fallback: infer layout from column count in first data row
        first_row = master_table.find('tr', class_=['rgRow', 'rgAltRow'])
        cell_count = len(first_row.find_all('td')) if first_row else 0
        if cell_count <= 5:
            # Slim layout (e.g. Riverside): File #, Ver., Title, Video
            logger.debug("using slim column layout", parser="legistar", cell_count=cell_count)
            column_map = {'file #': 0, 'ver.': 1, 'title': 2}
        else:
            logger.warning("[HTMLParser:Legistar] No header row found, using default column positions")
            column_map = {
                'file #': 0, 'ver.': 1, 'agenda #': 2, 'name': 3,
                'type': 4, 'status': 5, 'title': 6, 'action': 7,
                'result': 8, 'action details': 9, 'video': 10
            }

    # Find all agenda item rows (rgRow and rgAltRow)
    rows = master_table.find_all('tr', class_=['rgRow', 'rgAltRow'])

    if not rows:
        logger.debug("[HTMLParser:Legistar] No rgRow/rgAltRow rows found")
        return {'participation': {}, 'items': []}

    logger.debug("found RadGrid rows", parser="legistar", row_count=len(rows))

    for sequence, row in enumerate(rows, 1):
        try:
            cells = row.find_all('td')

            if len(cells) < 3:
                logger.debug("row has too few cells, skipping", parser="legistar", row=sequence, cell_count=len(cells))
                continue

            # Extract File # and legislation ID (always column 0)
            file_cell = cells[0]
            file_link = file_cell.find(
                'a',
                href=lambda value: isinstance(value, str)
                and 'LegislationDetail.aspx' in value,
            )

            if not file_link:
                logger.debug("row has no LegislationDetail link, skipping", parser="legistar", row=sequence)
                continue

            file_number = file_link.get_text(strip=True)

            # Extract legislation ID from URL (ID=7494673)
            href_value = file_link.get('href')
            href = href_value if isinstance(href_value, str) else ''
            legislation_id_match = re.search(r'ID=(\d+)', href)
            legislation_id = legislation_id_match.group(1) if legislation_id_match else None

            if not legislation_id:
                logger.debug("row has no ID in link, skipping", parser="legistar", row=sequence)
                continue

            # Extract fields using column map
            def get_cell_text(col_name: str) -> str:
                idx = column_map.get(col_name.lower())
                if idx is not None and idx < len(cells):
                    return cells[idx].get_text(strip=True)
                return ''

            version = get_cell_text('ver.')
            agenda_number = get_cell_text('agenda #')
            name = get_cell_text('name')
            item_type = get_cell_text('type')
            status = get_cell_text('status')  # May not exist in all layouts (e.g., Philadelphia)
            title = get_cell_text('title')

            # Use full title if available, otherwise fall back to name
            item_title = title if title else name

            if not item_title:
                logger.debug("row has no title, skipping", parser="legistar", row=sequence)
                continue

            # Build full legislation detail URL for potential future attachment fetching
            legislation_url = urljoin(base_url, href) if href else None

            item_data = {
                'vendor_item_id': legislation_id,
                'title': item_title,
                'sequence': sequence,
                'file_number': file_number,
                'matter_file': file_number,  # For matter tracking (e.g., "251041", "BL2025-1234")
                'matter_id': legislation_id,  # Legislation ID as matter_id
                'item_type': item_type,
                'attachments': [],  # Could fetch from LegislationDetail.aspx later
            }

            # Optional fields
            if version:
                item_data['version'] = version
            if agenda_number:
                item_data['agenda_number'] = agenda_number
            if status:
                item_data['status'] = status
            if legislation_url:
                item_data['legislation_url'] = legislation_url

            items.append(item_data)

            logger.debug(
                f"[HTMLParser:Legistar] Item {sequence}: File #{file_number} - '{item_title[:60]}...'"
            )

        except Exception as e:
            logger.warning(
                f"[HTMLParser:Legistar] Error parsing row {sequence}: {e}"
            )
            continue

    logger.info(
        f"[HTMLParser:Legistar] Extracted {len(items)} items from meeting {meeting_id}"
    )

    return {
        'participation': {},
        'items': items,
    }


# Confidence: 8/10
# Works with PrimeGov's current HTML structure.
# May need adjustments if they change class names or div IDs.


# Confidence: 7/10
# Tested against LA County AADA pages. The CSS-positioned div structure
# varies by Legistar instance; other AADA pages may need adjustments.
_ITEM_NUM_RE = re.compile(r'^(\d+)\.\s*$')
_MATTER_ID_RE = re.compile(r'\((\d{2}-\d{4,})\)')


def parse_aada_html(html: str, meeting_id: str, base_url: str) -> Dict[str, Any]:
    """
    Parse Legistar AADA (Accessible Agenda) HTML page to extract agenda items.

    AADA pages are CSS-positioned HTML renderings of agenda PDFs, used by
    jurisdictions like LA County where MeetingDetail.aspx is not publicly viewable.

    Structure: divs with CSS absolute positioning, items identified by numbered
    bold text (1., 2., 3...), matter IDs in parens like (26-2275), and attachment
    links (often to file.lacounty.gov PDFs).
    """
    soup = BeautifulSoup(html, 'html.parser')

    # Collect all relevant elements in document order.
    # AADA pages use spans with stl_XX classes for formatting; bold item numbers
    # use a different class than body text. We look for spans/divs containing
    # text and any anchor links.
    all_spans = soup.find_all('span')

    # First pass: find item number positions by scanning spans for "N." pattern.
    # Bold item numbers appear in specific spans (typically stl_17 on LA County).
    # We detect the bold class dynamically: the class used for the first "1." span.
    item_boundaries = []  # (sequence_number, span_element)
    bold_class = None

    for span in all_spans:
        text = span.get_text(strip=True)
        match = _ITEM_NUM_RE.match(text)
        if not match:
            continue

        # Confirm this looks like an item number by checking if it shares a class
        # with known bold/title elements, or if we haven't locked in a bold class yet
        class_value = span.get('class')
        span_classes = (
            [value for value in class_value if isinstance(value, str)]
            if isinstance(class_value, list)
            else []
        )
        if bold_class is None:
            # First item number found -- lock in its class as the bold class
            bold_class = span_classes[0] if span_classes else None
            item_boundaries.append((int(match.group(1)), span))
        elif bold_class in span_classes:
            item_boundaries.append((int(match.group(1)), span))

    if not item_boundaries:
        logger.debug("no item numbers found in AADA HTML", meeting_id=meeting_id)
        return {'participation': {}, 'items': []}

    # Get the title class: bold_class is used for both item numbers and titles.
    # Body text uses a different class (typically stl_18).

    # Build a position-ordered list of all spans and links for slicing.
    # BeautifulSoup iterates in document order, so we use source position.
    # Use find_all instead of descendants to get only top-level span/a elements,
    # and check get_text() instead of .string (which returns None for mixed content).
    all_elements = []
    for elem in soup.find_all(['span', 'a']):
        if elem.name == 'span':
            # Skip parent spans that contain child spans (avoid double-counting)
            if elem.find('span'):
                continue
            if elem.get_text(strip=True):
                all_elements.append(elem)
        elif elem.name == 'a' and elem.get('href'):
            all_elements.append(elem)

    # Map boundary spans to their index in all_elements
    boundary_indices = []
    for seq, boundary_span in item_boundaries:
        for i, elem in enumerate(all_elements):
            if elem is boundary_span:
                boundary_indices.append((seq, i))
                break

    # Parse each item by slicing between boundaries
    items = []
    for idx, (seq, start_idx) in enumerate(boundary_indices):
        end_idx = boundary_indices[idx + 1][1] if idx + 1 < len(boundary_indices) else len(all_elements)

        title_parts = []
        body_parts = []
        attachments = []
        matter_file = None
        in_attachments_section = False

        for elem in all_elements[start_idx + 1 : end_idx]:
            text = (elem.get_text(strip=True) or '').replace('\xa0', ' ').strip()
            if not text:
                continue

            # Check for matter ID anywhere in text
            if not matter_file:
                matter_match = _MATTER_ID_RE.search(text)
                if matter_match:
                    matter_file = matter_match.group(1)

            # Detect "Attachments:" header
            if re.match(r'^Attachments?\s*:?\s*$', text, re.IGNORECASE):
                in_attachments_section = True
                continue

            # Collect attachment links
            if elem.name == 'a':
                href = elem['href']
                link_name = text or 'Attachment'
                if in_attachments_section or '.pdf' in href.lower():
                    abs_url = href if href.startswith('http') else urljoin(base_url, href)
                    file_type = classify_attachment_type(abs_url, link_name)
                    attachments.append({
                        'name': link_name,
                        'url': abs_url,
                        'type': file_type if file_type != 'unknown' else 'pdf',
                    })
                continue

            # Title: bold-class text after item number.
            # Check own classes and parent span classes (leaf spans inside
            # bold parents inherit the bold role but may only have stl_10 etc.)
            elem_classes = set(elem.get('class', []))
            parent = elem.parent
            if parent and parent.name == 'span':
                elem_classes.update(parent.get('class', []))
            is_bold = bold_class and bold_class in elem_classes

            if is_bold and not in_attachments_section:
                title_parts.append(text)
            else:
                body_parts.append(text)

        title = ' '.join(title_parts) if title_parts else f"Item {seq}"

        item_data = {
            'vendor_item_id': f"{meeting_id}-{seq}",
            'title': title,
            'sequence': seq,
            'attachments': attachments,
        }
        if matter_file:
            item_data['matter_file'] = matter_file

        items.append(item_data)

    logger.info(
        "parsed AADA accessible agenda",
        meeting_id=meeting_id,
        item_count=len(items),
        attachment_count=sum(len(i.get('attachments', [])) for i in items),
    )

    return {
        'participation': {},
        'items': items,
    }
