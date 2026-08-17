"""
Async OnBase Agenda Online Adapter - Direct integration for Hyland OnBase platform

OnBase Agenda Online is Hyland's civic agenda management product. Deployments vary:
- Hyland Cloud: {city}.hylandcloud.com/{siteId} (San Diego, Tucson, Tampa)
- Self-hosted: {city-domain}/OnBaseAgendaOnline/ (Santa Cruz)
- Via Granicus: Handled by granicus_adapter (Redwood City)

This adapter handles direct OnBase instances (not via Granicus).
Config-based: Sites configured in data/onbase_sites.json, keyed by banana.
Slug should be the city banana (e.g., "sandiegoCA").
"""

import asyncio
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, cast
from urllib.parse import urljoin, urlparse, parse_qs, unquote

from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.html_attrs import string_attr
from vendors.adapters.parsers.granicus_parser import parse_agendaonline_html
from pipeline.protocols import MetricsCollector
from exceptions import VendorHTTPError


ONBASE_CONFIG_FILE = "data/onbase_sites.json"


def _translate_downloadfile_to_viewdocument(url: str) -> str:
    """Translate DownloadFile URL to ViewDocument URL for direct access."""
    if "/Documents/DownloadFile/" not in url:
        return url

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    params = parse_qs(parsed.query)

    path_parts = parsed.path.split("/DownloadFile/")
    if len(path_parts) < 2:
        return url

    # Reconstruct path with site prefix
    prefix = path_parts[0]  # e.g., /211agendaonlinecouncil/Documents
    doc_name = path_parts[1]

    return (
        f"{base_url}{prefix.replace('/Documents', '')}/Documents/ViewDocument/{doc_name}"
        f"?meetingId={params.get('meetingId', [''])[0]}"
        f"&documentType={params.get('documentType', ['1'])[0]}"
        f"&itemId={params.get('itemId', [''])[0]}"
        f"&publishId={params.get('publishId', [''])[0]}"
        f"&isSection={params.get('isSection', ['false'])[0].lower()}"
    )


def _load_onbase_config() -> Dict[str, List[str]]:
    return AsyncBaseAdapter._load_vendor_config(ONBASE_CONFIG_FILE, required=True)


