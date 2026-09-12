"""
Async CivicWeb Adapter - eSCRIBE's CivicWeb Portal for municipal meetings

Cities using CivicWeb: Sonoma CA, Calistoga CA, and others on {slug}.civicweb.net.

CivicWeb is eSCRIBE's older portal product (Drupal-based, ASP.NET backend).
Different from escribemeetings.com (newer product with calendar API).

Architecture:
  /Portal/MeetingTypeList.aspx  - Lists ALL bodies and their recent meetings
                                  with IDs, dates, and titles in static HTML.
  /Portal/MeetingInformation.aspx?Id={mid}  - Meeting page with embedded
                                              packet PDF viewer.
  /document/{doc_id}/{name}.pdf?handle={hash}  - Direct PDF download.

The packet PDF is typically a compiled agenda+staff reports document with
a PDF TOC (bookmarks). The chunker handles item extraction via TOC entries.

Minutes are not in the static HTML, but the SPA's own XHR serves them
without authentication:

    GET /Services/MeetingsService.svc/meetings/{Id}/meetingDocuments

{Id} is the same meeting id already scraped from MeetingTypeList. The reply
lists documents with a DocumentType from the portal's packageDocumentTypes
enum; minutes are 9/49 (HTML open), 10 (PDF open), 53/54 (adopted), with
11/12/51 the closed-session counterparts. Each downloads at
{base_url}/document/{Id}. Verified live 2026-09-11 on issaquah, evanston,
hudson, washingtoncounty and beecave.

Per-city: *.civicweb.net returns a bare JSON array while
*.community.diligentoneplatform.com wraps it in {"Documents": [...]};
issaquah and pearland publish type 53 only, evanston and stlouisco publish
some meetings as HTML (type 9) with no PDF at all; hemetca, beaverton and
pullman publish no minutes. Do not record a vendor-wide negative here again:
the note this replaces claimed minutes were unreachable, naming hudson as
verified, and hudson publishes them.

Slug is the civicweb subdomain (e.g. 'sonomacity' for sonomacity.civicweb.net).
"""

import re
from datetime import datetime
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from exceptions import VendorHTTPError
from pipeline.protocols import MetricsCollector

CIVICWEB_CONFIG_FILE = "data/civicweb_sites.json"


# Date in meeting title -- two formats across CivicWeb sites:
#   Sonoma:    "18 Mar 2026" (day month year)
#   Calistoga: "Mar 31 2026" (month day year)
_DATE_DMY_RE = re.compile(r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})')
_DATE_MDY_RE = re.compile(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})')

# Document URL on meeting pages: /document/{id}/{name}.{ext}?handle={hash}
# Most sites serve a native .pdf, but some (e.g. Bee Cave) upload .docx and
# rely on the ?printPdf=true rendering endpoint. Capture the extension so we
# can decide which URL form to feed the chunker.
_DOC_URL_RE = re.compile(
    r'/document/\d+/[^"\'>\s]+?\.(pdf|docx?|rtf)\?handle=[^"\'>\s]+',
    re.IGNORECASE,
)


# Minutes document types from the portal's own packageDocumentTypes config,
# in preference order: adopted PDF, open PDF, then HTML when a city posts no
# PDF. Closed-session types (11, 12, 51, 54) are deliberately excluded.
_MINUTES_DOCUMENT_TYPES = (53, 10, 49, 9)


