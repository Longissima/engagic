"""
Async Escribe Adapter - Item-level extraction for Escribe meeting management systems

Escribe (eScribe) is used by cities for agenda/meeting management.
Example: Raleigh NC uses pub-raleighnc.escribemeetings.com

Item-level extraction via Agenda=Merged view:
- Structured agenda items with unique IDs
- Per-item attachments via FileStream.ashx
- Matter file extraction from title prefixes (BOA-0039-2025, etc.)
- Nested section hierarchy

Confidence: 8/10 - Tested against Raleigh NC, may need adjustments for other cities
"""

import re
from typing import Dict, Any, Optional, List
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.html_attrs import string_attr, string_list_attr
from pipeline.protocols import MetricsCollector


# Matter file patterns found in Escribe title prefixes
# Format: PREFIX-NNNN-YYYY or PREFIX-YYYY-NNNN
MATTER_FILE_PATTERNS = [
    # Board of Adjustment: BOA-0039-2025
    r'\b(BOA-\d{4}-\d{4})\b',
    # Planning/Development: PLANDEV-BOA-0039-2025-2025-539
    r'\b(PLANDEV-[A-Z]+-\d{4}-\d{4}-\d{4}-\d+)\b',
    # Generic case numbers: ABC-2025-1234, ABC-1234-2025
    r'\b([A-Z]{2,10}-\d{4}-\d{4,6})\b',
    r'\b([A-Z]{2,10}-\d{4,6}-\d{4})\b',
    # Resolution/Ordinance: RES-2025-123, ORD-2025-456
    r'\b(RES-\d{4}-\d+)\b',
    r'\b(ORD-\d{4}-\d+)\b',
    # File numbers with prefix: File #2025-123
    r'\bFile\s*#?\s*(\d{4}-\d+)\b',
]

# Derive matter_type from matter_file prefix
# Prefixes are consistent within each Escribe instance
MATTER_TYPE_FROM_PREFIX = {
    "BOA": "Board of Adjustment",
    "COA": "Certificate of Appropriateness",
    "RES": "Resolution",
    "ORD": "Ordinance",
    "PLANDEV": "Planning & Development",
    "TC": "Text Change",
    "Z": "Zoning",
    "SP": "Site Plan",
    "SUP": "Special Use Permit",
    "AN": "Annexation",
    "CUP": "Conditional Use Permit",
    "VAR": "Variance",
}

# Tags that imply a line break when eScribe rich text is flattened.
_BLOCK_TAGS = (
    "p", "div", "br", "li", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
)

# Chrome that must never land in body text: labels, icon rows, attachment lists
# (whose filenames would otherwise read as content). AgendaItemAttachementsHeader
# is the vendor's own spelling.
_BODY_NOISE_CLASSES = (
    "AgendaItemHeader", "AgendaItemCategory", "AgendaItemSponsors",
    "AgendaItemIcons", "AgendaItemAttachmentsList", "AgendaItemAttachment",
    "AgendaItemPublicCommentHeader", "AgendaItemAttachementsHeader",
    "AgendaItemTitleRow", "ClosedAgendaItemTitleRow", "MotionLabel", "Number",
)

# Routing badges some instances emit as the entire description (Orlando does this
# on 84 of 98 items). Whole-string match only -- the same badge legitimately
# prefixes several KB of real text, and startswith would delete those items.
_PLACEHOLDER_BODY = re.compile(
    r"^(?:no agenda items|public comments:?|district\s*:[\s\d,]*(?:all)?|none|n/?a"
    r"|attachments\s*\|\s*public comments)\.?$",
    re.IGNORECASE,
)