class AsyncOnBaseAdapter(AsyncBaseAdapter):
    """Async adapter for direct OnBase Agenda Online instances."""

    MINUTES_DISCOVERY_SUPPORTED = True
    # OnBase item-detail endpoints are substantially more fragile than their
    # meeting listings.  The shared vendor rate limiter controls cadence, but
    # it deliberately has multiple slots and therefore does not bound a single
    # tenant's fan-out.  Keep tenant work small enough that one slow deployment
    # cannot occupy every global OnBase slot.
    _MEETING_DETAIL_CONCURRENCY = 3
    _ITEM_DETAIL_CONCURRENCY = 3
    _ITEM_DETAIL_FAILURE_BUDGET = 3

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="onbase", metrics=metrics)

        # Load URLs from config - slug is the banana
        config = _load_onbase_config()
        if self.slug not in config:
            raise ValueError(
                f"OnBase not configured for {self.slug}. "
                f"Add entry to {ONBASE_CONFIG_FILE}"
            )

        self.site_urls = [f"https://{path.rstrip('/')}/" for path in config[self.slug]]
        # Shared by all meetings and configured sites handled by this adapter
        # instance.  A per-call semaphore would multiply concurrency whenever
        # several meetings fetch their attachments at the same time.
        self._item_detail_semaphore = asyncio.Semaphore(
            self._ITEM_DETAIL_CONCURRENCY
        )
        logger.info(
            "initialized async adapter",
            component="vendor",
            vendor="onbase",
            city_slug=city_slug,
            site_count=len(self.site_urls)
        )

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings from all configured OnBase sites."""
        start_date, end_date = self._date_range(days_back, days_forward)

        all_meetings = []

        # Fetch from each configured site
        for base_url in self.site_urls:
            try:
                site_meetings = await self._fetch_site_meetings(
                    base_url, start_date, end_date
                )
                all_meetings.extend(site_meetings)
                logger.debug(
                    "fetched from site",
                    vendor="onbase",
                    slug=self.slug,
                    site=base_url,
                    count=len(site_meetings)
                )
            except Exception as e:
                logger.warning(
                    "failed to fetch from site",
                    vendor="onbase",
                    slug=self.slug,
                    site=base_url,
                    error=str(e)
                )

        logger.info(
            "meetings fetched",
            vendor="onbase",
            slug=self.slug,
            site_count=len(self.site_urls),
            total_meetings=len(all_meetings),
            with_items=sum(1 for m in all_meetings if m.get("items"))
        )

        return all_meetings

    async def _fetch_site_meetings(
        self, base_url: str, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch meetings from a single OnBase site."""
        response = await self._get(base_url)
        html = await response.text()

        meetings_data = await asyncio.to_thread(self._parse_meeting_listing, html)
        if not meetings_data:
            return []

        # Filter by date range
        meetings_in_range = []
        for meeting in meetings_data:
            meeting["_base_url"] = base_url  # Track source site
            meeting_date = meeting.get("date")
            if meeting_date and start_date <= meeting_date <= end_date:
                meetings_in_range.append(meeting)
            elif not meeting_date:
                meetings_in_range.append(meeting)

        if not meetings_in_range:
            return []

        if self._minutes_discovery_only:
            meetings = []
            for meeting in meetings_in_range:
                minutes_href = meeting.get("minutes_url")
                if not minutes_href:
                    continue
                minutes_url = urljoin(base_url, minutes_href)
                meetings.append({
                    "vendor_id": meeting["id"],
                    "title": meeting.get("title", "Meeting"),
                    "start": (
                        meeting["date"].isoformat() if meeting.get("date") else ""
                    ),
                    "minutes_url": minutes_url,
                })
            return meetings

        # Fetch meeting details concurrently
        detail_tasks = [
            self._fetch_meeting_detail(meeting)
            for meeting in meetings_in_range
        ]

        results = await self._bounded_gather(
            detail_tasks,
            max_concurrent=self._MEETING_DETAIL_CONCURRENCY,
            return_exceptions=True,
        )

        meetings = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "failed to fetch meeting detail",
                    vendor="onbase",
                    slug=self.slug,
                    meeting_id=meetings_in_range[i].get("id"),
                    error=str(result)
                )
            elif result is not None:
                meetings.append(result)

        return meetings

    def _parse_meeting_listing(self, html: str) -> List[Dict[str, Any]]:
        """Parse meeting listing from OnBase main page (JSON or static HTML)."""
        meetings = []
        seen_ids = set()

        # Method 1: Extract from inline JSON (Durham-style pages embed meeting data)
        json_meetings = re.findall(r'\{"ID":\d+[^}]+\}', html)
        for json_str in json_meetings:
            try:
                data = json.loads(json_str)
                meeting_id = str(data.get("ID"))
                if meeting_id in seen_ids:
                    continue

                # Parse ISO datetime
                time_str = data.get("Time", "")
                meeting_date = None
                if time_str:
                    try:
                        meeting_date = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        meeting_date = meeting_date.replace(tzinfo=None)
                    except ValueError:
                        pass

                meetings.append({
                    "id": meeting_id,
                    "title": data.get("Name", "Meeting"),
                    "date": meeting_date,
                    "has_agenda": data.get("IsAgendaAvailable", False),
                })
                seen_ids.add(meeting_id)
            except (json.JSONDecodeError, KeyError):
                continue

        # Method 2: Extract from static HTML links (fallback)
        if not meetings:
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.find_all(
                "a",
                href=lambda value: bool(value and "ViewMeeting" in value and "id=" in value),
            ):
                href = string_attr(link, "href")
                title = link.get_text(strip=True)

                if not title:
                    continue

                id_match = re.search(r'[?&]id=(\d+)', href)
                if not id_match:
                    continue

                meeting_id = id_match.group(1)
                if meeting_id in seen_ids:
                    continue

                meeting_date = self._extract_date_from_context(link)

                meetings.append({
                    "id": meeting_id,
                    "title": title,
                    "date": meeting_date,
                    "url": href,
                })
                seen_ids.add(meeting_id)

        # Minutes ship in the same listing HTML as direct DownloadFile links;
        # documentType=2 is the product's minutes doctype (mirrors the
        # ViewMeeting doctype=2 viewer). Keep the DownloadFile form: the
        # ViewDocument translation 404s for meeting-level docs (verified
        # Concord, Aug 2026), unlike item attachments.
        if meetings:
            minutes_by_id: Dict[str, str] = {}
            for href_match in re.finditer(r'href="([^"]*/Documents/DownloadFile/[^"]*)"', html):
                href = href_match.group(1).replace("&amp;", "&")
                params = parse_qs(urlparse(href).query)
                if params.get("documentType", [""])[0] != "2":
                    continue
                mid = params.get("meetingId", [""])[0]
                if mid:
                    minutes_by_id.setdefault(mid, href)
            for meeting in meetings:
                minutes_href = minutes_by_id.get(meeting["id"])
                if minutes_href:
                    meeting["minutes_url"] = minutes_href

        return meetings

    def _extract_date_from_context(self, element) -> Optional[datetime]:
        """Extract date from element's surrounding context."""
        # Look for date in parent elements
        for parent in element.parents:
            if parent.name in ["div", "tr", "td", "li", "section"]:
                text = parent.get_text()
                # Try common date patterns
                date_patterns = [
                    r'(\d{1,2}/\d{1,2}/\d{4})',  # 1/13/2026
                    r'(\d{1,2}/\d{1,2}/\d{2})',  # 1/13/26
                    r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})',
                ]
                for pattern in date_patterns:
                    match = re.search(pattern, text)
                    if match:
                        date_str = match.group(1)
                        for fmt in ["%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y"]:
                            try:
                                return datetime.strptime(date_str, fmt)
                            except ValueError:
                                continue
                # Stop after checking a few parents
                if parent.name in ["section", "div"] and "meeting" in parent.get("class", []):
                    break
        return None

    async def _fetch_meeting_detail(self, meeting_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fetch and parse individual meeting detail page.

        OnBase SPA architecture: the ViewMeeting page loads an agenda outline via
        AJAX (ViewAgenda), then each item's attachments load via AJAX (ViewMeetingAgendaItem).
        Both AJAX endpoints require:
          - ASP.NET session cookies (established by visiting ViewMeeting first)
          - X-Requested-With: XMLHttpRequest header
        Without these, the server returns empty page shells.
        """
        meeting_id = meeting_data.get("id")
        if not meeting_id:
            return None
        base_url = meeting_data.get("_base_url", self.site_urls[0])

        # Step 1: Visit the ViewMeeting page to establish ASP.NET session.
        # The session cookies propagate via the shared aiohttp session for this vendor.
        view_meeting_url = f"{base_url}/Meetings/ViewMeeting?id={meeting_id}&doctype=1"
        try:
            await self._get(view_meeting_url)
        except Exception as e:
            logger.debug("failed to establish session", vendor="onbase", slug=self.slug, meeting_id=meeting_id, error=str(e))

        # Step 2: Fetch the agenda outline via the same AJAX endpoint the page JS uses
        xhr_headers = {"X-Requested-With": "XMLHttpRequest"}

        # Try multiple URL formats - different OnBase instances use different endpoints
        urls_to_try = [
            f"{base_url}/Documents/ViewAgenda?meetingId={meeting_id}&type=agenda&doctype=1",
            f"{base_url}/Meetings/ViewMeetingAgenda?meetingId={meeting_id}&type=agenda",
        ]

        best_html = None
        best_url = None
        best_items = []

        for url in urls_to_try:
            try:
                response = await self._get(url, headers=xhr_headers)
                content = await response.text()

                # Skip error pages
                if "internal error" in content.lower() or "error occurred" in content.lower():
                    continue

                # Try parsing
                parsed = await asyncio.to_thread(parse_agendaonline_html, content, url)
                items = parsed.get("items", [])

                # Keep the result with the most items
                if len(items) > len(best_items):
                    best_html = content
                    best_url = str(response.url)
                    best_items = items

                # If we found items, we can stop
                if items:
                    break

            except Exception:
                continue

        if not best_html:
            logger.debug("no valid agenda page found", vendor="onbase", slug=self.slug, meeting_id=meeting_id)
            return None

        html = best_html
        final_url = best_url

        try:
            items = best_items

            # Step 3: Fetch attachments for items (uses same session + XHR header)
            if items:
                items = await self._fetch_item_attachments(items, meeting_id, base_url)

            meeting_date = meeting_data.get("date")
            meeting = {
                "vendor_id": meeting_id,
                "title": meeting_data.get("title", ""),
                "start": meeting_date.isoformat() if meeting_date is not None else "",
            }

            minutes_href = meeting_data.get("minutes_url")
            if minutes_href:
                site = urlparse(base_url)
                meeting["minutes_url"] = (
                    f"{site.scheme}://{site.netloc}{minutes_href}"
                    if minutes_href.startswith("/")
                    else minutes_href
                )

            if items:
                meeting["items"] = items
                meeting["agenda_url"] = final_url
                attachment_count = sum(len(item.get("attachments", [])) for item in items)
                logger.debug(
                    "parsed meeting with items",
                    vendor="onbase",
                    slug=self.slug,
                    meeting_id=meeting_id,
                    item_count=len(items),
                    attachment_count=attachment_count
                )
            else:
                # Try to find packet URL as fallback
                packet_url = self._find_packet_url(html, base_url)
                if packet_url:
                    meeting["packet_url"] = packet_url
                    logger.debug(
                        "no items, using packet fallback",
                        vendor="onbase",
                        slug=self.slug,
                        meeting_id=meeting_id
                    )

            return meeting

        except Exception as e:
            logger.warning(
                "error fetching meeting detail",
                vendor="onbase",
                slug=self.slug,
                meeting_id=meeting_id,
                error=str(e)
            )
            return None

    async def _fetch_item_attachments(
        self, items: List[Dict[str, Any]], meeting_id: str, base_url: str
    ) -> List[Dict[str, Any]]:
        """Fetch attachments for each item from detail pages.

        Uses X-Requested-With header so the server returns the partial HTML
        fragment with DownloadFile attachment links (same as browser AJAX).
        Relies on ASP.NET session cookies established by _fetch_meeting_detail.
        """
        xhr_headers = {"X-Requested-With": "XMLHttpRequest"}
        failure_count = 0
        in_flight = 0
        skipped_count = 0
        circuit_condition = asyncio.Condition()

        async def reserve_request() -> bool:
            """Reserve failure-budget capacity before starting an HTTP call.

            Counting in-flight requests against the budget prevents a wave of
            concurrent timeouts from overshooting it.  Successful and
            permanent-failure requests release their capacity; retryable
            failures consume it for the remainder of this meeting.
            """
            nonlocal in_flight, skipped_count
            async with circuit_condition:
                while (
                    failure_count < self._ITEM_DETAIL_FAILURE_BUDGET
                    and failure_count + in_flight
                    >= self._ITEM_DETAIL_FAILURE_BUDGET
                ):
                    await circuit_condition.wait()
                if failure_count >= self._ITEM_DETAIL_FAILURE_BUDGET:
                    skipped_count += 1
                    return False
                in_flight += 1
                return True

        async def finish_request(*, retryable_failure: bool) -> None:
            nonlocal failure_count, in_flight
            async with circuit_condition:
                in_flight -= 1
                if retryable_failure:
                    failure_count += 1
                circuit_condition.notify_all()

        async def fetch_item(item: Dict[str, Any]) -> Dict[str, Any]:
            item_id = item.get("vendor_item_id")
            if not item_id:
                return item

            if not await reserve_request():
                return item

            detail_url = (
                f"{base_url}/Meetings/ViewMeetingAgendaItem"
                f"?meetingId={meeting_id}&itemId={item_id}&isSection=false&type=agenda"
            )

            async with self._item_detail_semaphore:
                try:
                    response = await self._get(detail_url, headers=xhr_headers)
                    html = await response.text()
                    attachments = self._parse_attachments(html, base_url)
                    if attachments:
                        item["attachments"] = attachments
                except Exception as e:
                    retryable = (
                        isinstance(e, asyncio.TimeoutError)
                        or (
                            isinstance(e, VendorHTTPError)
                            and e.is_retryable
                        )
                    )
                    await finish_request(retryable_failure=retryable)
                    logger.debug(
                        "failed to fetch item attachments",
                        vendor="onbase",
                        slug=self.slug,
                        meeting_id=meeting_id,
                        item_id=item_id,
                        retryable=retryable,
                        error=str(e)
                    )
                    return item

            await finish_request(retryable_failure=False)

            return item

        results = list(
            await self._bounded_gather(
                (fetch_item(item) for item in items),
                max_concurrent=self._ITEM_DETAIL_CONCURRENCY,
                return_exceptions=False,
            )
        )
        if skipped_count:
            logger.warning(
                "onbase item attachment failure budget exhausted",
                vendor="onbase",
                slug=self.slug,
                meeting_id=meeting_id,
                retryable_failures=failure_count,
                failure_budget=self._ITEM_DETAIL_FAILURE_BUDGET,
                skipped_items=skipped_count,
                retained_items=len(results),
            )
        return cast(List[Dict[str, Any]], results)

    def _parse_attachments(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        """Parse attachment links from item detail page.

        The ViewMeetingAgendaItem AJAX response contains DownloadFile links
        that serve the actual document (PDF-converted via the .pdf suffix).
        Keep these URLs as-is -- they are direct download links.
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = []

        parsed_url = urlparse(base_url)
        domain = f"{parsed_url.scheme}://{parsed_url.netloc}"

        for link in soup.find_all("a", href=True):
            href = string_attr(link, "href")
            if "DownloadFile" not in href:
                continue

            name = link.get_text(strip=True)

            # The href OnBase gives us is /Documents/DownloadFile/NAME.pdf?... which
            # returns a JS shim page, not the PDF. Its JS fires a POST to
            # InvokeDownloadAttachment (stashes server-side state) then redirects to
            # /Documents/ViewDocument/NAME.pdf?... — that's the endpoint that
            # actually streams PDF bytes. ViewDocument accepts the same query params
            # and works without session cookies or the Invoke POST, so we skip the
            # shim entirely. /DownloadFileBytes/ works on some older OnBase
            # deployments but 404s on newer ones (Whittier, Concord, Hamilton Co).
            portal_href = href
            download_href = href.replace("/DownloadFile/", "/ViewDocument/")

            def _absolute(h: str) -> str:
                return f"{domain}{h}" if h.startswith("/") else h

            full_url = _absolute(download_href)
            portal_url = _absolute(portal_href)

            if not name:
                match = re.search(r'/(?:DownloadFile|ViewDocument)/([^?]+)', href)
                if match:
                    name = unquote(match.group(1))

            attachments.append({
                "name": name or "Attachment",
                "url": full_url,
                "portal_url": portal_url,
                "type": "pdf",  # OnBase appends .pdf to all DownloadFile URLs
            })

        return attachments

    def _find_packet_url(self, html: str, base_url: str) -> Optional[str]:
        """Find agenda packet PDF URL."""
        soup = BeautifulSoup(html, "html.parser")
        parsed = urlparse(base_url)
        domain = f"{parsed.scheme}://{parsed.netloc}"

        # Look for "Agenda Packet" link
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True).lower()
            href = string_attr(link, "href")

            if "packet" in text and (".pdf" in href.lower() or "DownloadFile" in href):
                if href.startswith("/"):
                    return f"{domain}{href}"
                return href

        return None