class AsyncCivicWebAdapter(AsyncBaseAdapter):
    """Async adapter for cities on the CivicWeb portal platform.

    Slug is the civicweb subdomain: 'sonomacity', 'calistoga', etc.
    Non-standard domains (e.g. highbond.com) configured in data/civicweb_sites.json.
    """

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="civicweb", metrics=metrics)
        site_config = self._load_vendor_config(CIVICWEB_CONFIG_FILE).get(self.slug, {})
        self.base_url = site_config.get("base_url", f"https://{self.slug}.civicweb.net")

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    MINUTES_DISCOVERY_SUPPORTED = True

    async def _fetch_minutes_url(self, meeting: Dict[str, Any]) -> None:
        """Set minutes_url from the meeting-documents service, when published."""
        vendor_id = meeting.get("vendor_id")
        if not vendor_id:
            return
        url = f"{self.base_url}/Services/MeetingsService.svc/meetings/{vendor_id}/meetingDocuments"
        try:
            response = await self._get(url)
            payload = await response.json(content_type=None)
        except (VendorHTTPError, ValueError) as e:
            logger.debug(
                "civicweb meeting documents unavailable",
                slug=self.slug,
                vendor_id=vendor_id,
                error=str(e),
            )
            return
        documents = payload if isinstance(payload, list) else (payload or {}).get("Documents") or []
        by_type = {}
        for document in documents:
            if not isinstance(document, dict) or not document.get("Id"):
                continue
            by_type.setdefault(document.get("DocumentType"), document)
        for document_type in _MINUTES_DOCUMENT_TYPES:
            document = by_type.get(document_type)
            if document:
                meeting["minutes_url"] = f"{self.base_url}/document/{document['Id']}"
                return

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 28
    ) -> List[Dict[str, Any]]:
        """Scrape MeetingTypeList for meetings, fetch packet PDFs."""
        start_date, end_date = self._date_range(days_back, days_forward)

        meetings = await self._scrape_meeting_type_list(start_date, end_date)

        if self._minutes_discovery_only:
            # Discovery mode touches neither meeting pages nor PDFs. The
            # vendor_id/title/start tuple must stay byte-identical to the
            # normal path: sweep_minutes recomputes the meeting id from it.
            await self._bounded_gather(
                [self._fetch_minutes_url(m) for m in meetings],
                max_concurrent=5,
            )
            return [m for m in meetings if m.get("minutes_url")]

        logger.info(
            "civicweb meetings scraped",
            slug=self.slug,
            count=len(meetings),
            start_date=str(start_date.date()),
            end_date=str(end_date.date()),
        )

        # Fetch packet PDF URL for each meeting (concurrent, bounded)
        enriched = await self._bounded_gather(
            [self._enrich_meeting(m) for m in meetings],
            max_concurrent=5,
        )
        await self._bounded_gather(
            [self._fetch_minutes_url(m) for m in meetings],
            max_concurrent=5,
        )

        results = []
        for idx, meeting in enumerate(enriched):
            if isinstance(meeting, Exception):
                logger.warning(
                    "meeting enrichment failed",
                    vendor="civicweb",
                    slug=self.slug,
                    error=str(meeting),
                    meeting_index=idx,
                )
            elif isinstance(meeting, dict):
                results.append(meeting)

        # Parse PDFs for structured items: try agenda (URL-based) then packet (TOC-based)
        async def _parse_pdf(meeting: Dict[str, Any]) -> None:
            packet_url = meeting.get("packet_url")
            agenda_url = meeting.pop("_agenda_pdf_url", None)
            if not packet_url and not agenda_url:
                return
            items = await self._chunk_agenda_then_packet(
                agenda_url=agenda_url,
                packet_url=packet_url,
                vendor_id=meeting.get("vendor_id"),
            )
            if items:
                meeting["items"] = items

        await self._bounded_gather(
            [_parse_pdf(m) for m in results],
            max_concurrent=3,
        )

        logger.info(
            "civicweb meetings with items",
            slug=self.slug,
            count=len(results),
            with_items=sum(1 for m in results if m.get("items")),
        )

        return results

    # ------------------------------------------------------------------
    # MeetingTypeList scraping
    # ------------------------------------------------------------------

    async def _scrape_meeting_type_list(
        self, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Scrape MeetingTypeList.aspx for all bodies and meetings.

        The page lists bodies as type= links, followed by their recent
        meetings as Id= links. We group by body and filter by date range.
        """
        url = f"{self.base_url}/Portal/MeetingTypeList.aspx"

        try:
            response = await self._get(url)
            html = await response.text()
        except VendorHTTPError as e:
            logger.error(
                "failed to fetch MeetingTypeList",
                vendor="civicweb",
                slug=self.slug,
                error=str(e),
            )
            return []

        soup = BeautifulSoup(html, "html.parser")

        meetings = []
        current_body: Optional[str] = None
        current_type_id: Optional[str] = None

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            if not isinstance(href, str) or "MeetingInformation" not in href:
                continue

            text = link.get_text(strip=True)
            if not text:
                continue

            if "type=" in href:
                # Body header link
                current_body = text
                type_match = re.search(r"type=(\d+)", href)
                current_type_id = type_match.group(1) if type_match else None
                continue

            if "Id=" not in href:
                continue

            # Individual meeting link
            meeting_id_match = re.search(r"Id=(\d+)", href)
            if not meeting_id_match:
                continue

            meeting_id = meeting_id_match.group(1)

            # Parse date from title: "City Council - 18 Mar 2026"
            meeting_date = self._parse_civicweb_date(text)
            if not meeting_date:
                continue

            m_date = self._parse_date(meeting_date)
            if m_date and not (start_date <= m_date <= end_date):
                continue

            # Detect cancelled
            meeting_status = self._parse_meeting_status(text, meeting_date)

            # Clean title: strip date suffix and ***CANCELLED*** prefix
            title = re.sub(r'\*{3}CANCELLED\*{3}\s*', '', text, flags=re.IGNORECASE).strip()
            title = re.sub(r'\s*-\s*(?:\d{1,2}\s+\w{3}|\w{3}\s+\d{1,2}),?\s+\d{4}.*$', '', title).strip()
            title = re.sub(r'\s+Amended Agenda$', '', title).strip()
            if not title:
                title = text

            result: Dict[str, Any] = {
                "vendor_id": meeting_id,
                "title": title,
                "start": meeting_date,
            }

            if meeting_status:
                result["meeting_status"] = meeting_status

            meta: Dict[str, str] = {}
            if current_body:
                meta["body"] = current_body
            if current_type_id:
                meta["body_type_id"] = current_type_id

            meeting_page_url = urljoin(self.base_url, href)
            meta["meeting_url"] = meeting_page_url

            if meta:
                result["metadata"] = meta

            result["_meeting_page_url"] = meeting_page_url

            meetings.append(result)

        return meetings

    # ------------------------------------------------------------------
    # Meeting enrichment: fetch packet PDF URL
    # ------------------------------------------------------------------

    async def _enrich_meeting(self, meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch meeting page to extract the packet PDF URL."""
        page_url = meeting.pop("_meeting_page_url", "")
        if not page_url:
            return meeting

        try:
            response = await self._get(page_url)
            html = await response.text()
        except VendorHTTPError as e:
            logger.debug(
                "failed to fetch meeting page",
                vendor="civicweb",
                slug=self.slug,
                url=page_url,
                error=str(e),
            )
            return meeting

        # Extract document URL: /document/{id}/{name}.{ext}?handle={hash}
        doc_match = _DOC_URL_RE.search(html)
        if doc_match:
            original_url = urljoin(self.base_url, doc_match.group(0))
            extension = doc_match.group(1).lower()

            doc_id_match = re.search(r'/document/(\d+)/', original_url)
            doc_id = doc_id_match.group(1) if doc_id_match else None

            # Two origin flavours observed in the wild:
            # (A) Native .pdf (e.g. Evanston): the handle URL is the full packet
            #     (100-600pg compiled). Doc N+1 is a short agenda-HTML printout
            #     with hyperlinks to per-item staff reports -- URL-parseable.
            # (B) .docx origin (e.g. Bee Cave): the handle URL is an HTML
            #     viewer wrapper, unusable directly. Doc N = short agenda when
            #     rendered via ?printPdf=true, doc N+1 = rich packet with
            #     sub-document bookmarks. TOC chunking the rich packet yields
            #     real items; the short agenda has too little structure.
            if extension == "pdf":
                meeting["packet_url"] = original_url
                if doc_id:
                    agenda_doc_id = int(doc_id) + 1
                    meeting["_agenda_pdf_url"] = f"{self.base_url}/document/{agenda_doc_id}/?printPdf=true"
            elif doc_id:
                # docx origin: doc N printPdf is the short URL-chunkable agenda
                # (numbered items + hyperlinks to per-item /document/{uuid}
                # attachments), doc N+1 printPdf is the compiled packet for
                # TOC-based fallback.
                packet_doc_id = int(doc_id) + 1
                meeting["packet_url"] = f"{self.base_url}/document/{packet_doc_id}/?printPdf=true"
                meeting["_agenda_pdf_url"] = f"{self.base_url}/document/{doc_id}/?printPdf=true"

        return meeting

    # ------------------------------------------------------------------
    # PDF chunker
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _parse_civicweb_date(self, text: str) -> Optional[str]:
        """Parse CivicWeb date from meeting title to ISO.

        Handles both formats:
          '18 Mar 2026' (day-month-year, Sonoma)
          'Mar 31 2026' (month-day-year, Calistoga)
        """
        # Try day-month-year first (more common)
        match = _DATE_DMY_RE.search(text)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%d %b %Y")
                return dt.isoformat()
            except ValueError:
                pass

        # Try month-day-year
        match = _DATE_MDY_RE.search(text)
        if match:
            try:
                dt = datetime.strptime(f"{match.group(1)} {match.group(2)} {match.group(3)}", "%b %d %Y")
                return dt.isoformat()
            except ValueError:
                pass

        return None
