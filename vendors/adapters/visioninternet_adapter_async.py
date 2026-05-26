"""
Async Vision Internet Adapter - Granicus govAccess CMS calendar widget scraper.

Cities using Vision Internet: Portola Valley CA, and other municipal sites built
on the Vision Internet (now Granicus govAccess) CMS platform.

Architecture:
  Calendar widgets render meetings in HTML tables at configured page paths.
  Each body (Planning Commission, Town Council, etc.) has its own page URL.

  Table structure:
    <table class='listtable'> with <tbody> rows containing:
      .event_title   - title with link to /Home/Components/Calendar/Event/{id}/{navId}
      .event_datetime - visible local time "MM/DD/YYYY H:MM AM/PM" + hidden <time> (UTC)
      .event_agenda  - one or more .agenda_minutes_link PDF links
      .event_minutes - same structure for minutes PDFs

  Document URLs: /home/showpublisheddocument/{docId}/{timestamp}
  Pagination: /-npage-{n} suffix, sorted newest-first (descending).
  Toggle filters: -toggle-all, -toggle-allupcoming, -toggle-allpast, etc.

  The visible date text is local time (preferred over <time> ISO which is UTC).
  Multiple agenda links per meeting are common (main packet + supplementals).

  When a packet PDF is found, the chunker extracts structured agenda items.

Per-site config in data/visioninternet_sites.json for base URL and calendar paths.
"""

import asyncio
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import NavigableString

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector
from config import config
from exceptions import VendorHTTPError


class _CurlCffiResponse:
    """Wraps curl_cffi response to match aiohttp.ClientResponse interface."""

    def __init__(self, resp):
        self._resp = resp
        self.status = resp.status_code
        self.headers = resp.headers

    async def text(self):
        return self._resp.text

    async def read(self):
        return self._resp.content

    async def json(self):
        return self._resp.json()


VISIONINTERNET_CONFIG_FILE = os.path.join(config.DB_DIR, "visioninternet_sites.json")

# Event detail URL: /Home/Components/Calendar/Event/{id}/{navId}
_EVENT_ID_RE = re.compile(r'/Calendar/Event/(\d+)/')

# Local datetime in visible cell text: "MM/DD/YYYY H:MM AM/PM"
_VISIBLE_DATE_RE = re.compile(r'(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s+[AP]M)')


def _load_config() -> Dict[str, Any]:
    return AsyncBaseAdapter._load_vendor_config(VISIONINTERNET_CONFIG_FILE)


