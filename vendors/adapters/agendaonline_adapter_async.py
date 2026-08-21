"""
Async AgendaOnline Adapter - Granicus AgendaOnline on custom domains.

Handles sites that run Granicus AgendaOnline but are NOT on *.granicus.com.
These sites have their own domain with a site path prefix (e.g., /SAFCA).

Architecture:
  Listing page: {base_url}{site_path}/Meetings/Search?{listing_params}
    - HTML table with meeting rows, dates, and ViewMeeting links
    - Meeting IDs extracted from /Meetings/ViewMeeting?id={id} hrefs

  Agenda view: {base_url}{site_path}/Meetings/ViewMeetingAgenda?meetingId={id}&type=agenda
    - Accessible HTML with .accessible-section and .accessible-item divs
    - Parsed by parse_agendaonline_html (shared with Granicus adapter)

  Item attachments: {base_url}{site_path}/Meetings/ViewMeetingAgendaItem?meetingId={id}&itemId={id}&isSection=false&type=agenda
    - Contains DownloadFile links for PDFs

Per-site config in data/agendaonline_sites.json, keyed by banana.
"""

import asyncio
import re
from datetime import datetime
from typing import Dict, Any, List, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.parsers.granicus_parser import parse_agendaonline_html
from pipeline.protocols import MetricsCollector


AGENDAONLINE_CONFIG_FILE = "data/agendaonline_sites.json"

_MEETING_ID_RE = re.compile(r'ViewMeeting\?id=(\d+)')
_DATE_RE = re.compile(r'(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M)')
# documentType=2 is the product's minutes doctype (mirrors ViewMeeting doctype=2)
_MINUTES_DOC_RE = re.compile(r'/Documents/DownloadFile/.*[?&]documentType=2(?!\d)')


def _load_config() -> Dict[str, Any]:
    return AsyncBaseAdapter._load_vendor_config(AGENDAONLINE_CONFIG_FILE)


def _translate_downloadfile_to_viewdocument(url: str) -> str:
    """Translate AgendaOnline DownloadFile URL to ViewDocument for direct fetch."""
    if "/Documents/DownloadFile/" not in url:
        return url
    return url.replace("/Documents/DownloadFile/", "/Documents/ViewDocument/")


