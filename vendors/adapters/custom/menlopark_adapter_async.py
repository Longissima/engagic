"""
Async Menlo Park Adapter - All commissions via table-based website with PDF item extraction

Scrapes https://menlopark.gov/Agendas-and-minutes which lists 8 bodies:
  City Council (#section-2), Complete Streets (#section-3), Environmental Quality (#section-4),
  Finance and Audit (#section-5), Housing (#section-6), Library (#section-7),
  Parks and Recreation (#section-8), Planning Commission (#section-9).

Each section has a table: Date | Agenda packet (PDF) | Minutes | Video.
Sections are delimited by <h2> headings.

Chunking strategy per body:
- City Council: thin agenda PDFs with hyperlinks -> v1 URL (custom parser)
- All other commissions: compiled packet PDFs with bookmark TOC -> v2 TOC

Confidence: 8/10 - Consistent table structure across all commissions
"""

import asyncio
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup, Tag

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from parsing.pdf import PdfExtractor
from parsing.menlopark_pdf import parse_menlopark_pdf_agenda
from pipeline.protocols import MetricsCollector


# Body definitions: (h2 heading text, body_name for committee tracking, parse method)
# City Council agendas are thin PDFs with hyperlinked staff reports -> v1 URL (custom parser)
# All other commissions publish compiled packet PDFs with bookmark trees -> v2 TOC
_BODIES: List[Tuple[str, str, str]] = [
    ("City Council", "City Council", "url"),
    ("Complete Streets Commission", "Complete Streets Commission", "toc"),
    ("Environmental Quality Commission", "Environmental Quality Commission", "toc"),
    ("Finance and Audit Commission", "Finance and Audit Commission", "toc"),
    ("Housing Commission", "Housing Commission", "toc"),
    ("Library Commission", "Library Commission", "toc"),
    ("Parks and Recreation Commission", "Parks and Recreation Commission", "toc"),
    ("Planning Commission", "Planning Commission", "toc"),
]


