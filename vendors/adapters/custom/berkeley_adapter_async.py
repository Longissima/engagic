"""
Async Berkeley City Council Adapter - Custom Drupal CMS

URL patterns:
- Meetings list: https://berkeleyca.gov/your-government/city-council/city-council-agendas
- HTML agenda: https://berkeleyca.gov/city-council-regular-meeting-eagenda-november-10-2025
- PDF packet: https://berkeleyca.gov/sites/default/files/city-council-meetings/2025-11-10%20Agenda%20Packet...pdf

HTML structure (verified Nov 2025):
- Drupal views table with 7 columns: Meeting | Date | Agenda | Agenda Packet | Annotated | Video | Download
- Date in cell 1 with <time> tag
- HTML agenda link in cell 2
- PDF packet link in cell 3
- Agenda items: <strong>1.</strong><a href="...pdf">Title</a> format
- Item metadata: From, Recommendation, Financial Implications, Contact
- Participation: Zoom, phone, email in intro paragraph

Async version with:
- aiohttp for async HTTP requests
- asyncio.to_thread for CPU-bound BeautifulSoup parsing
- Non-blocking I/O for concurrent fetching

Confidence: 9/10 - Verified working with item-level extraction
"""

import re
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup, Tag

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector


_COUNCIL_TITLE_RE = re.compile(r"BERKELEY CITY COUNCIL", re.IGNORECASE)
_FROM_RE = re.compile(r"^From:", re.IGNORECASE)
_RECOMMENDATION_RE = re.compile(r"^Recommendation:", re.IGNORECASE)


def _find_matching_strong(
    root: BeautifulSoup | Tag,
    pattern: re.Pattern[str],
    *,
    following: bool = False,
) -> Optional[Tag]:
    """Find a strong tag whose direct string matches ``pattern``."""
    candidates = (
        root.find_all_next("strong") if following else root.find_all("strong")
    )
    for candidate in candidates:
        if not isinstance(candidate, Tag):
            continue
        value = candidate.string
        if isinstance(value, str) and pattern.search(value):
            return candidate
    return None