class AsyncAgendaOnlineAdapter(AsyncBaseAdapter):
    """Async adapter for Granicus AgendaOnline sites on custom domains."""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="agendaonline", metrics=metrics)

        site_config = _load_config().get(self.slug, {})
        self.base_url = site_config.get("base_url")
        if not self.base_url:
            raise ValueError(
                f"No base_url configured for agendaonline slug '{self.slug}'. "
                f"Add entry to {AGENDAONLINE_CONFIG_FILE}"
            )

        self.site_path = site_config.get("site_path", "")
        self.listing_params = site_config.get("listing_params", "")
        self.body_name = site_config.get("body", "")

    def _site_url(self, path: str) -> str:
        return f"{self.base_url}{self.site_path}{path}"

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 28
    ) -> List[Dict[str, Any]]:
        start_date, end_date = self._date_range(days_back, days_forward)

        # Scrape meeting listing
        listing_url = self._site_url(f"/Meetings/Search?{self.listing_params}")
        try:
            response = await self._get(listing_url)
            html = await response.text()
        except Exception as e:
            logger.error(
                "listing page unreachable",
                vendor="agendaonline",
                slug=self.slug,
                url=listing_url,
                error=str(e),
            )
            return []

        meeting_refs = self._parse_listing(html, start_date, end_date)

        if not meeting_refs:
            logger.info(
                "no meetings in date range",
                vendor="agendaonline",
                slug=self.slug,
                start=str(start_date.date()),
                end=str(end_date.date()),
            )
            return []

        if self._minutes_discovery_only:
            return [
                {
                    "vendor_id": ref["meeting_id"],
                    "title": ref["title"],
                    "start": ref["start"],
                    "minutes_url": ref["minutes_url"],
                }
                for ref in meeting_refs
                if ref.get("minutes_url")
            ]

        # Fetch agenda details concurrently
        tasks = [self._fetch_meeting_detail(ref) for ref in meeting_refs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        meetings = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "meeting detail failed",
                    vendor="agendaonline",
                    slug=self.slug,
                    meeting_id=meeting_refs[idx]["meeting_id"],
                    error=str(result),
                )
            elif result:
                meetings.append(result)

        logger.info(
            "meetings fetched",
            vendor="agendaonline",
            slug=self.slug,
            count=len(meetings),
            with_items=sum(1 for m in meetings if m.get("items")),
        )

        return meetings

    # ------------------------------------------------------------------
    # Listing parser
    # ------------------------------------------------------------------

    def _parse_listing(
        self, html: str, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        seen_ids = set()
        meetings = []

        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            # Find meeting ID from ViewMeeting link (doctype=1 = agenda)
            agenda_link = row.find(
                "a",
                href=lambda x: bool(
                    x and "ViewMeeting" in x and "doctype=1" in x
                ),
            )
            if not agenda_link:
                continue

            agenda_href = agenda_link.get("href")
            if not isinstance(agenda_href, str):
                continue
            id_match = _MEETING_ID_RE.search(agenda_href)
            if not id_match:
                continue

            meeting_id = id_match.group(1)
            if meeting_id in seen_ids:
                continue
            seen_ids.add(meeting_id)

            # Parse date from cell text
            date_text = cells[2].get_text(strip=True)
            date_match = _DATE_RE.search(date_text)
            if not date_match:
                continue

            try:
                meeting_dt = datetime.strptime(date_match.group(1), "%m/%d/%Y %I:%M:%S %p")
            except ValueError:
                continue

            if not (start_date <= meeting_dt <= end_date):
                continue

            # Title from first cell or body name
            title = cells[0].get_text(strip=True) or self.body_name

            meeting_ref = {
                "meeting_id": meeting_id,
                "title": title,
                "start": meeting_dt.isoformat(),
            }

            # Minutes appear in the same row once published: a direct
            # DownloadFile PDF link, else the doctype=2 viewer. Keep the
            # DownloadFile form -- the ViewDocument translation 404s for
            # meeting-level docs on this product family (verified on the
            # OnBase sibling, Aug 2026).
            minutes_link = row.find("a", href=_MINUTES_DOC_RE)
            if minutes_link:
                minutes_href = minutes_link.get("href")
                if not isinstance(minutes_href, str):
                    minutes_href = ""
                meeting_ref["minutes_url"] = (
                    self.base_url + minutes_href
                    if minutes_href.startswith("/")
                    else minutes_href
                )
            else:
                viewer_link = row.find(
                    "a",
                    href=lambda x: bool(x and "ViewMeeting" in x and "doctype=2" in x),
                )
                if viewer_link:
                    meeting_ref["minutes_url"] = self._site_url(
                        f"/Meetings/ViewMeeting?id={meeting_id}&doctype=2"
                    )

            meetings.append(meeting_ref)

        return meetings

    # ------------------------------------------------------------------
    # Meeting detail (accessible agenda + attachments)
    # ------------------------------------------------------------------

    async def _fetch_meeting_detail(self, ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        meeting_id = ref["meeting_id"]
        agenda_url = self._site_url(
            f"/Meetings/ViewMeetingAgenda?meetingId={meeting_id}&type=agenda"
        )

        response = await self._get(agenda_url)
        html = await response.text()

        parsed = parse_agendaonline_html(html, agenda_url)
        items = parsed.get("items", [])

        if items:
            items = await self._fetch_attachments(items, meeting_id)

        meeting = {
            "vendor_id": meeting_id,
            "title": ref.get("title", ""),
            "start": ref.get("start", ""),
        }

        if ref.get("minutes_url"):
            meeting["minutes_url"] = ref["minutes_url"]

        if self.body_name:
            meeting.setdefault("metadata", {})["body"] = self.body_name

        if items:
            meeting["items"] = items
            meeting["agenda_url"] = agenda_url

        return meeting

    # ------------------------------------------------------------------
    # Attachment fetching
    # ------------------------------------------------------------------

    async def _fetch_attachments(
        self, items: List[Dict[str, Any]], meeting_id: str
    ) -> List[Dict[str, Any]]:
        semaphore = asyncio.Semaphore(5)

        async def fetch_item(item: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                item_id = item.get("vendor_item_id")
                if not item_id:
                    return item

                detail_url = self._site_url(
                    f"/Meetings/ViewMeetingAgendaItem?meetingId={meeting_id}"
                    f"&itemId={item_id}&isSection=false&type=agenda"
                )

                try:
                    response = await self._get(detail_url)
                    html = await response.text()
                    attachments = self._parse_attachments(html)
                    if attachments:
                        item["attachments"] = attachments
                except Exception as e:
                    logger.debug(
                        "failed to fetch item attachments",
                        vendor="agendaonline",
                        slug=self.slug,
                        item_id=item_id,
                        error=str(e),
                    )
                return item

        return list(await asyncio.gather(*[fetch_item(i) for i in items]))

    def _parse_attachments(self, html: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        attachments = []
        for link in soup.find_all(
            "a",
            href=lambda x: bool(
                x and "DownloadFile" in x and "isAttachment=True" in x
            ),
        ):
            href_value = link.get("href")
            if not isinstance(href_value, str):
                continue
            href = href_value
            name = link.get_text(strip=True)
            full_url = self.base_url + href if href.startswith("/") else href
            full_url = _translate_downloadfile_to_viewdocument(full_url)
            if not name:
                m = re.search(r'/DownloadFile/([^?]+)', href)
                if m:
                    name = unquote(m.group(1))
            attachments.append({
                "name": name or "Attachment",
                "url": full_url,
                "type": "pdf" if ".pdf" in href.lower() else "unknown",
            })
        return attachments