class AsyncMenloParkAdapter(AsyncBaseAdapter):
    """Async Menlo Park - all commissions with PDF agenda/packet extraction"""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="menlopark", metrics=metrics)
        self.base_url = "https://menlopark.gov"
        self.pdf_extractor = PdfExtractor.shared()

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings from all Menlo Park commissions, extracting items from PDFs."""
        today = datetime.now().date()
        start_date = today - timedelta(days=days_back)
        end_date = today + timedelta(days=days_forward)

        meetings_url = f"{self.base_url}/Agendas-and-minutes"

        logger.info("fetching meetings list", vendor="menlopark", slug=self.slug, url=meetings_url)

        try:
            response = await self._get(meetings_url)
            html = await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("failed to fetch meetings list", vendor="menlopark", slug=self.slug, error=str(e))
            return []

        soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')

        # Map h2 heading text -> first table following it (the current-year table)
        body_tables = self._find_body_tables(soup)

        results = []
        for heading_text, body_name, parse_method in _BODIES:
            table = body_tables.get(heading_text)
            if not table:
                logger.debug("no table found for body", vendor="menlopark", slug=self.slug, body=heading_text)
                continue

            for row in table.find_all('tr'):
                if not isinstance(row, Tag):
                    continue
                meeting = await self._parse_table_row(
                    row, body_name, parse_method, start_date, end_date
                )
                if meeting:
                    results.append(meeting)

        return results

    def _find_body_tables(self, soup: BeautifulSoup) -> Dict[str, Tag]:
        """Map each <h2> heading text to the first non-empty <table> that follows it.

        The Menlo Park page has an empty placeholder table right after each <h2>,
        then an <h3>2026</h3>, then the populated data table.  We skip empty
        tables and stop at the next <h2> to avoid crossing into another section.
        """
        body_tables: Dict[str, Tag] = {}
        known_headings = {heading for heading, _, _ in _BODIES}
        for h2 in soup.find_all('h2'):
            heading_text = h2.get_text(strip=True)
            if heading_text not in known_headings:
                continue

            # Search for the first table with data rows between this h2 and the next
            elem = h2.find_next()
            while elem:
                if isinstance(elem, Tag) and elem.name == 'h2':
                    break  # crossed into next section
                if isinstance(elem, Tag) and elem.name == 'table':
                    if elem.find('tr'):  # has at least one row (skip empty placeholders)
                        body_tables[heading_text] = elem
                        break
                elem = elem.find_next()

        return body_tables

    async def _parse_table_row(
        self,
        row: Tag,
        body_name: str,
        parse_method: str,
        start_date,
        end_date,
    ) -> Optional[Dict[str, Any]]:
        """Parse a single table row into a meeting dict, extracting items from the PDF."""
        cells = row.find_all('td')
        if len(cells) < 2:
            return None

        date_text = cells[0].get_text(strip=True)
        if not date_text:
            return None

        meeting_date = self._parse_menlopark_date(date_text)
        if not meeting_date:
            return None

        meeting_date_only = meeting_date.date()
        if meeting_date_only < start_date or meeting_date_only > end_date:
            return None

        # Cell 1: Agenda packet PDF link
        link = cells[1].find('a', href=True, class_='document')
        pdf_link = None
        if link:
            agenda_href = link.get("href")
            if isinstance(agenda_href, str):
                pdf_link = urljoin(self.base_url, agenda_href)
        if not pdf_link:
            return None

        # Cell 2: Minutes PDF link (populated once minutes are approved)
        minutes_url = None
        if len(cells) > 2:
            minutes_link = cells[2].find('a', href=True, class_='document')
            if minutes_link:
                minutes_href = minutes_link.get("href")
                if isinstance(minutes_href, str):
                    minutes_url = urljoin(self.base_url, minutes_href)

        url_path = pdf_link.replace(self.base_url, '').strip('/')
        vendor_id = self._generate_fallback_vendor_id(title=url_path, date=meeting_date)

        meeting_title = f"{body_name} Meeting"

        meeting_data: Dict[str, Any] = {
            'vendor_id': vendor_id,
            'start': meeting_date.isoformat(),
            'title': meeting_title,
            'body_name': body_name,
            'agenda_url': pdf_link,
        }

        if minutes_url:
            meeting_data['minutes_url'] = minutes_url

        if self._minutes_discovery_only:
            return meeting_data

        # Extract items using the appropriate chunking strategy
        try:
            if parse_method == "url":
                items = await self._extract_items_url(pdf_link, vendor_id)
            else:
                items = await self._extract_items_toc(pdf_link, vendor_id)

            if items:
                meeting_data['items'] = items
                logger.info(
                    "extracted items from PDF",
                    vendor="menlopark", slug=self.slug,
                    body=body_name, item_count=len(items),
                    date=meeting_date.strftime('%Y-%m-%d'),
                    method=parse_method,
                )
            else:
                logger.warning(
                    "no items extracted from PDF",
                    vendor="menlopark", slug=self.slug,
                    body=body_name, vendor_id=vendor_id,
                    method=parse_method,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.warning(
                "failed to parse PDF items",
                vendor="menlopark", slug=self.slug,
                body=body_name, vendor_id=vendor_id, error=str(e),
            )

        return meeting_data

    async def _extract_items_url(self, pdf_link: str, vendor_id: str) -> List[Dict[str, Any]]:
        """City Council: custom parser for thin agendas with hyperlinked staff reports (v1 URL)."""
        logger.info("extracting items via URL parser", vendor="menlopark", slug=self.slug, url=pdf_link)

        pdf_result = await asyncio.to_thread(
            self.pdf_extractor.extract_from_url,
            pdf_link,
            extract_links=True,
        )

        if not pdf_result['success']:
            logger.error(
                "PDF extraction failed", vendor="menlopark", slug=self.slug,
                vendor_id=vendor_id, error=pdf_result.get('error', 'unknown'),
            )
            return []

        parsed = await asyncio.to_thread(
            parse_menlopark_pdf_agenda,
            pdf_result['text'],
            pdf_result.get('links', []),
        )
        return parsed.get('items', [])

    async def _extract_items_toc(self, pdf_link: str, vendor_id: str) -> List[Dict[str, Any]]:
        """Other commissions: v2 TOC chunker for compiled packet PDFs with bookmarks."""
        logger.info("extracting items via TOC parser", vendor="menlopark", slug=self.slug, url=pdf_link)
        return await self._parse_packet_pdf(pdf_link, vendor_id, force_method="toc")

    def _parse_menlopark_date(self, date_str: str) -> Optional[datetime]:
        """Parse Menlo Park date formats: 'Nov. 4, 2025', 'October 21, 2025'."""
        date_str = date_str.strip()
        for fmt in ("%b. %d, %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None
