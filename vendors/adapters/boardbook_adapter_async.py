"""
Async BoardBook Adapter - Sparq Data Solutions BoardBook agenda system.

BoardBook is a SaaS used by school boards and special districts.  Each
organization has a numeric ID and is hosted on meetings.boardbook.org.

URL patterns:
  Listing:  /Public/Organization/{org_id}
  Agenda:   /Public/Agenda/{org_id}?meeting={meeting_id}
  File:     /Documents/DownloadPDF/{file_uuid}?org={org_id}

Attachment links in the agenda HTML use a viewer wrapper (FileViewerOrPublic)
that returns HTML, not the document. The DownloadPDF endpoint serves the
file directly as application/pdf -- BoardBook transparently converts non-PDF
sources (DOCX etc.) to PDF on the fly, so we always use DownloadPDF and
report attachments as pdf.

Listing page (#PublicMeetingsTable): one row per meeting with date+time+type
in the first cell, location spans in the second, and Agenda/Minutes links in
the third. The org's display name lives in the page <h2>.

Agenda page: a flat <tr class="agenda-item-information"> table where hierarchy
is encoded by class="agenda-item-children-of-{parent_id}" (0 = top-level) and
self-id via data-agendaitemid. Top-level items often act as section headers
(e.g. CONSENT AGENDA, CLOSED SESSION) but may carry descriptions worth
preserving. Attachments and descriptions are pre-rendered inline in this
configuration -- no AJAX fetch needed.

Slug convention: the organization's numeric ID (e.g. "2084" for Prosper ISD).

Note: BoardBook installations vary in surface (some pages are accessibility
variants, some have year selectors). This adapter handles the standard
public listing/agenda layout; revisit if a target site presents differently.
"""

import re
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector


_MEETING_ID_RE = re.compile(r'meeting=(\d+)')
_FILE_UUID_RE = re.compile(r'file=([0-9a-fA-F-]{36})')
_PARENT_CLASS_RE = re.compile(r'agenda-item-children-of-(\d+)')
_AGENDA_NUMBER_RE = re.compile(r'^(\d+(?:\.[A-Za-z0-9]+)*)\.\s+(.+)$', re.DOTALL)
_DATE_FORMATS = (
    "%B %d, %Y at %I:%M %p",
    "%B %d, %Y at %I:%M%p",
)