class AsyncEscribeAdapter(AsyncBaseAdapter):
    """Async adapter for cities using Escribe meeting management system.

    Item-level extraction from Agenda=Merged view with matter tracking.
    """

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        """city_slug is the Escribe subdomain (e.g., "pub-raleighnc")"""
        super().__init__(city_slug, vendor="escribe", metrics=metrics)
        self.base_url = f"https://{self.slug}.escribemeetings.com"

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings via calendar API with item-level extraction."""
        start_date, end_date = self._date_range(days_back, days_forward)

        # Use calendar API to get ALL meetings (upcoming + past, all types)
        calendar_url = f"{self.base_url}/MeetingsCalendarView.aspx/GetCalendarMeetings"

        logger.info("fetching meetings via calendar API", vendor="escribe", slug=self.slug)

        payload = {
            "calendarStartDate": start_date.strftime("%Y-%m-%d"),
            "calendarEndDate": end_date.strftime("%Y-%m-%d"),
        }

        response = await self._post(
            calendar_url,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        data = await response.json()

        # Response is in {"d": [...]} format
        meetings_data = data.get("d", [])
        if not meetings_data:
            logger.warning("no meetings from calendar API", vendor="escribe", slug=self.slug)
            return []

        logger.info(
            "found meetings in calendar API",
            vendor="escribe",
            slug=self.slug,
            count=len(meetings_data)
        )

        results = []
        for meeting_json in meetings_data:
            meeting_basic = self._parse_calendar_meeting(meeting_json)
            if not meeting_basic:
                continue

            if self._minutes_discovery_only:
                meeting_basic.pop("_uuid", None)
                meeting_basic.pop("has_agenda", None)
                if meeting_basic.get("minutes_url"):
                    results.append(meeting_basic)
                continue

            meeting_uuid = meeting_basic.get("_uuid")
            if meeting_uuid and meeting_basic.get("has_agenda"):
                meeting_data = await self._fetch_meeting_details(meeting_uuid, meeting_basic)
                if meeting_data:
                    # If HTML parsing yielded no items, try chunker on packet PDF
                    if not meeting_data.get("items") and meeting_data.get("packet_url"):
                        pdf_items = await self._parse_packet_pdf(
                            meeting_data["packet_url"], meeting_data.get("vendor_id")
                        )
                        if pdf_items:
                            meeting_data["items"] = pdf_items
                    results.append(meeting_data)
            else:
                # No HTML agenda — try chunker on packet PDF
                packet_url = meeting_basic.get("packet_url")
                if packet_url:
                    pdf_items = await self._parse_packet_pdf(
                        packet_url, meeting_basic.get("vendor_id")
                    )
                    if pdf_items:
                        meeting_basic["items"] = pdf_items
                meeting_basic.pop("_uuid", None)
                meeting_basic.pop("has_agenda", None)
                results.append(meeting_basic)

        logger.info(
            "collected meetings with items",
            vendor="escribe",
            slug=self.slug,
            count=len(results),
            with_items=sum(1 for m in results if m.get("items")),
        )

        return results

    def _parse_calendar_meeting(self, meeting_json: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse meeting from calendar API JSON response."""
        meeting_id = meeting_json.get("ID")
        if not meeting_id:
            return None

        title = meeting_json.get("MeetingName", "")
        start_date = meeting_json.get("StartDate")

        # Parse date using base adapter's parser
        parsed_date = None
        if start_date:
            # Handle "/Date(timestamp)/" format (millisecond timestamp)
            if "/Date(" in start_date:
                match = re.search(r"/Date\((\d+)\)/", start_date)
                if match:
                    timestamp_ms = int(match.group(1))
                    parsed_date = datetime.fromtimestamp(timestamp_ms / 1000)
            else:
                # Normalize YYYY/MM/DD to YYYY-MM-DD for base parser
                normalized = start_date.replace("/", "-")
                parsed_date = self._parse_date(normalized)

        # Skip meetings without valid dates
        if not parsed_date:
            logger.warning(
                "skipping meeting without valid date",
                vendor="escribe",
                slug=self.slug,
                title=title,
                start_date=start_date
            )
            return None

        # Extract UUID from URL if available
        meeting_uuid = None
        url = meeting_json.get("Url", "")
        if url:
            uuid_match = re.search(r"Id=([a-f0-9-]+)", url, re.IGNORECASE)
            if uuid_match:
                meeting_uuid = uuid_match.group(1)

        vendor_id = f"escribe_{meeting_uuid}" if meeting_uuid else self._generate_fallback_vendor_id(title, parsed_date)

        # Extract packet_url from MeetingDocumentLink array
        # Prefer Merged (revised agenda), fallback to Agenda
        packet_url = None
        doc_links = meeting_json.get("MeetingDocumentLink", [])
        if isinstance(doc_links, list):
            for doc in doc_links:
                if isinstance(doc, dict) and doc.get("Format") == ".pdf":
                    if doc.get("Type") == "Merged":
                        packet_url = doc.get("Url")
                        break
                    elif doc.get("Type") == "Agenda" and not packet_url:
                        packet_url = doc.get("Url")
            if packet_url and not packet_url.startswith("http"):
                packet_url = urljoin(self.base_url, packet_url)

        # Minutes ride the same payload; Type is "PostMinutes" on some sites,
        # "Minutes" on others. Never eligible for packet/agenda selection above.
        minutes_url = None
        if isinstance(doc_links, list):
            for doc in doc_links:
                if not isinstance(doc, dict) or doc.get("Type") not in ("Minutes", "PostMinutes"):
                    continue
                if not doc.get("Url"):
                    continue
                if doc.get("Format") == ".pdf":
                    minutes_url = doc["Url"]
                    break
                if not minutes_url:
                    minutes_url = doc["Url"]
            if minutes_url and not minutes_url.startswith("http"):
                minutes_url = urljoin(self.base_url, minutes_url)

        result = {
            "vendor_id": vendor_id,
            "title": title,
            "start": parsed_date.isoformat() if parsed_date else "",
            "packet_url": packet_url,
            "minutes_url": minutes_url,
            "_uuid": meeting_uuid,
            "has_agenda": meeting_json.get("HasAgenda", False),
        }

        return result

    async def _fetch_meeting_details(
        self, meeting_uuid: str, basic_meeting: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Fetch Agenda=Merged page and extract item-level details."""
        merged_url = f"{self.base_url}/Meeting.aspx?Id={meeting_uuid}&Agenda=Merged&lang=English"

        logger.debug(
            "fetching meeting details",
            vendor="escribe",
            slug=self.slug,
            meeting_uuid=meeting_uuid
        )

        response = await self._get(merged_url)
        html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')

        items = await self._parse_agenda_items(soup, meeting_uuid, merged_url)

        meeting_data = {
            "vendor_id": basic_meeting["vendor_id"],
            "title": basic_meeting["title"],
            "start": basic_meeting["start"],
            "agenda_url": merged_url,
            "packet_url": basic_meeting.get("packet_url"),
            "minutes_url": basic_meeting.get("minutes_url"),
            "items": items,
        }

        if basic_meeting.get("meeting_status"):
            meeting_data["meeting_status"] = basic_meeting["meeting_status"]

        logger.info(
            "extracted items from meeting",
            vendor="escribe",
            slug=self.slug,
            meeting_uuid=meeting_uuid,
            item_count=len(items)
        )

        return meeting_data

    async def _parse_agenda_items(
        self, soup: BeautifulSoup, meeting_uuid: str, base_url: str
    ) -> List[Dict[str, Any]]:
        """Parse agenda items from Escribe Merged agenda view."""
        items = []
        item_containers = soup.find_all("div", class_="AgendaItemContainer")

        current_section = None
        item_counter = 0

        for container in item_containers:
            section_header = self._extract_section_header(container)
            if section_header:
                current_section = section_header

            item_id = self._extract_item_id(container)
            if not item_id:
                continue

            item_counter += 1

            counter_elem = container.find(
                "div", class_=["AgendaItemCounter", "ClosedAgendaItemCounter"]
            )
            item_number = counter_elem.get_text(strip=True) if counter_elem else str(item_counter)

            # Skip section headers - organizational containers, not substantive items.
            # They aggregate all sub-item attachments via nesting, which poisons the
            # processor's shared-URL detection (all child URLs become "shared",
            # stripping PDF text from sub-items). Detect by: (1) A-G letter headers,
            # (2) any container with nested AgendaItemContainer children (confidence: 9/10)
            if item_number and re.match(r'^[A-G]\.$', item_number):
                continue

            has_child_items = container.find("div", class_="AgendaItemContainer") is not None
            if has_child_items:
                continue

            title = self._extract_item_title(container)
            if not title:
                continue

            matter_file = self._extract_matter_file(title)
            attachments = self._extract_item_attachments(container, base_url)
            body_text = self._extract_item_body_text(container)

            item_data = {
                "vendor_item_id": item_id,  # Raw vendor ID, orchestrator generates final item_id
                "title": title,
                "sequence": item_counter,
                "agenda_number": item_number,
                "section": current_section,
                "body_text": body_text,
                "attachments": attachments,
            }

            # Only the instance's own title-prefix filing scheme is vendor
            # knowledge. Identifiers cited in the agenda text are common to every
            # vendor and are derived once in the sync item funnel.
            if matter_file:
                item_data["matter_file"] = matter_file
                # Derive matter_type from prefix (BOA -> Board of Adjustment, etc.)
                prefix = matter_file.split("-")[0].upper()
                if prefix in MATTER_TYPE_FROM_PREFIX:
                    item_data["matter_type"] = MATTER_TYPE_FROM_PREFIX[prefix]

            items.append(item_data)

        return items

    def _extract_item_id(self, container: Tag) -> Optional[str]:
        """Extract item ID from AgendaItem class or SelectItem link."""
        agenda_item_div = container.find("div", class_=re.compile(r"AgendaItem\d+"))
        if not agenda_item_div:
            for cls in string_list_attr(container, "class"):
                if re.match(r"AgendaItem\d+", cls):
                    agenda_item_div = container
                    break

        if agenda_item_div:
            for cls in string_list_attr(agenda_item_div, "class"):
                match = re.match(r"AgendaItem(\d+)", cls)
                if match:
                    return match.group(1)

        select_link = container.find("a", href=re.compile(r"SelectItem\(\d+\)"))
        if select_link:
            match = re.search(r"SelectItem\((\d+)\)", string_attr(select_link, "href"))
            if match:
                return match.group(1)

        # Closed-session items carry no AgendaItemNNN class and no SelectItem
        # link, so they were dropped entirely -- along with real content
        # (liability claimants, labor negotiation parties). Their id survives on
        # the public-comment list wrapper.
        closed_list = container.find(
            "div", class_=re.compile(r"AgendaItemPublicCommentListIndent\d+Closed")
        )
        if closed_list:
            for cls in string_list_attr(closed_list, "class"):
                match = re.match(r"AgendaItemPublicCommentListIndent(\d+)Closed", cls)
                if match:
                    return match.group(1)

        return None

    def _extract_item_title(self, container: Tag) -> Optional[str]:
        """Extract item title from AgendaItemTitle div or SelectItem link."""
        for class_name in ("AgendaItemTitle", "ClosedAgendaItemTitle"):
            title_container = container.find("div", class_=class_name)
            if title_container:
                title_link = title_container.find("a")
                title = (
                    title_link.get_text(strip=True)
                    if title_link
                    else title_container.get_text(strip=True)
                )
                if title:
                    return title

        select_link = container.find("a", href=re.compile(r"SelectItem"))
        if select_link:
            return select_link.get_text(strip=True) or None

        return None

    def _extract_item_body_text(self, container: Tag) -> str:
        """Extract the item's own agenda text (the substance eScribe publishes inline).

        Layout varies by instance and the variants are not cosmetic:

        - Two AgendaItemContentRow divs, the first a department banner
          (div.AgendaItemHeader) and the second the real body. Taking the first
          row -- what this adapter did before -- returned the banner and dropped
          the body on a third of Detroit's items.
        - No AgendaItemDescription at all: Richmond publishes 45 of 48 items as
          div.MotionText inside ul.AgendaItemMotions.
        - Routing badges ("District: ALL") emitted as the whole description.

        The department banner is kept as a prefix when a body exists: with titles
        like "Whitfield-Calloway, reso. autho." the owning department is real
        context, not decoration. Confidence: 8/10.
        """
        parts = [self._block_text(node) for node in self._owned(container, "AgendaItemDescription")]

        if not any(parts):
            # Fallback: content rows with chrome stripped out. Scoped to this
            # container so a parent can never absorb a child item's text.
            parts = []
            for row in self._owned(container, "AgendaItemContentRow"):
                fragment = BeautifulSoup(str(row), "html.parser")
                for class_name in _BODY_NOISE_CLASSES:
                    for junk in fragment.find_all(class_=class_name):
                        junk.decompose()
                for nested in fragment.find_all("div", class_="AgendaItemContainer"):
                    nested.decompose()
                parts.append(self._clean_text(fragment.get_text("\n")))

        body = self._clean_text("\n".join(part for part in parts if part))
        if _PLACEHOLDER_BODY.match(body.replace("\n", " ")):
            body = ""

        # Staff recommendation. Deduped because the fallback path above may
        # already have absorbed it.
        motions = [self._block_text(node) for node in self._owned(container, "MotionText")]
        for motion in motions:
            if motion and motion not in body:
                body = f"{body}\n{motion}".strip()

        if not body:
            return ""

        headers = self._owned(container, "AgendaItemHeader")
        header = self._block_text(headers[0]) if headers else ""
        if header and header.lower() not in body.lower():
            return f"{header}\n{body}"
        return body

    @staticmethod
    def _owned(container: Tag, class_name: str) -> List[Tag]:
        """Nodes belonging to this container, not to a nested child item."""
        return [
            node
            for node in container.find_all(class_=class_name)
            if node.find_parent("div", class_="AgendaItemContainer") is container
        ]

    @classmethod
    def _block_text(cls, node: Tag) -> str:
        """Flatten rich text with block boundaries preserved as newlines.

        Measured over 212 real descriptions: get_text(strip=True) fuses 334 word
        boundaries ("Report:ROI No. 2263"), while get_text(" ", strip=True) fixes
        those but shreds Word-pasted per-glyph spans into "C o mmi ss i o n e r s".
        Breaking on block tags only leaves one real fusion in the corpus, and that
        one is fused in the source text node. Works on a detached copy so the
        caller's tree is untouched. Confidence: 9/10.
        """
        fragment = BeautifulSoup(str(node), "html.parser")
        for tag in fragment.find_all(_BLOCK_TAGS):
            tag.insert_after("\n")
        return cls._clean_text(fragment.get_text())

    @staticmethod
    def _clean_text(value: str) -> str:
        """Scrub eScribe whitespace, keeping single newlines as block boundaries."""
        text = (value or "").replace("\xa0", " ").replace("\u200b", "").replace("\u2009", " ")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\s*\n\s*", "\n", text)
        return re.sub(r"\n{2,}", "\n", text).strip()

    def _extract_section_header(self, container: Tag) -> Optional[str]:
        """Extract section header from container if present."""
        title_row = container.find("div", class_="AgendaItemTitleRow")
        if not title_row:
            return None
        strong = title_row.find("strong")
        if not strong:
            return None
        text = strong.get_text(strip=True)
        # Section headers are short and don't start with item numbers
        if text and len(text) < 100 and not re.match(r"^\d+\.", text):
            return text
        return None

    def _extract_matter_file(self, title: str) -> Optional[str]:
        """Extract matter file number from title prefix.

        Examples:
        - "BOA-0039-2025: 6809 Sandy Forks Road" -> "BOA-0039-2025"
        - "RES-2025-123: Approving budget" -> "RES-2025-123"
        """
        if not title:
            return None

        for pattern in MATTER_FILE_PATTERNS:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                return match.group(1).upper()

        # Fallback: look for prefix before colon
        if ":" in title:
            prefix = title.split(":", 1)[0].strip()
            # Must look like a case/file number (has digits and dashes/letters)
            if re.match(r"^[A-Z0-9]+-[A-Z0-9-]+$", prefix, re.IGNORECASE):
                return prefix.upper()

        return None

    def _extract_item_attachments(self, container: Tag, base_url: str) -> List[Dict[str, Any]]:
        """Extract attachments for a specific agenda item.

        Only includes attachments directly in this container, not in nested
        child AgendaItemContainer divs (which are separate sub-items).
        """
        attachments = []

        # Collect child container elements to exclude their attachment links
        child_containers = set(container.find_all("div", class_="AgendaItemContainer"))

        for link in container.find_all("a", href=re.compile(r"FileStream\.ashx\?DocumentId=", re.IGNORECASE)):
            href = string_attr(link, "href")
            if not href:
                continue

            # Skip links inside nested child items
            if child_containers and any(link in c.descendants for c in child_containers):
                continue

            attachment_url = urljoin(base_url, href) if not href.startswith("http") else href

            name = (
                link.get_text(strip=True)
                or string_attr(link, "aria-label")
                or string_attr(link, "title")
            )
            if not name:
                doc_id_match = re.search(r"DocumentId=(\d+)", href)
                name = f"Document_{doc_id_match.group(1)}" if doc_id_match else "Attachment"

            file_type = self._detect_file_type(name, href)

            attachments.append({"name": name, "url": attachment_url, "type": file_type})

        return attachments

    def _detect_file_type(self, name: str, href: str) -> str:
        """Detect file type from name or URL. Defaults to pdf."""
        from vendors.utils.attachments import classify_attachment_type
        result = classify_attachment_type(href, name)
        return result if result != 'unknown' else 'pdf'
