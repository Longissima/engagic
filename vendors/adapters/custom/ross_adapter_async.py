"""
Async Ross CA Adapter - Drupal/AHA FastTrack meetings scraper

Town of Ross uses AHA Consulting's FastTrack platform (Drupal 7) with the
aha_fasttrack_meetings module. Meetings are listed in an HTML table at
/meetings with server-side date filtering.

Meeting list page structure (table columns):
  Date | Meeting | Agendas | Minutes | Staff Reports | Audio | Video | Details

Detail pages (/towncouncil/page/...) have structured staff reports as
filefield-file attachments labeled "Item {N}. {title}" with direct PDF links.

Bodies are identified by the detail URL path prefix:
  /towncouncil/         -> Town Council
  /advisorydesignreview/ -> Advisory Design Review Group

Fallback chain per meeting:
  1. Structured items from detail page staff reports (Item 11a, 11b, etc.)
  2. Agenda PDF(s) for chunker (always set when available)
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from exceptions import VendorHTTPError
from pipeline.protocols import MetricsCollector


# Item label pattern on detail pages: "Item 11a." or "Item 12."
_ITEM_LABEL_RE = re.compile(r'^Item\s+(\d+[a-z]?)\.?\s+(.+)', re.IGNORECASE)

# Body name from URL path prefix
_BODY_MAP = {
    "towncouncil": "Town Council",
    "advisorydesignreview": "Advisory Design Review Group",
    "communityprotectioncommittee": "Community Protection Committee",
    "financecommittee": "Finance Committee",
    "generalgovernmentcommittee": "General Government Committee",
    "publicworkscommittee": "Public Works Committee",
}

# Node ID from fileattachment path: .../meeting/4576/filename.pdf
_NODE_ID_RE = re.compile(r'/meeting/(\d+)/')


class AsyncRossAdapter(AsyncBaseAdapter):
    """Custom adapter for Town of Ross CA (Drupal/AHA FastTrack)."""

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="ross", metrics=metrics)
        self.base_url = "https://www.townofrossca.gov"

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 14
    ) -> List[Dict[str, Any]]:
        """Scrape meetings table, then enrich from detail pages."""
        today = datetime.now()
        start_date = today - timedelta(days=days_back)
        end_date = today + timedelta(days=days_forward)

        meetings = await self._scrape_meetings_table(start_date, end_date)

        logger.info(
            "ross meetings scraped",
            slug=self.slug,
            count=len(meetings),
            start_date=str(start_date.date()),
            end_date=str(end_date.date()),
        )

        # Enrich with structured items from detail pages
        tasks = [self._enrich_meeting(m) for m in meetings]
        enriched = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for idx, meeting in enumerate(enriched):
            if isinstance(meeting, Exception):
                logger.warning(
                    "meeting enrichment failed",
                    vendor="ross",
                    slug=self.slug,
                    error=str(meeting),
                    meeting_index=idx,
                )
            elif isinstance(meeting, dict):
                results.append(meeting)

        return results

    async def _enrich_meeting(self, meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch detail page and extract structured items.

        Fallback: if detail page yields no items, try chunking the agenda PDF.
        """
        detail_url = meeting.pop("_detail_url", "")
        if not detail_url:
            return meeting

        items = await self._fetch_detail_items(detail_url)
        if items:
            meeting["items"] = items
            logger.info(
                "parsed items from detail page",
                vendor="ross",
                slug=self.slug,
                item_count=len(items),
                title=meeting.get("title", "")[:50],
            )
            return meeting

        # No structured items from detail page -- try agenda (url) then packet (toc)
        chunked = await self._chunk_agenda_then_packet(
            agenda_url=meeting.get("agenda_url"),
            packet_url=None,
            vendor_id=meeting.get("vendor_id"),
        )
        if chunked:
            meeting["items"] = chunked

        return meeting

    # ------------------------------------------------------------------
    # Meetings table scraping
    # ------------------------------------------------------------------

    async def _scrape_meetings_table(
        self, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch /meetings with date filter params and parse the HTML table."""
        params = {
            "date_filter[value][month]": str(start_date.month),
            "date_filter[value][day]": str(start_date.day),
            "date_filter[value][year]": str(start_date.year),
            "date_filter_1[value][month]": str(end_date.month),
            "date_filter_1[value][day]": str(end_date.day),
            "date_filter_1[value][year]": str(end_date.year),
        }

        try:
            response = await self._get(f"{self.base_url}/meetings", params=params)
            html = await response.text()
        except VendorHTTPError as e:
            logger.error(
                "failed to fetch meetings page",
                vendor="ross",
                slug=self.slug,
                error=str(e),
            )
            return []

        soup = BeautifulSoup(html, "html.parser")

        # The "All Meetings" tab has id="recentMeetings"
        table = soup.select_one("#recentMeetings table")
        if not table:
            logger.warning("meetings table not found", vendor="ross", slug=self.slug)
            return []

        meetings = []
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 8:
                continue

            meeting = self._parse_table_row(cells)
            if meeting:
                meetings.append(meeting)

        return meetings

    def _parse_table_row(self, cells: List[Tag]) -> Optional[Dict[str, Any]]:
        """Parse one row of the meetings table into a meeting dict.

        Columns: Date | Meeting | Agendas | Minutes | Staff Reports | Audio | Video | Details
        """
        # Date: ISO datetime in content attribute of <span>
        date_span = cells[0].find("span", class_="date-display-single")
        if not date_span:
            return None
        date_content = date_span.get("content", "")
        date_str = str(date_content) if date_content else ""

        # Meeting title
        title = cells[1].get_text(strip=True)
        if not title:
            return None

        # Detail page link (column 7) -> vendor_id and body
        detail_link = cells[7].find("a")
        detail_path = detail_link.get("href", "") if detail_link else ""
        if isinstance(detail_path, list):
            detail_path = detail_path[0] if detail_path else ""
        detail_url = urljoin(self.base_url, str(detail_path)) if detail_path else ""

        # Vendor ID from agenda fileattachment path, or from detail path
        vendor_id = self._extract_vendor_id(cells, str(detail_path))

        # Body from detail URL path prefix
        body = self._extract_body(str(detail_path))

        # Agenda PDFs (column 2)
        agenda_urls = self._extract_pdf_links(cells[2])

        # Minutes PDFs (column 3)
        minutes_urls = self._extract_pdf_links(cells[3])

        # Staff reports page link (column 4)
        staff_link = cells[4].find("a")
        staff_reports_url = ""
        if staff_link:
            href = staff_link.get("href", "")
            if isinstance(href, list):
                href = href[0] if href else ""
            staff_reports_url = urljoin(self.base_url, str(href))

        # Video link (column 6)
        video_link = cells[6].find("a")
        video_url = ""
        if video_link:
            href = video_link.get("href", "")
            if isinstance(href, list):
                href = href[0] if href else ""
            video_url = str(href)

        meeting_status = self._parse_meeting_status(title, date_str)

        result: Dict[str, Any] = {
            "vendor_id": vendor_id,
            "title": title,
            "start": date_str,
        }

        if meeting_status:
            result["meeting_status"] = meeting_status

        # Pick the main agenda (non-closed-session one if multiple)
        main_agenda = self._pick_main_agenda(agenda_urls)
        if main_agenda:
            result["agenda_url"] = main_agenda

        if minutes_urls:
            result["minutes_url"] = minutes_urls[0]
            result["metadata"] = result.get("metadata", {})
            result["metadata"]["minutes_urls"] = minutes_urls

        meta: Dict[str, Any] = result.get("metadata", {})
        if body:
            meta["body"] = body
        if video_url:
            meta["video_url"] = video_url
        if detail_url:
            meta["detail_url"] = detail_url
        if staff_reports_url:
            meta["staff_reports_url"] = staff_reports_url
        if meta:
            result["metadata"] = meta

        # Fetch detail page for structured items
        if staff_reports_url or detail_url:
            result["_detail_url"] = staff_reports_url or detail_url

        return result

    # ------------------------------------------------------------------
    # Detail page item extraction
    # ------------------------------------------------------------------

    async def _fetch_detail_items(self, detail_url: str) -> List[Dict[str, Any]]:
        """Fetch a meeting detail page and extract structured staff report items.

        Detail pages list file attachments with labels like:
          "Item 11a. Title of the item" -> staff report PDF
          "Item 12. Public Comment_Name" -> public comment PDF

        Returns pipeline-compatible AgendaItemSchema dicts.
        """
        try:
            response = await self._get(detail_url)
            html = await response.text()
        except VendorHTTPError:
            return []

        soup = BeautifulSoup(html, "html.parser")
        items_by_number: Dict[str, Dict[str, Any]] = {}
        sequence = 0

        for file_div in soup.find_all(class_="filefield-file"):
            link = file_div.find("a", href=True)
            if not link:
                continue

            href_val = link.get("href", "")
            href = str(href_val) if isinstance(href_val, str) else ""
            if not href or not href.endswith(".pdf"):
                continue

            label = link.get_text(strip=True)
            match = _ITEM_LABEL_RE.match(label)
            if not match:
                continue

            item_number = match.group(1)
            item_title = match.group(2).strip()
            url = urljoin(self.base_url, href)

            # Public comments are attachments on their parent item, not separate items
            is_public_comment = "public comment" in item_title.lower()

            if item_number not in items_by_number and not is_public_comment:
                sequence += 1
                items_by_number[item_number] = {
                    "vendor_item_id": item_number,
                    "title": item_title,
                    "sequence": sequence,
                    "agenda_number": item_number,
                    "attachments": [{
                        "name": item_title,
                        "url": url,
                        "type": "pdf",
                    }],
                }
            elif item_number in items_by_number:
                # Additional attachment on existing item (or public comment)
                att_name = f"Public Comment: {item_title.split('_', 1)[-1]}" if is_public_comment else item_title
                items_by_number[item_number]["attachments"].append({
                    "name": att_name,
                    "url": url,
                    "type": "pdf",
                })

        return list(items_by_number.values())

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _extract_pdf_links(self, cell: Tag) -> List[str]:
        """Extract all PDF URLs from a table cell."""
        urls = []
        for link in cell.find_all("a", href=True):
            href_val = link.get("href", "")
            href = str(href_val) if isinstance(href_val, str) else ""
            if href and ".pdf" in href.lower():
                urls.append(urljoin(self.base_url, href))
        return urls

    def _extract_vendor_id(self, cells: List[Tag], detail_path: str) -> str:
        """Extract a stable vendor ID from fileattachment paths or detail path."""
        # Try node ID from agenda PDF paths: .../meeting/4576/filename.pdf
        for cell in cells:
            for link in cell.find_all("a", href=True):
                href = str(link.get("href", ""))
                match = _NODE_ID_RE.search(href)
                if match:
                    return match.group(1)

        # Fallback: detail page path slug
        if detail_path:
            slug = detail_path.rstrip("/").rsplit("/", 1)[-1]
            return slug

        return ""

    def _extract_body(self, detail_path: str) -> Optional[str]:
        """Extract body name from detail page URL path prefix."""
        if not detail_path:
            return None
        # "/towncouncil/page/meeting-name" -> "towncouncil"
        path = detail_path.lstrip("/")
        prefix = path.split("/", 1)[0] if "/" in path else path
        return _BODY_MAP.get(prefix)

    def _pick_main_agenda(self, agenda_urls: List[str]) -> Optional[str]:
        """Pick the main meeting agenda from a list of agenda PDFs.

        Ross often has two: a closed session agenda and the regular agenda.
        Prefer the regular (non-closed-session) one.
        """
        if not agenda_urls:
            return None
        if len(agenda_urls) == 1:
            return agenda_urls[0]

        for url in agenda_urls:
            lower = url.lower()
            if "closed_session" not in lower and "special_meeting" not in lower:
                return url

        # All are closed session, return the last one
        return agenda_urls[-1]

