"""
Async Municode Adapter - API integration for Municode municipal meetings

Cities using Municode: Columbus GA, Tomball TX, Los Gatos CA, Cedar Park TX, and many others
Platform owned by CivicPlus, uses REST API + HTML agenda packets.

Three integration modes:

  Subdomain API (columbus-ga, tomball-tx):
    - API base: https://{slug}.municodemeetings.com
    - HTML packet: https://meetings.municode.com/adaHtmlDocument/index?cc={CITY_CODE}&me={GUID}&ip=True
    - PDF packet: https://mccmeetings.blob.core.usgovcloudapi.net/{slug-no-hyphens}-pubu/MEET-Packet-{GUID}.pdf

  Self-hosted API (LAKECTYFL -> lcfla.com):
    - Same REST API as subdomain mode, but on the city's own domain (Drupal aha_restapi_server module)
    - Configured via base_url in municode_sites.json
    - API base: https://{city-domain} (e.g., https://www.lcfla.com)
    - Same meeting list/details endpoints, same HTML agenda URLs

  PublishPage HTML (CPTX, etc.):
    - Listing: https://meetings.municode.com/PublishPage/index?cid={CODE}&ppid=0&p=-1
    - Uses city code as slug directly (e.g., "CPTX" for Cedar Park TX)
"""

import asyncio
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from config import config, get_logger
from exceptions import VendorHTTPError
from pipeline.protocols import MetricsCollector
from vendors.adapters.base_adapter_async import AsyncBaseAdapter
from vendors.adapters.parsers.municode_parser import parse_html_agenda

logger = get_logger(__name__).bind(component="vendor")

MUNICODE_CONFIG_FILE = "data/municode_sites.json"


def _load_municode_config() -> Dict[str, Any]:
    return AsyncBaseAdapter._load_vendor_config(MUNICODE_CONFIG_FILE)


_BLOB_GUID_RE = re.compile(r'MEET-(?:Agenda|Packet|Minutes)-([a-f0-9-]{32,36})\.pdf', re.IGNORECASE)

# Extract meeting GUID from adaHtmlDocument me= parameter
_ADA_ME_RE = re.compile(r'[?&]me=([a-f0-9-]{32,36})', re.IGNORECASE)


class _CurlCffiResponse:
    """Wraps curl_cffi response to match aiohttp.ClientResponse interface."""

    def __init__(self, resp):
        self._resp = resp
        self.status = resp.status_code

    async def text(self):
        return self._resp.text

    async def json(self, **kwargs):
        return self._resp.json(**kwargs)

    async def read(self):
        return self._resp.content