class AsyncBerkeleyAdapter(AsyncBaseAdapter):
    """Async Berkeley City Council - Custom Drupal CMS adapter"""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="berkeley", metrics=metrics)
        self.base_url = "https://berkeleyca.gov"

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 28) -> List[Dict[str, Any]]:
        """Fetch meetings from Berkeley's Drupal-based website."""
        today = datetime.now().date()
        start_date = today - timedelta(days=days_back)
        end_date = today + timedelta(days=days_forward)

        meetings_url = f"{self.base_url}/your-government/city-council/city-council-agendas"

        logger.info("fetching meetings list", vendor="berkeley", slug=self.slug, url=meetings_url)

        try:
            response = await self._get(meetings_url)
            html = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("failed to fetch meetings list", vendor="berkeley", slug=self.slug, error=str(e))
            return []

        # Parse HTML (CPU-bound, run in thread pool)
        soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')

        # Find table rows with meeting data
        rows = soup.find_all('tr')

        results = []
        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 4:  # Need at least title, date, and agenda columns
                continue

            # Cell 1: Date (with <time> tag)
            time_tag = cells[1].find('time')
            if not time_tag:
                date_text = cells[1].get_text(strip=True)
            else:
                datetime_value = time_tag.get("datetime")
                date_text = (
                    datetime_value
                    if isinstance(datetime_value, str)
                    else time_tag.get_text(strip=True)
                )

            if not date_text:
                continue

            # Parse date using base adapter's parser
            meeting_date = self._parse_date(date_text)
            if not meeting_date:
                logger.debug("could not parse date", vendor="berkeley", slug=self.slug, date_text=date_text)
                continue

            # Filter to meetings within date range
            meeting_date_only = meeting_date.date()

            if meeting_date_only < start_date or meeting_date_only > end_date:
                logger.debug("skipping meeting outside date window", vendor="berkeley", slug=self.slug, date=date_text)
                continue

            # Cell 2: HTML Agenda link (Berkeley always has HTML agendas)
            html_link = None
            html_cell = cells[2]
            html_a = html_cell.find('a', href=True)
            if html_a:
                html_href = html_a.get("href")
                if isinstance(html_href, str):
                    html_link = urljoin(self.base_url, html_href)

            # Skip if no HTML agenda (Berkeley should always have one)
            if not html_link:
                logger.debug("no HTML agenda link", vendor="berkeley", slug=self.slug, date=date_text)
                continue

            # Generate unique vendor_id using hash (date-only IDs collide on same-day meetings)
            # Use agenda URL path as unique identifier since it contains meeting-specific slug
            url_path = html_link.replace(self.base_url, '').strip('/')
            vendor_id = self._generate_fallback_vendor_id(title=url_path, date=meeting_date)

            # Extract time from date string (e.g., "11/10/2025 - 6:00 pm")
            time_match = re.search(r'(\d{1,2}:\d{2}\s*[ap]m)', date_text, re.IGNORECASE)
            meeting_time = time_match.group(1) if time_match else None

            meeting_data: Dict[str, Any] = {
                'vendor_id': vendor_id,
                'start': meeting_date.isoformat(),
                'title': "City Council Meeting",
                'agenda_url': html_link,
            }

            if meeting_time:
                meeting_data['time'] = meeting_time

            # Post-meeting record column (Drupal field-annotated-agenda):
            # "Annotated Agenda" PDFs for open sessions -- Berkeley's action
            # record in lieu of separate council minutes -- and "Closed
            # Minutes" PDFs for closed sessions. Empty until after the meeting.
            record_cell = row.find('td', class_='views-field-field-annotated-agenda')
            if record_cell:
                record_a = record_cell.find('a', href=True)
                if record_a:
                    record_href = record_a.get("href")
                    if isinstance(record_href, str):
                        meeting_data['minutes_url'] = urljoin(
                            self.base_url, record_href
                        )

            if self._minutes_discovery_only:
                if meeting_data.get("minutes_url"):
                    results.append(meeting_data)
                continue

            # Fetch HTML agenda detail to extract items
            try:
                logger.info("fetching HTML agenda detail", vendor="berkeley", slug=self.slug, url=html_link)
                detail = await self._fetch_meeting_detail(html_link)
                if detail:
                    if detail.get('participation'):
                        meeting_data['participation'] = detail['participation']
                    if detail.get('items'):
                        meeting_data['items'] = detail['items']
                        logger.info("extracted items from HTML agenda", vendor="berkeley", slug=self.slug, item_count=len(detail['items']))
                    if detail.get('title'):
                        meeting_data['title'] = detail['title']
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning("failed to fetch detail", vendor="berkeley", slug=self.slug, vendor_id=vendor_id, error=str(e))
                # Continue anyway - we have basic meeting data even without items

            item_count = len(meeting_data.get('items', []))
            attachment_count = sum(len(item.get('attachments', [])) for item in meeting_data.get('items', []))
            logger.info(
                "found items and attachments",
                vendor="berkeley",
                slug=self.slug,
                item_count=item_count,
                attachment_count=attachment_count,
                date=meeting_date.strftime('%Y-%m-%d')
            )

            results.append(meeting_data)

        return results

    async def _fetch_meeting_detail(self, agenda_url: str) -> Dict[str, Any]:
        """Fetch and parse HTML agenda detail page."""
        logger.debug("fetching detail page", vendor="berkeley", slug=self.slug, url=agenda_url)

        response = await self._get(agenda_url)
        html = await response.text()

        # Parse HTML (CPU-bound, run in thread pool)
        soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')

        # Extract title from header
        title = None
        title_tag = _find_matching_strong(soup, _COUNCIL_TITLE_RE)
        if title_tag:
            title = title_tag.get_text(strip=True)

        # Extract participation info (CPU-bound, but small)
        participation = await asyncio.to_thread(self._extract_participation, soup)

        # Extract agenda items (CPU-bound)
        items = await asyncio.to_thread(self._extract_items, soup)

        return {
            'title': title,
            'participation': participation,
            'items': items,
        }

    def _extract_participation(self, soup: BeautifulSoup) -> Dict[str, Any]:
        """Extract participation info (email, virtual_url, phone, is_hybrid) from intro paragraphs."""
        participation = {}

        # Get all text from page
        page_text = soup.get_text()

        # Email pattern
        email_match = re.search(r'council@berkeleyca\.gov', page_text, re.IGNORECASE)
        if email_match:
            participation['email'] = 'council@berkeleyca.gov'

        # Zoom URL
        zoom_match = re.search(r'https://cityofberkeley-info\.zoomgov\.com/j/(\d+)', page_text)
        if zoom_match:
            participation['virtual_url'] = zoom_match.group(0)
            participation['meeting_id'] = zoom_match.group(1)

        # Phone (look for pattern like "1-669-254-5252")
        phone_match = re.search(r'1-(\d{3})-(\d{3})-(\d{4})', page_text)
        if phone_match:
            participation['phone'] = f"+1{phone_match.group(1)}{phone_match.group(2)}{phone_match.group(3)}"

        # Meeting is hybrid if both Zoom and in-person mentioned
        if participation.get('virtual_url') and 'hybrid' in page_text.lower():
            participation['is_hybrid'] = True

        return participation

    def _extract_items(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """
        Extract agenda items from HTML content.

        Berkeley format: <strong>1.</strong><a href="...pdf">Title</a>
        followed by From:/Recommendation: metadata blocks.
        """
        items = []

        # Find all <strong> tags that contain item numbers (1., 2., etc.)
        strong_tags = soup.find_all('strong')

        for strong in strong_tags:
            if not isinstance(strong, Tag):
                continue
            text = strong.get_text(strip=True)

            # Match item numbers: "1.", "2.", etc. (not "H1.", "I1." - those are sections)
            if not re.match(r'^\d+\.$', text):
                continue

            item_number = int(text.rstrip('.'))

            # Find the link following this number
            next_link = strong.find_next('a', href=True)
            if not isinstance(next_link, Tag):
                continue

            title = next_link.get_text(strip=True)
            # Remove leading dash if present
            title = title.lstrip('-').strip()

            href_value = next_link.get("href")
            href = href_value if isinstance(href_value, str) else ""
            attachment_url = urljoin(self.base_url, href) if href else None

            # Find "From:" line
            from_line = _find_matching_strong(strong, _FROM_RE, following=True)
            sponsor = None
            if from_line:
                sponsor_text = from_line.get_text(strip=True)
                sponsor = sponsor_text.replace('From:', '').strip()

            # Find "Recommendation:" line
            rec_line = _find_matching_strong(
                strong, _RECOMMENDATION_RE, following=True
            )
            recommendation = None
            if rec_line:
                # Get text between "Recommendation:" and next <strong> tag
                rec_text = []
                current = rec_line.next_sibling
                while current and not (
                    isinstance(current, Tag) and current.name == "strong"
                ):
                    if isinstance(current, str):
                        rec_text.append(current.strip())
                    elif isinstance(current, Tag) and current.name == 'br':
                        break
                    current = getattr(current, "next_sibling", None)
                recommendation = ' '.join(rec_text).strip()

            attachments = []
            if attachment_url and attachment_url.endswith('.pdf'):
                attachments.append({
                    'name': title,
                    'url': attachment_url,
                    'type': 'pdf',
                })

            item_data = {
                'vendor_item_id': str(item_number),
                'title': title,
                'sequence': item_number,
                'attachments': attachments,
            }

            if sponsor:
                item_data['sponsors'] = [sponsor]
            if recommendation:
                item_data['recommendation'] = recommendation

            items.append(item_data)

        logger.debug("extracted items", vendor="berkeley", slug=self.slug, item_count=len(items))
        return items
