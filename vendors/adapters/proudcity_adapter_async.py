"""
Async ProudCity Adapter - WP REST API + HTML scraping for ProudCity municipal sites

Cities using ProudCity: Belvedere CA, Colma CA, Fairfax CA, and hundreds of other
small municipalities.

ProudCity is a WordPress-based white-label gov CMS. All sites share:
- WP REST API at /wp-json/wp/v2/ with a `meetings` custom post type
- Consistent HTML tab structure (#tab-agenda, #tab-agenda-packet, #tab-minutes, #tab-video)
- PDFs hosted on storage.googleapis.com/proudcity/{site}/
- Identical theme/plugin versions across the network
- Meeting taxonomy in class_list (e.g. meeting-taxonomy-city-council-2026)

The REST API returns meetings for ALL bodies (council, planning commission,
committees, etc.) in a single paginated endpoint. No per-body configuration
needed.

IMPORTANT: The WP REST API `date` field is the POST PUBLICATION date, not the
meeting date. The actual meeting date is embedded in the title (e.g.
"City Council Meeting: April 13, 2026"). This adapter extracts the meeting date
from the title, falling back to the post date only if title parsing fails.

IMPORTANT: The REST API list endpoint returns empty `content.rendered` for the
meetings CPT. Document URLs must be extracted by fetching individual meeting pages
(the enrichment step).

Domain auto-discovery: probes common patterns ({slug}.gov, www.{slug}.gov,
{slug}.org, www.{slug}.org) against /wp-json/wp/v2/meetings to find the
site. Optional domain override in data/proudcity_sites.json for edge cases.
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from bs4 import BeautifulSoup, Tag

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from exceptions import VendorHTTPError
from pipeline.protocols import MetricsCollector


PROUDCITY_CONFIG_FILE = "data/proudcity_sites.json"

# Tab anchors on individual meeting pages follow the pattern #tab-{key}.
DOCUMENT_TYPES = [
    ("agenda", "Agenda"),
    ("agenda-packet", "Agenda Packet"),
    ("minutes", "Minutes"),
    ("video", "Video"),
]

_PDF_URL_RE = re.compile(
    r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    re.IGNORECASE,
)
_IFRAME_SRC_RE = re.compile(
    r'<iframe[^>]+src=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_VIDEO_URL_RE = re.compile(
    r'(?:href|src)=["\']([^"\']*(?:youtube\.com|youtu\.be|vimeo\.com|zoom\.us)[^"\']*)["\']',
    re.IGNORECASE,
)

# Date pattern in meeting titles: "Month Day, Year"
_TITLE_DATE_RE = re.compile(
    r'(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2},?\s+\d{4}'
)

# Numbered item at start of text: "1." or "4.A." or "3. "
_ITEM_NUMBER_RE = re.compile(r'^\s*(\d+(?:\.\w+)?)\.\s+(.+)', re.DOTALL)

# Sections that are procedural boilerplate -- no actionable items
_PROCEDURAL_SECTIONS = frozenset({
    "call to order", "roll call", "pledge of allegiance",
    "adjournment", "public comments", "public comment",
    "public comments on non-agenda items", "approval of agenda",
    "adoption of agenda", "meeting protocol", "closed session",
    "commissioner comments/requests", "planning director's report",
    "mayor/city council reports", "reports", "city manager report",
    "council comments", "council member comments", "committee reports",
})


def _load_proudcity_config() -> Dict[str, Any]:
    return AsyncBaseAdapter._load_vendor_config(PROUDCITY_CONFIG_FILE)


class AsyncProudCityAdapter(AsyncBaseAdapter):
    """Async adapter for cities using the ProudCity platform.

    Slug is a clean identifier (e.g. 'cityofbelvedere', 'colma.ca', 'townoffairfaxca').
    Domain is auto-discovered by probing candidate URLs against the WP REST API,
    or overridden via data/proudcity_sites.json for edge cases.
    """

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="proudcity", metrics=metrics)

        site_config = _load_proudcity_config().get(self.slug, {})

        # Domain override from config, or None for auto-discovery at fetch time
        domain_override = site_config.get("domain")
        if domain_override:
            self.base_url: Optional[str] = f"https://{domain_override}"
        else:
            self.base_url = None

    async def _discover_base_url(self) -> Optional[str]:
        async def _validate(response):
            data = await response.json()
            return isinstance(data, list)

        return await super()._discover_base_url(
            probe_path="/wp-json/wp/v2/meetings?per_page=1",
            validate=_validate,
        )

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 28
    ) -> List[Dict[str, Any]]:
        """Fetch meetings via WP REST API, falling back to HTML scrape."""
        if not self.base_url:
            self.base_url = await self._discover_base_url()
        if not self.base_url:
            logger.error("no base url, cannot fetch", vendor="proudcity", slug=self.slug)
            return []

        start_date, end_date = self._date_range(days_back, days_forward)

        meetings_raw = await self._fetch_meetings_rest_api(start_date, end_date)

        if meetings_raw is None:
            logger.warning(
                "REST API unavailable, falling back to HTML scrape",
                vendor="proudcity",
                slug=self.slug,
            )
            meetings_raw = await self._fetch_meetings_html_scrape(start_date, end_date)

        logger.info(
            "proudcity meetings retrieved",
            slug=self.slug,
            count=len(meetings_raw),
            start_date=str(start_date.date()),
            end_date=str(end_date.date()),
        )

        # Enrich each meeting with document URLs from individual pages
        tasks = [self._enrich_meeting(m) for m in meetings_raw]
        enriched = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for idx, meeting in enumerate(enriched):
            if isinstance(meeting, Exception):
                logger.warning(
                    "meeting enrichment failed",
                    vendor="proudcity",
                    slug=self.slug,
                    error=str(meeting),
                    meeting_index=idx,
                )
            elif isinstance(meeting, dict):
                results.append(meeting)

        return results

    # ------------------------------------------------------------------
    # REST API fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_rest_api(
        self, start_date: datetime, end_date: datetime
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch meetings from /wp-json/wp/v2/meetings with pagination.

        Returns None if the endpoint is unavailable, signaling fallback
        to HTML scraping.

        The WP `date` field is post publication time, not meeting time.
        We paginate by publication date (desc) and stop when posts are
        old enough that no upcoming meetings would appear. Meeting date
        filtering happens client-side after title parsing.
        """
        all_meetings: List[Dict[str, Any]] = []
        page = 1
        per_page = 100
        max_pages = 20  # Safety cap to prevent unbounded pagination
        api_url = f"{self.base_url}/wp-json/wp/v2"

        while True:
            url = f"{api_url}/meetings"
            params = {
                "per_page": str(per_page),
                "page": str(page),
                "orderby": "date",
                "order": "desc",
            }

            try:
                response = await self._get(url, params=params)
            except VendorHTTPError as e:
                if page == 1:
                    logger.debug(
                        "meetings endpoint unreachable",
                        vendor="proudcity",
                        slug=self.slug,
                        error=str(e),
                        status_code=getattr(e, "status_code", None),
                    )
                    return None
                break

            try:
                data = await response.json()
            except ValueError:
                break

            if not data:
                break

            page_meetings = []
            for post in data:
                meeting = self._rest_post_to_meeting(post)
                if meeting:
                    page_meetings.append(meeting)
            all_meetings.extend(page_meetings)

            # Stop paginating when all meeting dates on this page are
            # before our window. Uses meeting date from title, not pub date,
            # since some cities bulk-create events months in advance.
            page_meeting_dates = []
            for m in page_meetings:
                parsed = self._parse_date(m.get("start", ""))
                if parsed:
                    page_meeting_dates.append(parsed)

            if page_meeting_dates and max(page_meeting_dates) < start_date:
                break

            # Fallback: if no meeting dates parseable, use publication date
            if not page_meeting_dates:
                cutoff = datetime.now() - timedelta(days=180)
                last_pub = self._parse_date(data[-1].get("date", ""))
                if last_pub and last_pub < cutoff:
                    break

            try:
                total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            except (ValueError, TypeError):
                total_pages = 1
            if page >= total_pages:
                break

            if page >= max_pages:
                logger.warning(
                    "pagination cap reached",
                    vendor="proudcity",
                    slug=self.slug,
                    max_pages=max_pages,
                    meetings_so_far=len(all_meetings),
                )
                break

            page += 1

        # Filter by MEETING date (extracted from title), not publication date
        filtered = []
        for m in all_meetings:
            m_date = self._parse_date(m.get("start", ""))
            if m_date is None:
                filtered.append(m)
            elif start_date <= m_date <= end_date:
                filtered.append(m)

        return filtered

    def _rest_post_to_meeting(self, post: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert a WP REST API meeting post to our standardized schema."""
        post_id = post.get("id")
        if not post_id:
            return None

        title = self._extract_rendered_text(post.get("title", {}))
        if not title:
            return None

        # Meeting date from title is authoritative. Post `date` is publication time.
        meeting_date = self._extract_date_from_title(title)
        if not meeting_date:
            meeting_date = post.get("date", "")
            logger.debug(
                "meeting date not in title, using post date",
                vendor="proudcity",
                slug=self.slug,
                title=title[:80],
                post_date=meeting_date,
            )

        meeting_status = self._parse_meeting_status(title, meeting_date)

        # content.rendered is typically empty in list responses;
        # real docs come from the enrichment step
        content_html = post.get("content", {}).get("rendered", "")
        docs = self._extract_docs_from_html(content_html) if content_html else {}

        meeting_url = post.get("link", "")

        result: Dict[str, Any] = {
            "vendor_id": str(post_id),
            "title": title,
            "start": meeting_date,
        }

        if meeting_status:
            result["meeting_status"] = meeting_status

        if docs.get("agenda"):
            result["agenda_url"] = docs["agenda"]
        if docs.get("agenda-packet"):
            result["packet_url"] = docs["agenda-packet"]
        if docs.get("minutes"):
            result["minutes_url"] = docs["minutes"]

        # Non-standard doc URLs and meeting page URL go into metadata
        extra: Dict[str, str] = {}
        if docs.get("minutes"):
            extra["minutes_url"] = docs["minutes"]
        if docs.get("video"):
            extra["video_url"] = docs["video"]
        if meeting_url:
            extra["meeting_url"] = meeting_url

        # Extract body/committee from taxonomy classes
        body_name = self._extract_body_from_classes(post.get("class_list", []))
        if body_name:
            extra["body"] = body_name

        if extra:
            result["metadata"] = extra

        return result

    # ------------------------------------------------------------------
    # HTML scrape fallback
    # ------------------------------------------------------------------

    async def _fetch_meetings_html_scrape(
        self, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Scrape meeting list pages as fallback when REST API is unavailable.

        Tries common ProudCity meeting page paths.
        """
        candidate_paths = [
            "/city-council-meetings/",
            "/council-meetings/",
            "/meetings/",
        ]

        all_meetings: List[Dict[str, Any]] = []

        for path in candidate_paths:
            try:
                response = await self._get(f"{self.base_url}{path}")
                html = await response.text()
                page_meetings = self._parse_meeting_list_html(html)
                all_meetings.extend(page_meetings)
                if page_meetings:
                    break  # found the meeting list page
            except VendorHTTPError:
                continue

        if not all_meetings:
            logger.warning(
                "no meeting list page found",
                vendor="proudcity",
                slug=self.slug,
            )

        # Client-side date filter
        filtered = []
        for m in all_meetings:
            m_date = self._parse_date(m.get("start", ""))
            if m_date is None:
                filtered.append(m)
            elif start_date <= m_date <= end_date:
                filtered.append(m)

        return filtered

    def _parse_meeting_list_html(self, html: str) -> List[Dict[str, Any]]:
        """Parse meetings from ProudCity meeting list table HTML."""
        meetings = []

        row_re = re.compile(
            r'<tr>\s*<td><a\s+href="([^"]+/meetings/[^"]+/)"\s*[^>]*>([^<]+)</a></td>'
            r'(.*?)</tr>',
            re.DOTALL,
        )

        for match in row_re.finditer(html):
            meeting_url = match.group(1)
            title = match.group(2).strip()
            row_html = match.group(3)

            if meeting_url.startswith("/"):
                meeting_url = f"{self.base_url}{meeting_url}"

            date_str = self._extract_date_from_title(title)
            meeting_status = self._parse_meeting_status(title, date_str)

            docs: Dict[str, str] = {}
            doc_link_re = re.compile(r"href='([^']*#tab-([^']+))'")
            for doc_match in doc_link_re.finditer(row_html):
                doc_url = doc_match.group(1)
                doc_type = doc_match.group(2)
                if doc_url.startswith("/"):
                    doc_url = f"{self.base_url}{doc_url}"
                docs[doc_type] = doc_url

            slug_match = re.search(r'/meetings/([^/]+)/', meeting_url)
            vendor_id = slug_match.group(1) if slug_match else self._generate_fallback_vendor_id(
                title, self._parse_date(date_str) if date_str else None
            )

            result: Dict[str, Any] = {
                "vendor_id": vendor_id,
                "title": title,
                "start": date_str or "",
            }

            if meeting_status:
                result["meeting_status"] = meeting_status
            if docs.get("agenda"):
                result["agenda_url"] = docs["agenda"]
            if docs.get("agenda-packet"):
                result["packet_url"] = docs["agenda-packet"]
            if docs.get("minutes"):
                result["minutes_url"] = docs["minutes"]

            extra: Dict[str, str] = {}
            if docs.get("minutes"):
                extra["minutes_url"] = docs["minutes"]
            if docs.get("video"):
                extra["video_url"] = docs["video"]
            if meeting_url:
                extra["meeting_url"] = meeting_url
            if extra:
                result["metadata"] = extra

            meetings.append(result)

        return meetings

    # ------------------------------------------------------------------
    # Meeting enrichment: HTML items -> agenda PDF -> packet PDF
    # ------------------------------------------------------------------

    async def _enrich_meeting(self, meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch individual meeting page and extract items + documents.

        Fallback chain:
        1. Parse HTML from #tab-agenda-packet for structured items with
           per-item PDF attachments (Fairfax-style). If found, set items
           and agenda_url = meeting page.
        2. If no HTML items: set agenda_url = agenda PDF from #tab-agenda
           for URL-based chunking (PDF may have hyperlinks to staff reports).
        3. If no agenda PDF: set packet_url = packet PDF from
           #tab-agenda-packet for TOC/monolithic chunking.
        """
        if self._minutes_discovery_only and meeting.get("minutes_url"):
            return meeting

        metadata = meeting.get("metadata", {}) or {}
        meeting_url = metadata.get("meeting_url", "")

        # Always fetch the meeting page -- we need the full HTML
        page_html = ""
        if meeting_url:
            try:
                response = await self._get(meeting_url)
                page_html = await response.text()
            except VendorHTTPError as e:
                logger.debug(
                    "failed to fetch meeting page",
                    vendor="proudcity",
                    slug=self.slug,
                    url=meeting_url,
                    error=str(e),
                )

        if not page_html:
            return meeting

        # Extract tab pane HTML for each document type
        tab_panes = self._extract_tab_panes(page_html)

        # Extract PDF URLs from all tabs (needed for fallback)
        docs = self._extract_docs_from_panes(tab_panes)

        # Always set minutes/video metadata
        if docs.get("minutes"):
            # Direct PDF from the meeting page beats any list-scrape anchor URL
            meeting["minutes_url"] = docs["minutes"]
            meeting.setdefault("metadata", {})["minutes_url"] = docs["minutes"]
        if docs.get("video"):
            meeting.setdefault("metadata", {})["video_url"] = docs["video"]

        if self._minutes_discovery_only:
            return meeting

        # --- Fallback chain ---

        # 1. Try HTML item extraction from agenda-packet tab (richest content)
        packet_pane = tab_panes.get("agenda-packet", "")
        items = self._parse_agenda_items_from_html(packet_pane) if packet_pane else []

        if not items:
            # Also try the agenda tab (some sites put rich content there)
            agenda_pane = tab_panes.get("agenda", "")
            items = self._parse_agenda_items_from_html(agenda_pane) if agenda_pane else []

        items_with_attachments = sum(1 for i in items if i.get("attachments"))

        if items:
            meeting["items"] = items
            logger.info(
                "parsed items from HTML",
                vendor="proudcity",
                slug=self.slug,
                item_count=len(items),
                items_with_attachments=items_with_attachments,
            )

        if items and items_with_attachments:
            # Rich items with per-item PDFs (Fairfax) -- self-sufficient.
            meeting["agenda_url"] = meeting_url
            if docs.get("agenda-packet"):
                meeting["packet_url"] = docs["agenda-packet"]
            return meeting

        # Bare items from HTML (Colma) or no items at all --
        # fall through to agenda URL parse, then packet TOC parse.
        agenda_pdf = docs.get("agenda")
        packet_pdf = docs.get("agenda-packet")
        chunked = await self._chunk_agenda_then_packet(
            agenda_url=agenda_pdf,
            packet_url=packet_pdf,
            vendor_id=meeting.get("vendor_id"),
        )
        if chunked:
            # Chunker produced items with actual content -- use those
            meeting["items"] = chunked
        elif items:
            # Chunker failed but we have bare HTML titles -- keep them
            # so the processor can at least attempt packet-level processing
            pass

        if agenda_pdf:
            meeting["agenda_url"] = agenda_pdf
        if packet_pdf:
            meeting["packet_url"] = packet_pdf

        return meeting

    # ------------------------------------------------------------------
    # HTML agenda item parsing
    # ------------------------------------------------------------------

    def _parse_agenda_items_from_html(self, tab_html: str) -> List[Dict[str, Any]]:
        """Parse structured agenda items from a ProudCity tab pane.

        Looks for numbered items (in <ol>/<li> or numbered paragraphs) with
        optional per-item PDF attachments. Returns pipeline-compatible item
        dicts matching AgendaItemSchema.

        Returns empty list if content isn't structured enough to parse.

        Handles merged section+item paragraphs (e.g. Colma puts
        "CONSENT CALENDAR\\n1. Motion to Approve..." in one <p>).
        """
        soup = BeautifulSoup(tab_html, "html.parser")

        # The rich content lives in the first col-md-9 div
        content = soup.find("div", class_=re.compile(r"col-md-9"))
        if not content:
            content = soup

        # Quick check: enough text to be a real agenda?
        full_text = content.get_text(strip=True)
        if len(full_text) < 100:
            return []

        items: List[Dict[str, Any]] = []
        current_section: Optional[str] = None
        sequence = 0

        for elem in content.children:
            if isinstance(elem, str) or not isinstance(elem, Tag):
                continue

            text = elem.get_text(strip=True)
            if not text:
                continue

            # --- Step 1: Check for section header in any bold child ---
            # Some ProudCity sites merge section header + first item in one <p>.
            # Always check for section name in bold text, then look for items.
            section_name = self._extract_section_name(elem)
            if section_name:
                current_section = section_name
                # After updating section, check if remaining text has a
                # numbered item (merged paragraph case)
                remaining = text[len(section_name):].strip()
                remaining = remaining.lstrip("•·-–—")  # strip bullet chars
                match = _ITEM_NUMBER_RE.match(remaining)
                if match and not self._is_procedural(current_section):
                    item_number = match.group(1)
                    item = self._parse_item_element(
                        elem, item_number, current_section
                    )
                    if item:
                        # Fix title: strip the section prefix
                        item["title"] = match.group(2).strip()
                        if len(item["title"]) > 300:
                            item["title"] = item["title"][:300].rsplit(".", 1)[0] + "."
                        sequence += 1
                        item["sequence"] = sequence
                        items.append(item)
                continue

            # --- Step 2: Skip items in procedural sections ---
            if self._is_procedural(current_section):
                continue

            # --- Step 3: Parse <ol> lists (Fairfax-style) ---
            if elem.name == "ol":
                start_attr = elem.get("start")
                start_num = int(start_attr) if isinstance(start_attr, str) else 1
                for idx, li in enumerate(elem.find_all("li", recursive=False)):
                    item = self._parse_item_element(
                        li, str(start_num + idx), current_section
                    )
                    if item:
                        sequence += 1
                        item["sequence"] = sequence
                        items.append(item)
                continue

            # --- Step 4: Parse numbered paragraphs ---
            if elem.name == "p":
                match = _ITEM_NUMBER_RE.match(text)
                if match:
                    item_number = match.group(1)
                    item = self._parse_item_element(
                        elem, item_number, current_section
                    )
                    if item:
                        sequence += 1
                        item["sequence"] = sequence
                        items.append(item)

        return items

    def _extract_section_name(self, elem: Tag) -> Optional[str]:
        """Extract section header name from bold text in element.

        Returns the section name if found, None otherwise.
        ProudCity section headers: <strong>CONSENT CALENDAR</strong>
        """
        bold = elem.find(["strong", "b"])
        if not bold:
            return None

        bold_text = bold.get_text(strip=True)
        if not bold_text or len(bold_text) < 3 or len(bold_text) > 120:
            return None

        # Should be mostly uppercase
        alpha_chars = [c for c in bold_text if c.isalpha()]
        if not alpha_chars:
            return None
        uppercase_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
        if uppercase_ratio < 0.7:
            return None

        # Reject if it looks like a numbered item title ("4. ANNUAL INVESTMENT")
        if _ITEM_NUMBER_RE.match(bold_text):
            return None

        return bold_text

    def _is_procedural(self, section: Optional[str]) -> bool:
        """Check if current section is procedural boilerplate."""
        if not section:
            return False
        return section.lower().strip() in _PROCEDURAL_SECTIONS

    def _parse_item_element(
        self, elem: Tag, item_number: str, section: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Extract an agenda item from an HTML element (<li> or <p>).

        Returns pipeline-compatible dict or None if element isn't a real item.
        """
        text = elem.get_text(strip=True)
        if not text or len(text) < 5:
            return None

        # For <li> elements, the full text is the title
        # For <p> elements, strip the leading "N. " prefix
        if elem.name == "p":
            match = _ITEM_NUMBER_RE.match(text)
            title = match.group(2).strip() if match else text
        else:
            title = text

        # Clean up title: remove trailing description after first sentence
        # but keep it reasonable length
        if len(title) > 300:
            title = title[:300].rsplit(".", 1)[0] + "."

        # Extract PDF attachments from links within the element
        attachments = []
        for link in elem.find_all("a", href=True):
            href_val = link["href"]
            href = href_val if isinstance(href_val, str) else ""
            if not href:
                continue

            if ".pdf" in href.lower():
                name = link.get_text(strip=True) or "Attachment"
                if href.startswith("/"):
                    href = f"{self.base_url}{href}"
                attachments.append({
                    "name": name,
                    "url": href,
                    "type": "pdf",
                })

        item: Dict[str, Any] = {
            "vendor_item_id": item_number,
            "title": title,
            "sequence": 0,  # caller sets this
            "agenda_number": item_number,
            "attachments": attachments,
        }

        if section:
            item["metadata"] = {"section": section}

        return item

    # ------------------------------------------------------------------
    # Tab pane extraction and document URL extraction
    # ------------------------------------------------------------------

    def _extract_tab_panes(self, html: str) -> Dict[str, str]:
        """Extract raw HTML content for each tab pane from a meeting page.

        Returns dict mapping doc_key -> pane HTML string.
        """
        panes: Dict[str, str] = {}
        for doc_key, _ in DOCUMENT_TYPES:
            pane_re = re.compile(
                rf'id=["\']tab-{re.escape(doc_key)}["\'][^>]*>(.*?)(?=<div[^>]*\bid=["\']tab-|$)',
                re.DOTALL | re.IGNORECASE,
            )
            match = pane_re.search(html)
            if match:
                panes[doc_key] = match.group(1)
        return panes

    def _extract_docs_from_panes(self, panes: Dict[str, str]) -> Dict[str, str]:
        """Extract PDF/video URLs from pre-extracted tab pane HTML."""
        docs: Dict[str, str] = {}

        for doc_key, pane_html in panes.items():
            if doc_key == "video":
                video_match = _VIDEO_URL_RE.search(pane_html)
                if video_match:
                    docs[doc_key] = video_match.group(1)
                elif (iframe_match := _IFRAME_SRC_RE.search(pane_html)):
                    docs[doc_key] = iframe_match.group(1)
            else:
                pdf_match = _PDF_URL_RE.search(pane_html)
                if pdf_match:
                    url = pdf_match.group(1)
                    if url.startswith("/"):
                        url = f"{self.base_url}{url}"
                    docs[doc_key] = url
                elif (iframe_match := _IFRAME_SRC_RE.search(pane_html)):
                    docs[doc_key] = iframe_match.group(1)

        return docs

    def _extract_docs_from_html(self, content_html: str) -> Dict[str, str]:
        """Extract document URLs from REST API rendered content HTML."""
        docs: Dict[str, str] = {}

        for pdf_match in _PDF_URL_RE.finditer(content_html):
            url = pdf_match.group(1)
            url_lower = url.lower()
            if url.startswith("/"):
                url = f"{self.base_url}{url}"

            if "packet" in url_lower:
                docs.setdefault("agenda-packet", url)
            elif "minute" in url_lower:
                docs.setdefault("minutes", url)
            elif "agenda" in url_lower:
                docs.setdefault("agenda", url)

        video_match = _VIDEO_URL_RE.search(content_html)
        if video_match:
            docs.setdefault("video", video_match.group(1))

        return docs

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _extract_rendered_text(self, field: Any) -> str:
        """Extract plain text from a WP REST API rendered field."""
        if isinstance(field, dict):
            raw = field.get("rendered", "")
        elif isinstance(field, str):
            raw = field
        else:
            return ""
        return self._strip_html(raw)

    def _extract_date_from_title(self, title: str) -> Optional[str]:
        """Extract meeting date from title as ISO string.

        Common ProudCity formats:
        - 'City Council Meeting: April 13, 2026'
        - 'Regular Council Meeting -- March 25, 2026'
        - 'Planning Commission: March 19, 2026'
        - 'Special Town Council Meeting: April 2, 2026'
        """
        match = _TITLE_DATE_RE.search(title)
        if match:
            date_text = match.group(0).replace(",", "")
            try:
                dt = datetime.strptime(date_text, "%B %d %Y")
                return dt.isoformat()
            except ValueError:
                pass
        return None

    def _extract_body_from_classes(self, class_list: List[str]) -> Optional[str]:
        """Extract meeting body name from WP taxonomy classes.

        ProudCity tags meetings with classes like:
        - meeting-taxonomy-city-council
        - meeting-taxonomy-planning-commission-2026  (year suffix)
        - meeting-taxonomy-2026-parks               (year prefix)
        - meeting-taxonomy-bicycle-pedestrian-advisory-committee

        Strips 'meeting-taxonomy-' prefix and any 4-digit year segment.
        """
        for cls in class_list:
            if not cls.startswith("meeting-taxonomy-"):
                continue
            body = cls.removeprefix("meeting-taxonomy-")
            # Strip year from either end or middle
            body = re.sub(r'(?:^|-)\d{4}(?:-|$)', '-', body).strip("-")
            if not body:
                continue
            return body.replace("-", " ").title()
        return None