class AsyncBoardBookAdapter(AsyncBaseAdapter):
    """Async BoardBook adapter - school district / special district board agendas."""

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="boardbook", metrics=metrics)
        self.base_url = "https://meetings.boardbook.org"
        self.org_id = city_slug

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 14
    ) -> List[Dict[str, Any]]:
        start_date, end_date = self._date_range(days_back, days_forward)

        listing_url = f"{self.base_url}/Public/Organization/{self.org_id}"
        try:
            response = await self._get(listing_url)
            html = await response.text()
        except Exception as e:
            logger.error(
                "listing page unreachable",
                vendor="boardbook", slug=self.slug, url=listing_url, error=str(e),
            )
            return []

        soup = BeautifulSoup(html, "html.parser")
        body_name = self._extract_body_name(soup)
        meeting_refs = self._parse_listing(soup, start_date, end_date, body_name)

        if not meeting_refs:
            logger.info(
                "no meetings in date range",
                vendor="boardbook", slug=self.slug,
                start=str(start_date.date()), end=str(end_date.date()),
            )
            return []

        coros = [self._fetch_meeting_detail(ref) for ref in meeting_refs]
        results = await self._bounded_gather(coros, max_concurrent=5)

        meetings: List[Dict[str, Any]] = []
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "meeting detail failed",
                    vendor="boardbook", slug=self.slug,
                    meeting_id=meeting_refs[idx]["meeting_id"], error=str(result),
                )
            elif result:
                meetings.append(result)

        logger.info(
            "meetings fetched",
            vendor="boardbook", slug=self.slug,
            count=len(meetings),
            with_items=sum(1 for m in meetings if m.get("items")),
        )
        return meetings

    # ------------------------------------------------------------------
    # Listing parser
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_body_name(soup: BeautifulSoup) -> str:
        """Pull the org name from the listing page <h2>."""
        h2 = soup.find("h2")
        if not h2:
            return ""
        text = h2.get_text(strip=True)
        if text.endswith(" Meetings"):
            text = text[: -len(" Meetings")]
        return text

    def _parse_listing(
        self,
        soup: BeautifulSoup,
        start_date: datetime,
        end_date: datetime,
        body_name: str,
    ) -> List[Dict[str, Any]]:
        table = soup.find("table", id="PublicMeetingsTable")
        if not table:
            return []

        meetings: List[Dict[str, Any]] = []
        seen_ids: set = set()

        for row in table.find_all("tr", class_="row-for-board"):
            if not isinstance(row, Tag):
                continue
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            # Cell 0 starts with a <div> containing the date+time+type
            first_div = cells[0].find("div")
            if not first_div:
                continue
            display_text = first_div.get_text(strip=True)
            meeting_dt, meeting_type = self._parse_meeting_text(display_text)
            if not meeting_dt:
                continue
            if not (start_date <= meeting_dt <= end_date):
                continue

            agenda_link = cells[2].find(
                "a", href=lambda h: bool(h) and "/Public/Agenda/" in h
            )
            if not agenda_link:
                continue
            href = agenda_link.get("href", "")
            id_match = _MEETING_ID_RE.search(href)
            if not id_match:
                continue
            meeting_id = id_match.group(1)
            if meeting_id in seen_ids:
                continue
            seen_ids.add(meeting_id)

            location = self._parse_location_cell(cells[1])

            # Minutes viewer link lives alongside the Agenda link in cell 2
            minutes_link = cells[2].find(
                "a", href=lambda h: bool(h) and "/Public/Minutes/" in h
            )
            minutes_url = (
                urljoin(self.base_url, minutes_link.get("href", ""))
                if minutes_link else None
            )

            title = body_name or "Board Meeting"
            if meeting_type:
                title = f"{title} - {meeting_type}"

            meetings.append({
                "meeting_id": meeting_id,
                "title": title,
                "start": meeting_dt.isoformat(),
                "location": location,
                "meeting_type": meeting_type,
                "body_name": body_name,
                "agenda_url": urljoin(self.base_url, href),
                "minutes_url": minutes_url,
            })

        return meetings

    @staticmethod
    def _parse_meeting_text(text: str) -> Tuple[Optional[datetime], str]:
        """Parse 'April 20, 2026 at 4:30 PM - Regular' -> (datetime, 'Regular')."""
        if not text:
            return None, ""
        # Use rsplit so meeting types containing ' - ' are unaffected by the date side
        parts = text.rsplit(" - ", 1)
        date_str = parts[0].strip()
        meeting_type = parts[1].strip() if len(parts) > 1 else ""

        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(date_str, fmt), meeting_type
            except ValueError:
                continue
        return None, meeting_type

    @staticmethod
    def _parse_location_cell(cell: Tag) -> Optional[str]:
        """Build a location string from the location-* span trio."""
        loc_spans = cell.find_all("span", id=re.compile(r"^location"))
        parts = [s.get_text(strip=True) for s in loc_spans]
        parts = [p for p in parts if p]
        return ", ".join(parts) if parts else None

    # ------------------------------------------------------------------
    # Meeting detail
    # ------------------------------------------------------------------

    async def _fetch_meeting_detail(self, ref: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        agenda_url = ref["agenda_url"]
        try:
            response = await self._get(agenda_url)
            html = await response.text()
        except Exception as e:
            logger.warning(
                "agenda fetch failed",
                vendor="boardbook", slug=self.slug,
                meeting_id=ref["meeting_id"], error=str(e),
            )
            return None

        soup = BeautifulSoup(html, "html.parser")
        items = self._parse_agenda_items(soup)
        packet_url = self._extract_custom_agenda_pdf(soup)

        meeting: Dict[str, Any] = {
            "vendor_id": ref["meeting_id"],
            "title": ref["title"],
            "start": ref["start"],
            "agenda_url": agenda_url,
        }
        if ref.get("location"):
            meeting["location"] = ref["location"]
        if ref.get("minutes_url"):
            meeting["minutes_url"] = ref["minutes_url"]
        if items:
            meeting["items"] = items
        elif packet_url:
            # CustomAgendaForMeeting variant: /Public/Agenda/... 302s to a
            # PDF-viewer page with no structured items. Hand the packet URL
            # to the monolithic-summarization path.
            meeting["packet_url"] = packet_url

        metadata: Dict[str, Any] = {}
        if ref.get("body_name"):
            metadata["body"] = ref["body_name"]
        if ref.get("meeting_type"):
            metadata["meeting_type"] = ref["meeting_type"]
        if metadata:
            meeting["metadata"] = metadata

        status = self._parse_meeting_status(ref["title"])
        if status:
            meeting["meeting_status"] = status

        return meeting

    def _extract_custom_agenda_pdf(self, soup: BeautifulSoup) -> Optional[str]:
        """Detect the CustomAgendaForMeeting variant and return its PDF URL.

        These pages have no agenda-item table; the entire agenda is a single
        PDF rendered through SparqWebViewer, with the document GUID in a
        hidden input. Convert to the direct DownloadPDF endpoint.
        """
        doc_input = soup.find("input", id="NewDocumentViewerDocumentID")
        if not doc_input:
            return None
        doc_id = doc_input.get("value", "")
        if not doc_id:
            return None
        return f"{self.base_url}/Documents/DownloadPDF/{doc_id}?org={self.org_id}"

    # ------------------------------------------------------------------
    # Agenda item parser
    # ------------------------------------------------------------------

    def _parse_agenda_items(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """Flatten BoardBook's hierarchical agenda rows into engagic items.

        Each row carries data-agendaitemid plus a class
        'agenda-item-children-of-{parent_id}' (0 for top-level).  We walk the
        rows in document order, keeping the parent map so each child item can
        record its enclosing top-level section in metadata.
        """
        rows = soup.find_all("tr", class_="agenda-item-information")
        if not rows:
            return []

        nodes: Dict[str, Dict[str, Any]] = {}
        parents: Dict[str, str] = {}
        order: List[str] = []

        for row in rows:
            if not isinstance(row, Tag):
                continue
            item_id = row.get("data-agendaitemid", "")
            if not item_id:
                continue

            parent_id = "0"
            for cls in row.get("class", []):
                m = _PARENT_CLASS_RE.match(cls)
                if m:
                    parent_id = m.group(1)
                    break

            form_check = row.find("div", class_="form-check")
            if not form_check:
                continue

            title, inline_body = self._extract_form_check_content(form_check)
            agenda_number, title = self._split_agenda_number(title)

            # Description (sibling of form-check, separate from any inline body)
            description = self._extract_description(form_check)
            body_text = "\n\n".join(p for p in (description, inline_body) if p)

            attachments = self._parse_item_attachments(row)

            nodes[item_id] = {
                "agenda_number": agenda_number,
                "title": title,
                "body_text": body_text,
                "attachments": attachments,
            }
            parents[item_id] = parent_id
            order.append(item_id)

        items: List[Dict[str, Any]] = []
        for sequence, item_id in enumerate(order, start=1):
            node = nodes[item_id]

            # Build top-down breadcrumb of ancestor titles. BoardBook agendas
            # nest several levels (e.g. Closed Session > Personnel > New Hires
            # > Michelle Manna); only the deepest leaf carries meaningful
            # context, and the intermediate levels are where the topic lives.
            crumb: List[str] = []
            cursor = parents.get(item_id, "0")
            while cursor != "0" and cursor in nodes:
                crumb.append(nodes[cursor]["title"])
                cursor = parents.get(cursor, "0")
            crumb.reverse()

            item: Dict[str, Any] = {
                "vendor_item_id": item_id,
                "title": node["title"] or f"Item {sequence}",
                "sequence": sequence,
                "agenda_number": node["agenda_number"] or str(sequence),
                "attachments": node["attachments"],
            }
            if node["body_text"]:
                item["body_text"] = node["body_text"]
            if crumb:
                item["metadata"] = {
                    "section": crumb[0],
                    "section_path": " > ".join(crumb),
                }
            items.append(item)

        return items

    @staticmethod
    def _extract_form_check_content(form_check: Tag) -> Tuple[str, str]:
        """Return (title_text, body_text) for a form-check div.

        Most rows have a simple '<number>. <title>' shape, but some children
        (e.g. 'Important Dates') embed a <ul>/<ol> directly inside form-check.
        Those block elements get peeled off as body_text.
        """
        # Drop drag-handle icons that screen-show but add nothing semantic
        for icon in form_check.find_all("i", class_="fa"):
            icon.decompose()

        body_blocks: List[str] = []
        for tag in list(form_check.find_all(["ul", "ol"], recursive=False)):
            body_text = tag.get_text(separator="\n", strip=True)
            if body_text:
                body_blocks.append(body_text)
            tag.decompose()

        title = form_check.get_text(separator=" ", strip=True)
        title = re.sub(r"\s+", " ", title).rstrip(":").strip()
        body = "\n".join(body_blocks).strip()
        return title, body

    @staticmethod
    def _extract_description(form_check: Tag) -> str:
        """Pull text out of a sibling <div class='Description'>, if any."""
        parent_div = form_check.parent
        if not parent_div:
            return ""
        desc_div = parent_div.find("div", class_="Description")
        if not desc_div:
            return ""
        # Strip the leading 'Description:' label
        label = desc_div.find("strong")
        if label and "Description" in label.get_text():
            label.decompose()
        text = desc_div.get_text(separator=" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _split_agenda_number(text: str) -> Tuple[str, str]:
        """Split '1. CALL TO ORDER' or '3.A. Private Consultation' -> (number, title)."""
        m = _AGENDA_NUMBER_RE.match(text)
        if m:
            return m.group(1), m.group(2).strip()
        return "", text

    def _parse_item_attachments(self, row: Tag) -> List[Dict[str, Any]]:
        att_div = row.find("div", class_="Attachments")
        if not att_div:
            return []

        attachments: List[Dict[str, Any]] = []
        seen_uuids: set = set()
        for link in att_div.find_all("a", href=True):
            href = str(link.get("href", "")).strip()
            if not href or href == "#":
                continue

            # Prefer data-documentid; fall back to file= query param in the href.
            file_uuid = link.get("data-documentid") or ""
            if not file_uuid:
                m = _FILE_UUID_RE.search(href)
                if m:
                    file_uuid = m.group(1)
            if not file_uuid:
                continue
            if file_uuid in seen_uuids:
                continue
            seen_uuids.add(file_uuid)

            name_span = link.find("span", class_="fileNameValue")
            name = name_span.get_text(strip=True) if name_span else link.get_text(strip=True)
            if not name:
                name = "Attachment"

            url = f"{self.base_url}/Documents/DownloadPDF/{file_uuid}?org={self.org_id}"
            attachments.append({
                "name": name,
                "url": url,
                "type": "pdf",
            })
        return attachments