class AsyncMunicodeAdapter(AsyncBaseAdapter):
    """Async adapter for cities using Municode platform."""

    def __init__(self, city_slug: str, city_code: Optional[str] = None, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="municode", metrics=metrics)

        self._all_config = _load_municode_config()
        self._discovered_city_code: Optional[str] = None
        self._is_drupal = False
        self._drupal_url: Optional[str] = None
        self._proxy: Optional[str] = None
        self._curl_session = None
        self._tunnel_down = False

        # Check config for this slug (try slug directly, then uppercase variant)
        slug_config = self._all_config.get(self.slug, self._all_config.get(self.slug.upper(), {}))

        # Four modes, checked in priority order:
        # 1. Drupal listing: CivicPlus Drupal 10 site hosts meeting listings (Cloudflare-protected)
        #    Municode still hosts HTML agendas at meetings.municode.com/adaHtmlDocument
        # 2. Self-hosted API: city runs Municode REST API on their own domain (base_url in config)
        # 3. Subdomain API: slug has hyphens -> {slug}.municodemeetings.com
        # 4. PublishPage: short city code slug -> meetings.municode.com/PublishPage/
        if slug_config.get("drupal_url"):
            # Drupal mode: scrape city's Drupal meeting listing, enrich via meetings.municode.com
            self._is_publish_page = False
            self._is_drupal = True
            self._drupal_url = slug_config["drupal_url"].rstrip("/")
            self.base_url = "https://meetings.municode.com"
            self._city_code_override = city_code or slug_config.get("city_code") or self.slug.upper()
            self._proxy = config.RESIDENTIAL_PROXY
        elif slug_config.get("base_url"):
            # Self-hosted API mode: same REST API as subdomain, different domain
            self._is_publish_page = False
            self.base_url = slug_config["base_url"].rstrip("/")
            self._city_code_override = city_code or slug_config.get("city_code") or self.slug.upper()
        elif self._detect_publish_page_mode(self.slug):
            # PublishPage mode: slug IS the city code
            self._is_publish_page = True
            self.base_url = "https://meetings.municode.com"
            self._city_code_override = self.slug.upper()
        else:
            # Subdomain API mode
            self._is_publish_page = False
            self.base_url = f"https://{self.slug}.municodemeetings.com"
            self._city_code_override = city_code or slug_config.get("city_code")

        # Load site-specific config
        self._site_config = slug_config

    def _detect_publish_page_mode(self, slug: str) -> bool:
        """Detect if slug is a city code for PublishPage vs subdomain slug.

        City codes: short (2-8 chars), no hyphens, alphanumeric (e.g., CPTX, COLUMGA)
        Subdomain slugs: longer, have hyphens (e.g., columbus-ga, tomball-tx)
        """
        # If it has a hyphen, it's a subdomain slug
        if "-" in slug:
            return False
        # Short alphanumeric strings are likely city codes
        if len(slug) <= 10 and slug.replace("-", "").isalnum():
            return True
        return False

    @property
    def city_code(self) -> str:
        """Get city code (override > discovered > derived)."""
        if self._city_code_override:
            return self._city_code_override
        if self._discovered_city_code:
            return self._discovered_city_code
        # Default derivation
        return self.slug.replace('-', '').upper()

    def _extract_city_code_from_url(self, url: str) -> Optional[str]:
        """
        Extract city code from a URL containing cc= parameter or blob path.

        Examples:
            ?cc=COLUMGA&me=... -> COLUMGA
            /columga-meet-{guid}/... -> COLUMGA (uppercased)
        """
        # Try cc= parameter
        cc_match = re.search(r'[?&]cc=([A-Z0-9]+)', url, re.IGNORECASE)
        if cc_match:
            return cc_match.group(1).upper()

        # Try blob storage path pattern: {code}-meet-{guid} or {code}-pubu
        blob_match = re.search(r'/([a-z0-9]+)-(meet|pubu)-', url, re.IGNORECASE)
        if blob_match:
            return blob_match.group(1).upper()

        return None

    @staticmethod
    def _extract_guid_from_blob_url(*urls: Optional[str]) -> Optional[str]:
        """Extract 32-char hex GUID from Municode blob PDF URLs."""
        for url in urls:
            if not url:
                continue
            m = _BLOB_GUID_RE.search(url)
            if m:
                return m.group(1).replace("-", "")
        return None

    def _try_discover_city_code(self, data: Any) -> None:
        """Discover city code from API response URLs containing cc= or blob paths."""
        if self._discovered_city_code:
            return

        # Fields that might contain URLs with city codes
        url_fields = [
            'AgendaLinksHtmlURL', 'AgendaLinksURL', 'PacketLinksHtmlURL',
            'PacketLinksURL', 'MinutesLinksHtmlURL', 'MinutesLinksURL'
        ]

        if isinstance(data, dict):
            for field in url_fields:
                url = data.get(field)
                if url:
                    code = self._extract_city_code_from_url(str(url))
                    if code:
                        self._discovered_city_code = code
                        logger.debug("discovered city code from API", vendor="municode", slug=self.slug, city_code=code, field=field)
                        return

        elif isinstance(data, list):
            # Try first few items
            for item in data[:3]:
                if isinstance(item, dict):
                    self._try_discover_city_code(item)

    # -- Drupal mode: curl_cffi + residential proxy for Cloudflare bypass --
    # Routes ALL requests to the Drupal domain through the proxy (listing pages,
    # /media/ PDFs, etc.). Requests to other domains (meetings.municode.com,
    # mccmeetings.blob) go through normal aiohttp.

    async def _get_curl_session(self):
        """Lazy-init a reusable curl_cffi async session."""
        if self._curl_session is None:
            from curl_cffi.requests import AsyncSession
            self._curl_session = AsyncSession(impersonate="chrome")
        return self._curl_session

    def _is_drupal_domain(self, url: str) -> bool:
        """Check if URL is on the Drupal site (needs proxy)."""
        return bool(self._drupal_url and self._drupal_url in url)

    async def _request(self, method: str, url: str, **kwargs):
        """Route Drupal domain requests through residential proxy.

        Requests to the city's Drupal site (listings, /media/ PDFs) go through
        curl_cffi with Chrome TLS fingerprint to bypass Cloudflare.
        Everything else (meetings.municode.com, blob storage) uses normal aiohttp.
        """
        if not self._is_drupal or not self._is_drupal_domain(url) or not self._proxy:
            return await super()._request(method, url, **kwargs)

        if self._tunnel_down:
            raise VendorHTTPError(
                "Residential proxy tunnel is down, skipping",
                vendor=self.vendor, url=url, city_slug=self.slug,
            )

        session = await self._get_curl_session()
        start_time = time.time()

        try:
            resp = await session.request(
                method, url,
                proxies={"https": self._proxy, "http": self._proxy},
                timeout=300,  # 5 min -- large PDFs (25MB+) through residential proxy
            )
            duration = time.time() - start_time

            logger.debug(
                "drupal request (curl_cffi)",
                vendor="municode", slug=self.slug,
                status_code=resp.status_code,
                duration_seconds=round(duration, 2),
            )

            if resp.status_code >= 400:
                raise VendorHTTPError(
                    f"HTTP {resp.status_code}",
                    vendor=self.vendor, status_code=resp.status_code,
                    url=url, city_slug=self.slug,
                )

            return _CurlCffiResponse(resp)

        except VendorHTTPError:
            raise
        except Exception as e:
            err_str = str(e).lower()
            if "connection refused" in err_str or "socks" in err_str or "proxy" in err_str:
                self._tunnel_down = True
                logger.warning("residential proxy tunnel down", vendor="municode", slug=self.slug, error=str(e))
            raise VendorHTTPError(
                f"Drupal fetch failed: {e}",
                vendor=self.vendor, url=url, city_slug=self.slug,
            ) from e

    async def _fetch_drupal_meetings(self, days_back: int, days_forward: int) -> List[Dict[str, Any]]:
        """Fetch meetings from CivicPlus Drupal site, then enrich with HTML agenda items.

        Scrapes the Drupal Views table at /meetings/recent (paginated, newest-first).
        Extracts meeting GUIDs from municode/blob URLs in the table, then enriches
        through the standard meetings.municode.com/adaHtmlDocument pipeline.
        """
        start_date, end_date = self._date_range(days_back, days_forward)

        all_meetings: List[Dict[str, Any]] = []

        # Scrape /meetings/recent with pagination (newest-first)
        page = 0
        while page < 50:
            url = f"{self._drupal_url}/meetings/recent?page={page}"
            try:
                response = await self._get(url)
                html = await response.text()
            except Exception as e:
                logger.warning("drupal page fetch failed", vendor="municode", slug=self.slug, page=page, error=str(e))
                break

            page_meetings = await asyncio.to_thread(self._parse_drupal_table, html)
            if not page_meetings:
                break

            all_meetings.extend(page_meetings)

            # Stop paginating when oldest meeting on page is before our window
            dates = [m["_parsed_date"] for m in page_meetings if m.get("_parsed_date")]
            if dates and min(dates) < start_date:
                break

            page += 1

        # Also grab upcoming meetings (usually just 1 page)
        try:
            response = await self._get(f"{self._drupal_url}/meetings")
            html = await response.text()
            upcoming = await asyncio.to_thread(self._parse_drupal_table, html)
            all_meetings.extend(upcoming)
        except Exception as e:
            logger.debug("upcoming meetings page failed", vendor="municode", slug=self.slug, error=str(e))

        # Deduplicate by vendor_id
        seen: set[str] = set()
        unique: List[Dict[str, Any]] = []
        for m in all_meetings:
            vid = m["vendor_id"]
            if vid not in seen:
                seen.add(vid)
                unique.append(m)

        # Filter by date range
        filtered: List[Dict[str, Any]] = []
        for meeting in unique:
            meeting_date = meeting.get("_parsed_date")
            if meeting_date:
                if start_date <= meeting_date <= end_date:
                    filtered.append(meeting)
            else:
                # Include meetings with unparseable dates
                filtered.append(meeting)

        logger.info(
            "drupal meetings fetched",
            vendor="municode", slug=self.slug,
            total_scraped=len(unique),
            in_range=len(filtered),
            pages_scraped=page + 1,
        )

        # Enrich meetings with HTML agenda items (same path as PublishPage)
        semaphore = asyncio.Semaphore(5)

        async def enrich_meeting(meeting: Dict[str, Any]) -> None:
            if meeting.get("meeting_status") in ("cancelled", "postponed", "deferred"):
                return
            guid = meeting.get("_meeting_guid")
            async with semaphore:
                # Path 1: GUID available -- fetch structured HTML agenda from meetings.municode.com
                if guid:
                    html_url = self._build_html_packet_url(guid)
                    items_data = None
                    try:
                        items_data = await self._fetch_html_agenda_items(html_url)
                    except Exception as e:
                        logger.debug("HTML agenda fetch failed, trying PDF",
                                     vendor="municode", slug=self.slug, error=str(e))

                    if items_data and items_data.get("items"):
                        meeting["items"] = items_data["items"]
                        if items_data.get("participation"):
                            meeting["participation"] = items_data["participation"]
                        meeting["agenda_url"] = html_url
                        logger.info(
                            "found agenda items",
                            vendor="municode", slug=self.slug,
                            title=meeting.get("title", "")[:50],
                            count=len(items_data["items"]),
                        )
                        return

                # Path 2: no GUID or HTML agenda empty -- chunk the PDF packet
                if meeting.get("packet_url"):
                    chunked = await self._chunk_agenda_then_packet(
                        packet_url=meeting["packet_url"],
                        vendor_id=guid or meeting["vendor_id"],
                    )
                    if chunked:
                        meeting["items"] = chunked

        await asyncio.gather(*[enrich_meeting(m) for m in filtered])

        # Clean up internal fields
        for meeting in filtered:
            meeting.pop("_parsed_date", None)
            meeting.pop("_meeting_guid", None)

        return filtered

    def _parse_drupal_table(self, html: str) -> List[Dict[str, Any]]:
        """Parse CivicPlus Drupal Views meeting table.

        Three schema variants seen in the wild (same column order, different field names):
          DELREYOAKS-style: field-smart-date, title, nothing, nothing-1, nothing-2, nothing-3, view-node
          ROLLWDTX-style:   field-calendar-date, title, field-agendas, field-packets-link,
                            field-minutes, field-video-link, view-node
          DESCHUTES-style:  field-calendar-date-1, title, field-agendas, field-packets,
                            field-minutes, field-video-link, view-node

        Drupal Views auto-numbers field classes (-1, -2, ...) when the same field appears
        in multiple places (e.g. upcoming + past tables on one page), and field names
        sometimes vary (`field-packets-link` vs `field-packets`). Match by class PREFIX
        so any -N or trimmed variant resolves to the same logical column.
        """
        soup = BeautifulSoup(html, "html.parser")
        meetings: List[Dict[str, Any]] = []

        table = soup.find("table", class_="views-table")
        if not table:
            return []

        def find_by_prefix(cell_map: Dict[str, Any], *prefixes: str) -> Any:
            for prefix in prefixes:
                for key, cell in cell_map.items():
                    if key.startswith(prefix):
                        return cell
            return None

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            # Map cells by CSS class prefix
            cell_map: Dict[str, Any] = {}
            for cell in cells:
                for cls in cell.get("class", []):
                    if cls.startswith("views-field-"):
                        cell_map[cls] = cell
                        break

            # Date: match any "views-field-field-{smart,calendar}-date[-N]" variant
            date_cell = find_by_prefix(
                cell_map,
                "views-field-field-smart-date",
                "views-field-field-calendar-date",
            )
            title_cell = cell_map.get("views-field-title")
            if not date_cell or not title_cell:
                continue

            title = title_cell.get_text(strip=True)
            parsed_date = self._parse_drupal_date(date_cell.get_text(strip=True))

            # Agenda: try named-field first ("views-field-field-agendas[-N]"), then
            # fall back to DELREYOAKS' generic "views-field-nothing".
            agenda_cell = find_by_prefix(cell_map, "views-field-field-agendas") or cell_map.get(
                "views-field-nothing"
            )
            # Packet: same pattern. The "views-field-field-packets" prefix matches both
            # "field-packets" (Deschutes) and "field-packets-link" (Rollingwood) cleanly.
            packet_cell = find_by_prefix(cell_map, "views-field-field-packets") or cell_map.get(
                "views-field-nothing-1"
            )

            meeting_guid = None
            agenda_url = None
            packet_url = None

            for cell in [agenda_cell, packet_cell]:
                if not cell:
                    continue
                for link in cell.find_all("a", href=True):
                    href = link["href"]

                    # HTML agenda on meetings.municode.com
                    if "adaHtmlDocument" in href:
                        me_match = _ADA_ME_RE.search(href)
                        if me_match:
                            meeting_guid = me_match.group(1).replace("-", "")
                        if not agenda_url:
                            agenda_url = href

                    # PDF on Municode blob storage
                    elif "mccmeetings.blob" in href:
                        guid = self._extract_guid_from_blob_url(href)
                        if guid:
                            meeting_guid = meeting_guid or guid
                        if not packet_url:
                            packet_url = href

                    # Local Drupal media (Planning Commission, committees)
                    elif href.startswith("/media/"):
                        if not packet_url:
                            packet_url = f"{self._drupal_url}{href}"

            vendor_id = meeting_guid or (
                f"{parsed_date.strftime('%Y%m%d') if parsed_date else 'nodate'}_"
                f"{title[:30].replace(' ', '_')}"
            )

            meeting: Dict[str, Any] = {
                "vendor_id": vendor_id,
                "title": title,
                "start": parsed_date.isoformat() if parsed_date else None,
                "_parsed_date": parsed_date,
                "_meeting_guid": meeting_guid,
            }

            if agenda_url:
                meeting["agenda_url"] = agenda_url
            if packet_url:
                meeting["packet_url"] = packet_url

            meeting_status = self._parse_meeting_status(title)
            if meeting_status:
                meeting["meeting_status"] = meeting_status

            meetings.append(meeting)

        return meetings

    def _parse_drupal_date(self, date_text: str) -> Optional[datetime]:
        """Parse Drupal Views date cell. Two known formats:
          smart_date:    'Mar 24, 2026 | 6pm' or 'Mar 18, 2026 | 2pm - 3pm'   (separator '|')
          calendar-date: '04/15/2026 - 7:00pm'                                (separator '-')
        """
        if not date_text:
            return None

        # Split date and time. smart_date uses '|', calendar-date uses ' - '
        # (but ' - ' also appears inside smart_date ranges, so try '|' first).
        if "|" in date_text:
            date_part, _, time_part = date_text.partition("|")
        else:
            date_part, _, time_part = date_text.partition(" - ")
        date_part = date_part.strip()
        time_part = time_part.strip()

        parsed: Optional[datetime] = None
        for fmt in ("%b %d, %Y", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(date_part, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

        if time_part:
            start_time = time_part.split(" - ")[0].strip()
            time_match = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", start_time, re.I)
            if time_match:
                hour = int(time_match.group(1))
                minute = int(time_match.group(2) or "0")
                ampm = time_match.group(3).lower()
                if ampm == "pm" and hour != 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                parsed = parsed.replace(hour=hour, minute=minute)

        return parsed

    def _build_html_packet_url(self, meeting_guid: str) -> str:
        """Build HTML agenda packet URL with full attachments (ip=True)."""
        return f"https://meetings.municode.com/adaHtmlDocument/index?cc={self.city_code}&me={meeting_guid}&ip=True"

    def _build_pdf_packet_url(self, meeting_guid: str) -> str:
        """Build PDF packet URL as fallback."""
        slug_clean = self.slug.replace('-', '')
        return f"https://mccmeetings.blob.core.usgovcloudapi.net/{slug_clean}-pubu/MEET-Packet-{meeting_guid}.pdf"

    def _parse_calendar_date(self, calendar_date: list[Any]) -> Optional[datetime]:
        """Parse CalendarDate which varies by city.

        Two known formats:
        - List of ints: [year, month, day, hour?, minute?, second?, ms?]
        - List of dicts: [{"FromDate": "2026-03-18 16:00:00", ...}]
        """
        if not isinstance(calendar_date, list) or not calendar_date:
            if calendar_date is not None:
                logger.warning(
                    "CalendarDate is not a list",
                    vendor="municode",
                    slug=self.slug,
                    calendar_date_type=type(calendar_date).__name__,
                )
            return None

        first = calendar_date[0]

        # Dict format: extract FromDate string
        if isinstance(first, dict):
            from_date = first.get("FromDate")
            if not from_date:
                return None
            try:
                return datetime.strptime(from_date, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError) as e:
                logger.warning("failed to parse CalendarDate FromDate", vendor="municode", slug=self.slug, from_date=from_date, error=str(e))
                return None

        # Int list format: [year, month, day, ...]
        if len(calendar_date) < 3:
            return None
        try:
            padded = (calendar_date + [0, 0, 0])[:6]
            return datetime(*padded)
        except (ValueError, TypeError) as e:
            logger.warning("failed to parse CalendarDate", vendor="municode", slug=self.slug, calendar_date=calendar_date, error=str(e))
            return None

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings from Municode API, PublishPage HTML, or Drupal listing."""
        if self._is_drupal:
            return await self._fetch_drupal_meetings(days_back, days_forward)
        if self._is_publish_page:
            return await self._fetch_publish_page_meetings(days_back, days_forward)

        # Subdomain API mode
        meetings = await self._fetch_meeting_list(days_back, days_forward)

        logger.info("municode meetings retrieved", vendor="municode", slug=self.slug, count=len(meetings))

        # Process meetings concurrently (with limit)
        results = await self._bounded_gather(
            [self._process_meeting(m) for m in meetings],
            max_concurrent=5,
            return_exceptions=False,
        )

        processed = [r for r in results if r is not None]
        logger.info("municode meetings processed", vendor="municode", slug=self.slug, processed=len(processed), total=len(meetings))

        return processed

    async def _fetch_publish_page_meetings(self, days_back: int, days_forward: int) -> List[Dict[str, Any]]:
        """Fetch meetings from PublishPage HTML table, then enrich with HTML agenda items.

        Supports multi-ppid cities (e.g. Meridian ID): config `ppid` may be a string
        OR a list of UUIDs, one per body (Council, Planning, Historic, etc.). Each
        ppid surfaces a distinct meeting set; we merge and dedupe by meeting GUID.
        """
        start_date, end_date = self._date_range(days_back, days_forward)

        # PublishPage URL: cid=CITYCODE, ppid for page ID (0 default, specific UUIDs per body).
        # ppid may be a single string or a list -- normalize to list for uniform handling.
        ppid_cfg = self._site_config.get("ppid", "0")
        ppids = ppid_cfg if isinstance(ppid_cfg, list) else [ppid_cfg]

        try:
            all_meetings: List[Dict[str, Any]] = []
            for ppid in ppids:
                base_publish_url = f"{self.base_url}/PublishPage/index?cid={self.city_code}&ppid={ppid}"

                # p=-1 fetches all meetings, but some ppids 500 on it -- fall back to p=1
                html = None
                for page_param in ["-1", "1"]:
                    url = f"{base_publish_url}&p={page_param}"
                    try:
                        response = await self._get(url)
                        html = await response.text()
                        # Sanity: error pages can return 200 with "Sorry, something went wrong";
                        # treat them as failures to trigger fallback.
                        if "Friendly Error Page" in html or "Sorry, something went wrong" in html:
                            html = None
                            continue
                        break
                    except Exception as e:
                        logger.debug("publish page param failed, trying next", vendor="municode", slug=self.slug, ppid=ppid, p=page_param, error=str(e))

                if not html:
                    logger.warning("publish page attempts failed for ppid", vendor="municode", slug=self.slug, ppid=ppid)
                    continue

                meetings = await asyncio.to_thread(self._parse_publish_page_html, html)
                all_meetings.extend(meetings)

            if not all_meetings:
                logger.error("all publish page attempts failed", vendor="municode", slug=self.slug, ppid_count=len(ppids))
                return []

            # Dedupe by meeting GUID (same meeting could in theory appear under multiple
            # ppids, though in practice each body has its own).
            seen_guids: set[str] = set()
            unique: List[Dict[str, Any]] = []
            for m in all_meetings:
                guid = m.get("_meeting_guid")
                if guid:
                    if guid in seen_guids:
                        continue
                    seen_guids.add(guid)
                unique.append(m)

            # Filter by date range
            filtered = []
            for meeting in unique:
                meeting_date = meeting.get("_parsed_date")
                if meeting_date:
                    if start_date <= meeting_date <= end_date:
                        filtered.append(meeting)
                else:
                    # Include meetings with unparseable dates
                    filtered.append(meeting)

            logger.info(
                "municode PublishPage meetings fetched",
                vendor="municode",
                slug=self.slug,
                ppid_count=len(ppids),
                total=len(unique),
                in_range=len(filtered)
            )

            # Enrich meetings with HTML agenda items using extracted GUIDs
            semaphore = asyncio.Semaphore(5)

            async def enrich_meeting(meeting: Dict[str, Any]) -> None:
                guid = meeting.get("_meeting_guid")
                if not guid:
                    return
                if meeting.get("meeting_status") in ("cancelled", "postponed", "deferred"):
                    return
                async with semaphore:
                    html_url = self._build_html_packet_url(guid)
                    items_data = None
                    try:
                        items_data = await self._fetch_html_agenda_items(html_url)
                    except Exception as e:
                        logger.debug("HTML agenda fetch failed, trying PDF",
                                     vendor="municode", slug=self.slug, error=str(e))

                    if items_data and items_data.get("items"):
                        meeting["items"] = items_data["items"]
                        if items_data.get("participation"):
                            meeting["participation"] = items_data["participation"]
                        meeting["agenda_url"] = html_url
                        logger.info(
                            "found agenda items",
                            vendor="municode",
                            slug=self.slug,
                            title=meeting.get("title", "")[:50],
                            count=len(items_data["items"]),
                        )
                    elif meeting.get("packet_url"):
                        # Use the actual packet_url scraped from the page
                        chunked = await self._chunk_agenda_then_packet(
                            packet_url=meeting["packet_url"], vendor_id=guid
                        )
                        if chunked:
                            meeting["items"] = chunked

            await asyncio.gather(*[enrich_meeting(m) for m in filtered])

            # Clean up internal fields
            for meeting in filtered:
                meeting.pop("_parsed_date", None)
                meeting.pop("_meeting_guid", None)

            return filtered

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("failed to fetch PublishPage", vendor="municode", slug=self.slug, error=str(e))
            return []

    def _parse_publish_page_html(self, html: str) -> List[Dict[str, Any]]:
        """Parse PublishPage HTML table into meeting dicts.

        Column order varies by city (Grand Prairie has venue before date,
        Cedar Park has date before venue). Uses CSS classes on td elements
        to find columns by name instead of hardcoding indices.
        """
        soup = BeautifulSoup(html, "html.parser")
        meetings = []

        table = soup.find("table")
        if not table:
            logger.warning("no table found in PublishPage", vendor="municode", slug=self.slug)
            return []

        rows = table.find_all("tr")
        if len(rows) < 2:
            return []

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue

            try:
                # Find columns by CSS class — order varies across cities
                cell_map = {}
                for cell in cells:
                    classes = cell.get("class", [])
                    for cls in classes:
                        if cls in ("meeting", "date", "time", "venue", "agenda", "packet", "minutes"):
                            cell_map[cls] = cell
                            break

                # Fall back to positional if no CSS classes found
                if not cell_map:
                    cell_map = {
                        "meeting": cells[0],
                        "date": cells[1],
                        "time": cells[2],
                        "venue": cells[3] if len(cells) > 3 else None,
                        "agenda": cells[4] if len(cells) > 4 else None,
                        "packet": cells[5] if len(cells) > 5 else None,
                        "minutes": cells[6] if len(cells) > 6 else None,
                    }

                meeting_type = cell_map.get("meeting", cells[0]).get_text(strip=True)
                date_str = cell_map["date"].get_text(strip=True) if "date" in cell_map else ""
                time_str = cell_map["time"].get_text(strip=True) if "time" in cell_map else ""
                venue_cell = cell_map.get("venue")
                venue = venue_cell.get_text(strip=True) if venue_cell else ""

                parsed_date = self._parse_publish_page_date(date_str, time_str)

                agenda_cell = cell_map.get("agenda")
                packet_cell = cell_map.get("packet")
                minutes_cell = cell_map.get("minutes")

                agenda_link = agenda_cell.find("a", href=True) if agenda_cell else None
                packet_link = packet_cell.find("a", href=True) if packet_cell else None
                minutes_link = minutes_cell.find("a", href=True) if minutes_cell else None

                agenda_url = urljoin(self.base_url, agenda_link["href"]) if agenda_link else None
                packet_url = urljoin(self.base_url, packet_link["href"]) if packet_link else None
                minutes_url = urljoin(self.base_url, minutes_link["href"]) if minutes_link else None

                meeting_guid = self._extract_guid_from_blob_url(agenda_url, packet_url, minutes_url)

                vendor_id = f"{date_str.replace('/', '-')}_{meeting_type[:20]}".replace(" ", "_")

                title = f"{meeting_type} - {date_str}" if meeting_type else date_str

                meeting: Dict[str, Any] = {
                    "vendor_id": vendor_id,
                    "title": title,
                    "start": parsed_date.isoformat() if parsed_date else None,
                    "_parsed_date": parsed_date,
                    "_meeting_guid": meeting_guid,
                }

                if agenda_url:
                    meeting["agenda_url"] = agenda_url
                if packet_url:
                    meeting["packet_url"] = packet_url
                if venue:
                    meeting["location"] = venue

                meeting_status = self._parse_meeting_status(meeting_type)
                if meeting_status:
                    meeting["meeting_status"] = meeting_status

                meetings.append(meeting)

            except (IndexError, AttributeError, KeyError) as e:
                logger.debug("failed to parse PublishPage row", vendor="municode", slug=self.slug, error=str(e))
                continue

        return meetings

    def _parse_publish_page_date(self, date_str: str, time_str: str) -> Optional[datetime]:
        """Parse date and time from PublishPage table cells."""
        if not date_str:
            return None

        # Try common date formats: 1/22/2026, 01/22/2026
        date_formats = ["%m/%d/%Y", "%m/%d/%y"]

        for fmt in date_formats:
            try:
                parsed = datetime.strptime(date_str, fmt)

                # Try to add time if available
                if time_str:
                    time_match = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", time_str, re.I)
                    if time_match:
                        hour = int(time_match.group(1))
                        minute = int(time_match.group(2))
                        ampm = time_match.group(3)
                        if ampm and ampm.upper() == "PM" and hour != 12:
                            hour += 12
                        elif ampm and ampm.upper() == "AM" and hour == 12:
                            hour = 0
                        parsed = parsed.replace(hour=hour, minute=minute)

                return parsed
            except ValueError:
                continue

        return None

    async def _fetch_meeting_list(self, days_back: int, days_forward: int) -> List[Dict[str, Any]]:
        """Fetch meeting list from API with date range."""
        start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=days_forward)).strftime("%Y-%m-%d")

        url = f"{self.base_url}/api/v1/public/meeting/list.json"
        params = {"datefrom": start_date, "dateto": end_date}

        try:
            data = await self._get_json(url, params=params)
            meetings = data.get("Meetings", [])

            # Try to discover city code from API response
            if meetings:
                self._try_discover_city_code(meetings)

            return meetings
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.error("failed to fetch meeting list", vendor="municode", slug=self.slug, error=str(e))
            return []

    async def _fetch_meeting_details(self, meeting_id: int) -> Optional[Dict[str, Any]]:
        """Fetch meeting details for doc URLs (varies by city)."""
        url = f"{self.base_url}/api/v1/public/meeting/{meeting_id}/details.json"

        try:
            return await self._get_json(url)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.debug("failed to fetch meeting details", vendor="municode", slug=self.slug, meeting_id=meeting_id, error=str(e))
            return None

    def _extract_meeting_guid(self, meeting: Dict[str, Any]) -> Optional[str]:
        """
        Extract meeting GUID for HTML URL construction.

        OriginMeetingID in API response is the GUID used in URLs.
        Format: "7b067cbee37b476bab57c9ccac496c34" (32 hex chars, no hyphens)
        """
        origin_id = meeting.get("OriginMeetingID", "")
        if origin_id:
            # Remove any hyphens (some may have UUID format)
            return origin_id.replace("-", "")
        return None

    async def _process_meeting(self, meeting: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process a single meeting, fetching HTML agenda if available."""
        meeting_id = meeting.get("MeetingID")
        title = meeting.get("Title", "")
        group_name = meeting.get("GroupName", "")

        # Parse meeting datetime
        calendar_date = meeting.get("CalendarDate", [])
        start_dt = self._parse_calendar_date(calendar_date)
        if not start_dt:
            logger.warning("meeting has no valid date", vendor="municode", slug=self.slug, meeting_id=meeting_id, title=title[:50])
            return None

        # Use group name + title for better meeting title
        full_title = f"{group_name} - {title}" if group_name and title else (group_name or title)

        # Check for meeting status
        meeting_status = self._parse_meeting_status(full_title)

        result: Dict[str, Any] = {
            "vendor_id": str(meeting_id),
            "title": full_title,
            "start": start_dt.isoformat(),
        }

        if meeting_status:
            result["meeting_status"] = meeting_status

        # Use URLs from API response — these are the real URLs from the site
        packet_url = meeting.get("PacketLinksURL") or meeting.get("PacketLinksHtmlURL")
        agenda_api_url = meeting.get("AgendaLinksHtmlURL") or meeting.get("AgendaLinksURL")

        # Get meeting GUID for HTML agenda construction
        meeting_guid = self._extract_meeting_guid(meeting)

        if meeting_guid:
            html_url = agenda_api_url or self._build_html_packet_url(meeting_guid)
            result["agenda_url"] = html_url

            # Skip enrichment for cancelled meetings
            if meeting_status not in ("cancelled", "postponed", "deferred"):
                items_data = await self._fetch_html_agenda_items(html_url)
                if items_data and items_data.get("items"):
                    result["items"] = items_data["items"]
                    if items_data.get("participation"):
                        result["participation"] = items_data["participation"]
                    logger.info("found agenda items", vendor="municode", slug=self.slug, title=full_title[:50], count=len(items_data["items"]))

            # Use packet_url from API if available, not a constructed one
            if packet_url:
                result["packet_url"] = packet_url
        else:
            logger.debug("meeting has no GUID", vendor="municode", slug=self.slug, meeting_id=meeting_id)

        return result

    async def _fetch_html_agenda_items(self, html_url: str) -> Optional[Dict[str, Any]]:
        """Fetch and parse HTML agenda packet for items and participation."""
        try:
            response = await self._get(html_url)
            html = await response.text()

            # Parse in thread to avoid blocking
            parsed = await asyncio.to_thread(parse_html_agenda, html)

            # Stamp portal_url on attachments so users get a stable link
            for item in parsed.get("items", []):
                for att in item.get("attachments", []):
                    if not att.get("portal_url"):
                        att["portal_url"] = html_url

            # Fallback: discover city code from attachment URLs
            if not self._discovered_city_code:
                self._try_discover_city_code_from_items(parsed.get("items", []))

            # Count attachments for logging
            total_attachments = sum(
                len(item.get("attachments", []))
                for item in parsed.get("items", [])
            )

            logger.debug(
                "parsed HTML agenda",
                vendor="municode",
                slug=self.slug,
                items=len(parsed["items"]),
                attachments=total_attachments
            )

            return parsed

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("failed to fetch/parse HTML agenda", vendor="municode", slug=self.slug, url=html_url[:80], error=str(e))
            return None

    def _try_discover_city_code_from_items(self, items: List[Dict[str, Any]]) -> None:
        """Discover city code from attachment URLs in parsed items."""
        for item in items[:3]:
            for att in item.get("attachments", [])[:2]:
                url = att.get("url", "")
                if url and (code := self._extract_city_code_from_url(url)):
                    self._discovered_city_code = code
                    logger.debug("discovered city code from HTML attachment", vendor="municode", slug=self.slug, city_code=code)
                    return


# Confidence: 7/10 - Tested against columbus-ga and tomball-tx HTML samples.
# API response format confirmed through exploration.
# Further city validation recommended before broader rollout.