class AsyncVisionInternetAdapter(AsyncBaseAdapter):
    """Async adapter for Vision Internet (Granicus govAccess) CMS sites.

    Slug maps to a site entry in data/visioninternet_sites.json with
    base_url and calendar_paths for each body.
    """

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="visioninternet", metrics=metrics)

        site_config = _load_config().get(self.slug, {})
        self.base_url = site_config.get("base_url")
        if not self.base_url:
            raise ValueError(
                f"No base_url configured for visioninternet slug '{self.slug}'. "
                f"Add entry to {VISIONINTERNET_CONFIG_FILE}"
            )

        self.calendar_paths: List[Dict[str, str]] = site_config.get("calendar_paths", [])
        if not self.calendar_paths:
            raise ValueError(
                f"No calendar_paths configured for visioninternet slug '{self.slug}'"
            )

        # Residential proxy for Akamai bypass (curl_cffi with Chrome TLS fingerprint)
        self._proxy = config.RESIDENTIAL_PROXY
        self._curl_session = None
        self._tunnel_down = False

    async def _get_curl_session(self):
        """Lazy-init a reusable curl_cffi async session.

        Registers a closer with AsyncSessionManager so the libcurl handles
        get torn down on shutdown -- without this they hold ESTAB+CLOSE_WAIT
        sockets through the residential proxy and deadlock asyncpg teardown.
        """
        if self._curl_session is None:
            from curl_cffi.requests import AsyncSession
            from vendors.session_manager_async import AsyncSessionManager
            self._curl_session = AsyncSession(impersonate="chrome")
            AsyncSessionManager.register_closer(self._close_curl_session)
        return self._curl_session

    async def _close_curl_session(self) -> None:
        if self._curl_session is not None:
            session, self._curl_session = self._curl_session, None
            await session.close()

    async def _request(self, method: str, url: str, **kwargs):
        """Route through residential proxy via curl_cffi when configured.

        Degrades gracefully when the tunnel is down — logs a warning once
        per sync cycle instead of spamming errors on every request.
        """
        if not self._proxy:
            return await super()._request(method, url, **kwargs)

        # Fast-fail if we already know the tunnel is down this cycle
        if self._tunnel_down:
            raise VendorHTTPError(
                "Residential proxy tunnel is down, skipping",
                vendor=self.vendor, url=url, city_slug=self.slug,
            )

        session = await self._get_curl_session()
        start_time = time.time()

        try:
            logger.debug("vendor request (curl_cffi)", vendor=self.vendor, slug=self.slug, method=method, url=url[:100])
            resp = await session.request(
                method, url,
                proxies={"https": self._proxy, "http": self._proxy},
                timeout=config.VENDOR_HTTP_TIMEOUT,
            )
            duration = time.time() - start_time

            logger.debug(
                "vendor response (curl_cffi)",
                vendor=self.vendor, slug=self.slug,
                status_code=resp.status_code,
                content_length=len(resp.content),
                duration_seconds=round(duration, 2),
            )

            if resp.status_code >= 400:
                self.metrics.vendor_requests.labels(vendor=self.vendor, status=f"http_{resp.status_code}").inc()
                raise VendorHTTPError(
                    f"HTTP {resp.status_code} error",
                    vendor=self.vendor, status_code=resp.status_code,
                    url=url, city_slug=self.slug,
                )

            self.metrics.vendor_requests.labels(vendor=self.vendor, status="success").inc()
            self.metrics.vendor_request_duration.labels(vendor=self.vendor).observe(duration)
            return _CurlCffiResponse(resp)

        except VendorHTTPError:
            raise
        except Exception as e:
            duration = time.time() - start_time

            # Detect tunnel-down errors (connection refused, SOCKS handshake fail)
            err_str = str(e).lower()
            if "connection refused" in err_str or "socks" in err_str or "proxy" in err_str:
                self._tunnel_down = True
                logger.warning(
                    "residential proxy tunnel unreachable, skipping vendor this cycle",
                    vendor=self.vendor, slug=self.slug, proxy=self._proxy,
                )
                raise VendorHTTPError(
                    "Residential proxy tunnel is down",
                    vendor=self.vendor, url=url, city_slug=self.slug,
                ) from e

            self.metrics.vendor_requests.labels(vendor=self.vendor, status="error").inc()
            logger.error("vendor request failed (curl_cffi)", vendor=self.vendor, slug=self.slug, url=url[:100], error=str(e), duration_seconds=round(duration, 2))
            raise VendorHTTPError(f"Request failed: {e}", vendor=self.vendor, url=url, city_slug=self.slug) from e

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 14
    ) -> List[Dict[str, Any]]:
        """Scrape calendar pages for all configured bodies, then enrich with chunker."""
        self._tunnel_down = False  # Reset for each sync cycle
        start_date, end_date = self._date_range(days_back, days_forward)

        # Scrape all body calendars concurrently
        tasks = [
            self._scrape_calendar(cal, start_date, end_date)
            for cal in self.calendar_paths
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_meetings: List[Dict[str, Any]] = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "calendar scrape failed",
                    vendor="visioninternet",
                    slug=self.slug,
                    path=self.calendar_paths[idx].get("path", "unknown"),
                    error=str(result),
                )
            elif isinstance(result, list):
                all_meetings.extend(result)

        # Enrich meetings that have packet PDFs (concurrent, bounded)
        enrich_results = await self._bounded_gather(
            [self._enrich_meeting_from_pdf(m) for m in all_meetings],
            max_concurrent=5,
        )
        for idx, result in enumerate(enrich_results):
            if isinstance(result, Exception):
                logger.warning(
                    "meeting enrichment failed",
                    vendor="visioninternet",
                    slug=self.slug,
                    vendor_id=all_meetings[idx].get("vendor_id", "unknown"),
                    error=str(result),
                )

        logger.info(
            "visioninternet meetings scraped",
            slug=self.slug,
            count=len(all_meetings),
            bodies=len(self.calendar_paths),
            start_date=str(start_date.date()),
            end_date=str(end_date.date()),
        )

        return all_meetings

    # ------------------------------------------------------------------
    # Calendar page scraping with pagination
    # ------------------------------------------------------------------

    async def _scrape_calendar(
        self,
        cal_config: Dict[str, str],
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict[str, Any]]:
        """Scrape a single calendar page (one body) with pagination.

        Pages are sorted newest-first. Stop paginating when all rows
        on a page are older than start_date.
        """
        path = cal_config["path"]
        body = cal_config.get("body", "")

        meetings: List[Dict[str, Any]] = []
        page = 1
        max_pages = 5

        while page <= max_pages:
            url = f"{self.base_url}{path}/-toggle-all"
            if page > 1:
                url += f"/-npage-{page}"

            try:
                response = await self._get(url)
                html = await response.text()
            except VendorHTTPError as e:
                if page == 1:
                    logger.error(
                        "calendar page unreachable",
                        vendor="visioninternet",
                        slug=self.slug,
                        url=url,
                        error=str(e),
                    )
                break

            page_meetings, has_next, past_range = self._parse_calendar_table(
                html, body, start_date, end_date
            )
            meetings.extend(page_meetings)

            if not has_next or past_range:
                break

            page += 1

        return meetings

    # ------------------------------------------------------------------
    # HTML table parsing
    # ------------------------------------------------------------------

    def _parse_calendar_table(
        self,
        html: str,
        body: str,
        start_date: datetime,
        end_date: datetime,
    ) -> Tuple[List[Dict[str, Any]], bool, bool]:
        """Parse calendar table HTML into meeting dicts.

        Returns (meetings, has_next_page, reached_past_range).
        """
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="listtable")
        if not table:
            return [], False, False

        tbody = table.find("tbody")
        if not tbody:
            return [], False, False

        meetings = []
        reached_past = False

        for row in tbody.find_all("tr"):
            meeting = self._parse_row(row, body)
            if not meeting:
                continue

            m_date = self._parse_date(meeting.get("start", ""))
            if m_date:
                if m_date < start_date:
                    # Table is descending -- everything below is older
                    reached_past = True
                    continue
                if m_date > end_date:
                    continue

            meetings.append(meeting)

        # Check for active next-page link
        has_next = False
        pager = soup.find("div", class_="list-pager")
        if pager:
            next_btn = pager.find("a", class_="pg-next-button")
            if next_btn and "disabled" not in (next_btn.get("class") or []):
                has_next = bool(next_btn.get("href"))

        return meetings, has_next, reached_past

    def _parse_row(self, row, body: str) -> Optional[Dict[str, Any]]:
        """Parse a single <tr> into a meeting dict."""
        # -- Title and vendor_id --
        title_cell = row.find("td", class_="event_title")
        if not title_cell:
            return None

        title_link = title_cell.find("a", href=True)
        if not title_link:
            return None

        title_span = title_link.find("span", itemprop="summary")
        title = title_span.get_text(strip=True) if title_span else title_link.get_text(strip=True)

        href = title_link.get("href", "")
        id_match = _EVENT_ID_RE.search(href)
        if not id_match:
            return None
        vendor_id = id_match.group(1)

        # -- Date/time: use visible text (local time), not <time> ISO (UTC) --
        datetime_cell = row.find("td", class_="event_datetime")
        start = None
        if datetime_cell:
            # First text node is the local datetime, before hidden <time> children
            for child in datetime_cell.children:
                if isinstance(child, NavigableString) and child.strip():
                    date_text = child.strip()
                    # Handle time ranges: "03/16/2026 1:30 PM - 5:30 PM"
                    m = _VISIBLE_DATE_RE.search(date_text)
                    if m:
                        parsed = self._parse_date(m.group(1))
                        if parsed:
                            start = parsed.isoformat()
                    break

        if not start:
            return None

        meeting_status = self._parse_meeting_status(title)

        result: Dict[str, Any] = {
            "vendor_id": vendor_id,
            "title": title,
            "start": start,
        }

        if meeting_status:
            result["meeting_status"] = meeting_status

        meta: Dict[str, Any] = {}
        if body:
            meta["body"] = body

        meta["meeting_url"] = urljoin(self.base_url, href)

        # -- Agenda/packet PDFs (first link is primary, rest are supplemental) --
        agenda_urls = self._extract_doc_urls(row, "event_agenda")
        if agenda_urls:
            result["packet_url"] = agenda_urls[0]
            if len(agenda_urls) > 1:
                meta["supplemental_docs"] = agenda_urls[1:]

        # -- Minutes PDF --
        minutes_urls = self._extract_doc_urls(row, "event_minutes")
        if minutes_urls:
            meta["minutes_url"] = minutes_urls[0]

        # -- Recording URL (YouTube etc.) --
        recording_url = self._find_recording_link(row)
        if recording_url:
            meta["recording_url"] = recording_url

        if meta:
            result["metadata"] = meta

        return result

    # ------------------------------------------------------------------
    # PDF enrichment via chunker
    # ------------------------------------------------------------------

    async def _enrich_meeting_from_pdf(self, meeting: Dict[str, Any]) -> None:
        """Download packet PDF and parse for structured agenda items."""
        packet_url = meeting.get("packet_url")
        if not packet_url:
            return

        items = await self._parse_packet_pdf(packet_url, meeting.get("vendor_id"), force_method="toc")
        if items:
            meeting["items"] = items

    # ------------------------------------------------------------------
    # Cell extraction helpers
    # ------------------------------------------------------------------

    def _extract_doc_urls(self, row, cell_class: str) -> List[str]:
        """Extract all document URLs from a table cell.

        Vision Internet calendar cells can have multiple .agenda_minutes_link
        anchors (e.g., main packet + supplemental presentations).
        """
        cell = row.find("td", class_=cell_class)
        if not cell:
            return []

        urls = []
        for link in cell.find_all("a", href=True):
            href = link.get("href", "").strip()
            if href:
                urls.append(urljoin(self.base_url, href))
        return urls

    def _find_recording_link(self, row) -> Optional[str]:
        """Find recording URL in table row.

        Recording links contain text 'Recording' with a non-empty href,
        typically a YouTube URL.
        """
        for link in row.find_all("a", href=True):
            href = link.get("href", "").strip()
            text = link.get_text(strip=True).lower()
            if text == "recording" and href:
                return href
        return None

