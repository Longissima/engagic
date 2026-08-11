"""
Async Legistar Adapter - API integration for Legistar platform

API-first with HTML fallback. Cities: Seattle WA, NYC, Cambridge MA, and many others
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from urllib.parse import urljoin, urlparse
import json
from json import JSONDecodeError
import re
import asyncio
import xml.etree.ElementTree as ET
from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.html_attrs import string_attr
from vendors.adapters.parsers.legistar_parser import parse_html_agenda, parse_legislation_attachments, parse_aada_html
from pipeline.filters import should_skip_meeting, should_skip_processing
from pipeline.utils import combine_date_time
from pipeline.protocols import MetricsCollector
from exceptions import VendorHTTPError, VendorParsingError
import aiohttp
import os

# Minimum item count from MeetingDetail before we treat the page as a stub
# and retry via AADA when an AADA link is present in the calendar row.
# LA County MeetingDetail stubs typically return <=2 procedural rows.
AADA_RETRY_MIN_ITEMS = 3
PREFER_AADA_CONFIG = "data/legistar_prefer_aada.json"


def _load_prefer_aada_slugs() -> set:
    """Load slugs where AADA agenda is preferred over MeetingDetail.

    LA County's MeetingDetail page renders but returns only stub rows
    ("Public Comment", "Adjournment") despite having a full AADA agenda.
    """
    if not os.path.exists(PREFER_AADA_CONFIG):
        return set()
    try:
        with open(PREFER_AADA_CONFIG, "r") as f:
            data = json.load(f)
        return set(data.get("slugs", []))
    except (JSONDecodeError, OSError):
        return set()


_PREFER_AADA_SLUGS = _load_prefer_aada_slugs()


# Legistar's MatterStatusName is free text each city configures itself, so the
# same word can mean opposite things: Milwaukee's "Placed On File" is how an
# ordinance dies, while Oakland's "Filed" marks one completed. Only terms whose
# meaning is unambiguous across cities are mapped; everything else returns None
# and leaves the stored status alone. Guessing here would assert a matter is
# dead when it is merely waiting, which is worse than saying nothing.
# Confidence: 8/10 -- derived from live status surveys of milwaukee, denver and
# oakland. Extend from the "unrecognized vendor matter status" debug log.
_MATTER_STATUS_MAP = {
    "passed": "passed",
    "adopted": "passed",
    "approved": "passed",
    "confirmed": "passed",
    "granted": "passed",
    "enacted": "enacted",
    "signed": "enacted",
    "vetoed": "vetoed",
    "failed": "failed",
    "defeated": "failed",
    "denied": "failed",
    "dead": "failed",
    "disallowed": "failed",
    "placed on file": "failed",
    "in council-placed on file": "failed",
    "tabled": "tabled",
    "held": "tabled",
    "withdrawn": "withdrawn",
    "referred": "referred",
    "in committee": "referred",
    "in commission": "referred",
    "in rules committee": "referred",
    "heard in committee": "referred",
    "amended": "amended",
}

# Deliberately unmapped, do not add without checking the city means what you
# think: "Filed" (completed in Oakland, killed elsewhere), "Settled",
# "Presentation", "Agenda Ready", "To be Scheduled", "Introduced".


def map_matter_status(raw: Optional[str]) -> Optional[str]:
    """Normalize a vendor status string into engagic's status vocabulary.

    Returns None for anything unrecognized so the caller writes nothing.
    """
    if not raw:
        return None
    return _MATTER_STATUS_MAP.get(" ".join(raw.split()).lower())


class AsyncLegistarAdapter(AsyncBaseAdapter):
    """Async adapter for cities using Legistar platform."""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(
        self,
        city_slug: str,
        api_token: Optional[str] = None,
        metrics: Optional[MetricsCollector] = None
    ):
        super().__init__(city_slug, vendor="legistar", metrics=metrics)
        self.api_token = api_token
        self.base_url = f"https://webapi.legistar.com/v1/{self.slug}"
        self.prefer_aada = self.slug in _PREFER_AADA_SLUGS

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings via API, falling back to HTML if needed."""
        meetings = []
        try:
            logger.info("legistar using API", slug=self.slug)
            meetings = await self._fetch_meetings_api(days_back, days_forward)
        except (VendorHTTPError, aiohttp.ClientError) as e:
            # Fall back to HTML for client errors (API disabled) and server errors (API broken)
            if isinstance(e, VendorHTTPError) and e.status_code in [400, 403, 404, 500, 502, 503]:
                logger.warning(
                    "legistar API failed, falling back to HTML",
                    slug=self.slug,
                    status=e.status_code
                )
                meetings = await self._fetch_meetings_html(days_back, days_forward)
            else:
                raise
            return meetings

        # If API succeeded but returned 0 events, fall back to HTML
        if len(meetings) == 0:
            logger.warning(
                "legistar API returned 0 events, falling back to HTML",
                slug=self.slug
            )
            meetings = await self._fetch_meetings_html(days_back, days_forward)
        elif self._api_items_are_garbage(meetings):
            logger.warning(
                "legistar API returned garbage items, falling back to HTML",
                slug=self.slug,
                api_meeting_count=len(meetings),
            )
            meetings = await self._fetch_meetings_html(days_back, days_forward)
        else:
            logger.info("legistar API success", slug=self.slug, count=len(meetings))

        return meetings

    def _api_items_are_garbage(self, meetings: List[Dict[str, Any]]) -> bool:
        """Detect misconfigured Legistar APIs that return text fragments as items.

        Signals: items with no agenda numbers, no matter IDs, and junk titles
        like "page break".  When the majority of items across all meetings
        lack both agenda_number and matter_id, the API is not providing
        structured data and we should fall back to HTML scraping.
        """
        total_items = 0
        useless_items = 0
        has_page_break = False
        boilerplate_items = 0

        # Titles that are clearly page chrome, not agenda items
        _BOILERPLATE = {
            "page break", "agenda", "meetings", "attendance and participation by the public",
        }

        for meeting in meetings:
            for item in meeting.get("items", []):
                total_items += 1
                has_number = bool(item.get("agenda_number"))
                has_matter = bool(item.get("matter_id") or item.get("matter_file"))
                if not has_number and not has_matter:
                    useless_items += 1
                title = (item.get("title") or "").strip()
                title_lower = title.lower()
                if title_lower == "page break":
                    has_page_break = True
                # Section dividers, boilerplate headings, and empty titles
                if (title_lower in _BOILERPLATE
                        or title.startswith("___")
                        or not title
                        or (len(title) < 3 and not has_number)):
                    boilerplate_items += 1

        if total_items == 0:
            return False

        useless_ratio = useless_items / total_items
        boilerplate_ratio = boilerplate_items / total_items
        # If >50% of items have neither agenda number nor matter ID
        # AND we see literal "page break" items, the API is garbage.
        # Page breaks alone don't indicate garbage -- some cities (Riverside)
        # use them as structural separators alongside real items.
        if has_page_break and (useless_ratio > 0.5 or boilerplate_ratio > 0.15):
            logger.debug(
                "garbage detection triggered",
                slug=self.slug,
                total_items=total_items,
                useless_items=useless_items,
                useless_ratio=round(useless_ratio, 2),
                boilerplate_items=boilerplate_items,
                boilerplate_ratio=round(boilerplate_ratio, 2),
                has_page_break=has_page_break,
            )
            return True
        return False

    async def _fetch_meetings_api(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings from Legistar Web API."""
        # Build date range
        start_date_dt, end_date_dt = self._date_range(days_back, days_forward)

        # Format dates for OData filter
        start_date = start_date_dt.strftime("%Y-%m-%d")
        end_date = end_date_dt.strftime("%Y-%m-%d")

        # Build OData filter
        filter_str = (
            f"EventDate ge datetime'{start_date}' and EventDate lt datetime'{end_date}'"
        )

        # API parameters
        params = {
            "$filter": filter_str,
            "$orderby": "EventDate desc",
            "$top": 1000,  # API max
        }

        # Add API token if provided
        if self.api_token:
            params["token"] = self.api_token

        # Fetch events from API
        url = f"{self.base_url}/Events"
        response = await self._get(url, params=params)

        # Parse response (JSON or XML)
        content_type = response.headers.get('content-type', '').lower()

        if 'json' in content_type:
            events = await response.json()
        elif 'xml' in content_type:
            # Parse XML response
            text = await response.text()
            events = self._parse_xml_events(text)
        else:
            # Unknown content type - read text first to avoid stream consumption issues
            text = await response.text()
            try:
                events = json.loads(text)
            except (JSONDecodeError, ValueError):
                events = self._parse_xml_events(text)

        if not isinstance(events, list):
            raise VendorParsingError(
                f"Expected list from Legistar API at {url}, got {type(events).__name__}",
                vendor=self.vendor,
                city_slug=self.slug
            )

        # Some APIs (Nashville) ignore server filters - filter client-side
        filtered_events = []
        for event in events:
            event_date_str = event.get("EventDate")
            if event_date_str:
                try:
                    event_date = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                    if start_date_dt <= event_date <= end_date_dt:
                        filtered_events.append(event)
                except (ValueError, TypeError):
                    filtered_events.append(event)
            else:
                filtered_events.append(event)

        logger.info(
            "legistar client-side filtered events",
            slug=self.slug,
            total=len(events),
            filtered=len(filtered_events)
        )

        meetings = []
        for event in filtered_events:
            meeting = await self._process_api_event(event)
            if meeting:
                meetings.append(meeting)

        return meetings

    def _parse_xml_events(self, xml_text: str) -> List[Dict]:
        """Parse XML response from Legistar API"""
        root = ET.fromstring(xml_text)

        # Find all entry elements (Atom feed format)
        ns = {'atom': 'http://www.w3.org/2005/Atom', 'd': 'http://schemas.microsoft.com/ado/2007/08/dataservices', 'm': 'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata'}

        events = []
        for entry in root.findall('.//atom:entry', ns):
            content = entry.find('.//m:properties', ns)
            if content is not None:
                event = {}
                for prop in content:
                    # Remove namespace from tag
                    tag = prop.tag.split('}')[1] if '}' in prop.tag else prop.tag
                    event[tag] = prop.text
                events.append(event)

        return events

    async def _process_api_event(self, event: Dict) -> Optional[Dict[str, Any]]:
        """Process a single API event into meeting dictionary"""
        try:
            event_id = event.get("EventId")
            event_guid = event.get("EventGuid")
            event_body_id = event.get("EventBodyId")  # Legistar body/committee ID
            event_name = event.get("EventBodyName", "Unknown Body")
            event_location = event.get("EventLocation")
            event_agenda_status = event.get("EventAgendaStatusName", "")

            if not event_id:
                return None

            # Skip test/demo/mock meetings
            if should_skip_meeting(event_name):
                logger.debug("skipping mock meeting", title=event_name, event_id=event_id)
                return None

            # Parse date
            event_date_str = event.get("EventDate")
            event_time_str = event.get("EventTime")

            start_datetime = None
            if event_date_str:
                start_datetime = combine_date_time(event_date_str, event_time_str)

            # Parse meeting status from title and agenda status
            meeting_status = self._parse_meeting_status(event_name, event_agenda_status)

            meeting = {
                "vendor_id": str(event_id),
                "title": event_name,
                "start": start_datetime,
            }

            # Include body ID for committee linkage
            if event_body_id:
                meeting["vendor_body_id"] = str(event_body_id)

            if event_location:
                meeting["location"] = event_location

            if meeting_status:
                meeting["meeting_status"] = meeting_status

            # Try to get agenda PDF URL from API
            agenda_url = event.get("EventAgendaFile")
            packet_url = event.get("EventMinutesFile")  # Sometimes agenda is in minutes field
            minutes_url = event.get("EventMinutesFile")

            if minutes_url:
                meeting["minutes_url"] = minutes_url

            if self._minutes_discovery_only:
                return meeting

            # Fetch agenda items for this event (concurrent with any HTML
            # agenda URL fallback below) only during a full sync.
            items_task = asyncio.create_task(self._fetch_event_items_api(event_id))

            # If API didn't provide agenda URL, discover from HTML detail page.
            # Try two URL formats: GUID-based (common) then InSiteURL/LEGID (San Jose).
            if not agenda_url:
                detail_urls = []
                if event_guid:
                    detail_urls.append(
                        f"https://{self.slug}.legistar.com/MeetingDetail.aspx?GUID={event_guid}"
                    )
                in_site_url = event.get("EventInSiteURL")
                if in_site_url and in_site_url not in detail_urls:
                    detail_urls.append(in_site_url)

                for detail_url in detail_urls:
                    try:
                        response = await self._get(detail_url)
                        html = await response.text()

                        # Find agenda PDF link (View.ashx?M=A, not M=AADA)
                        agenda_match = re.search(
                            r'href="([^"]*View\.ashx\?M=A&[^"]*)"', html, re.IGNORECASE
                        )
                        if agenda_match:
                            agenda_url = urljoin(detail_url, agenda_match.group(1))
                            break
                    except (AttributeError, IndexError, VendorHTTPError, aiohttp.ClientError):
                        continue

            # Wait for items to finish fetching
            items = await items_task

            if items:
                meeting["items"] = items
                if agenda_url:
                    meeting["agenda_url"] = agenda_url
            elif agenda_url:
                # No API items — try parsing the agenda PDF for items
                # (some cities like San Jose have thin URL agendas with hyperlinked staff reports)
                chunked_items = await self._chunk_agenda_then_packet(
                    agenda_url=agenda_url,
                    vendor_id=str(event_id),
                )
                if chunked_items:
                    meeting["items"] = chunked_items
                    logger.info(
                        "legistar chunked items from agenda PDF",
                        slug=self.slug, event_id=event_id,
                        item_count=len(chunked_items),
                    )
                meeting["agenda_url"] = agenda_url
            if packet_url and "agenda_url" not in meeting:
                meeting["packet_url"] = packet_url
            return meeting

        except (AttributeError, ValueError, TypeError) as e:
            logger.warning("failed to process API event", error=str(e), error_type=type(e).__name__)
            return None

    async def _fetch_event_items_api(self, event_id: int) -> List[Dict[str, Any]]:
        """Fetch agenda items for an event from API with full metadata"""
        try:
            url = f"{self.base_url}/Events/{event_id}/EventItems"
            params = {}
            if self.api_token:
                params["token"] = self.api_token

            response = await self._get(url, params=params)

            # Parse response
            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                event_items = await response.json()
            else:
                text = await response.text()
                event_items = self._parse_xml_event_items(text)

            # Process items concurrently (each may fetch matter metadata/attachments)
            item_tasks = []
            for item_data in event_items:
                item_tasks.append(self._process_api_item(item_data))

            processed_items = await asyncio.gather(*item_tasks, return_exceptions=True)

            # Filter out errors
            items = []
            for idx, item in enumerate(processed_items):
                if isinstance(item, Exception):
                    logger.warning("item processing failed", event_id=event_id, item_index=idx, error=str(item))
                elif isinstance(item, dict):
                    items.append(item)

            return items

        except (VendorHTTPError, aiohttp.ClientError, VendorParsingError) as e:
            logger.warning("failed to fetch event items from API", event_id=event_id, error=str(e))
            return []

    def _parse_xml_event_items(self, xml_text: str) -> List[Dict]:
        """Parse XML event items response"""
        root = ET.fromstring(xml_text)
        ns = {'atom': 'http://www.w3.org/2005/Atom', 'd': 'http://schemas.microsoft.com/ado/2007/08/dataservices', 'm': 'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata'}

        items = []
        for entry in root.findall('.//atom:entry', ns):
            content = entry.find('.//m:properties', ns)
            if content is not None:
                item = {}
                for prop in content:
                    tag = prop.tag.split('}')[1] if '}' in prop.tag else prop.tag
                    item[tag] = prop.text
                items.append(item)

        return items

    async def _process_api_item(self, item_data: Dict) -> Optional[Dict[str, Any]]:
        """Process API item into standardized format with full metadata"""
        try:
            item_id = item_data.get("EventItemId")
            if not item_id:
                return None

            # Get matter ID (for deduplication)
            matter_id = item_data.get("EventItemMatterId")
            matter_file = item_data.get("EventItemMatterFile")
            agenda_number = item_data.get("EventItemAgendaNumber")

            # Get title
            title = item_data.get("EventItemTitle") or item_data.get("EventItemMatterName") or "Untitled Item"

            # Get sequence
            sequence = item_data.get("EventItemAgendaSequence")
            if sequence:
                try:
                    sequence = int(sequence)
                except (ValueError, TypeError):
                    sequence = 0
            else:
                sequence = 0

            # Build item dictionary
            item = {
                "vendor_item_id": str(item_id),  # EventItemId - orchestrator generates final item_id
                "title": title,
                "sequence": sequence,
            }

            if matter_id:
                item["matter_id"] = str(matter_id)
            if matter_file:
                item["matter_file"] = matter_file
            if agenda_number:
                item["agenda_number"] = agenda_number

            # Fetch matter metadata, attachments, and votes concurrently
            # Votes keyed by event_item_id, metadata/attachments by matter_id
            votes_task = asyncio.create_task(self._fetch_event_item_votes_api(int(item_id)))

            if matter_id:
                # Fetch metadata and attachments concurrently
                metadata_task = asyncio.create_task(self._fetch_matter_metadata_async(matter_id))
                attachments_task = asyncio.create_task(self._fetch_matter_attachments_async(matter_id))

                votes, metadata, attachments = await asyncio.gather(
                    votes_task, metadata_task, attachments_task, return_exceptions=True
                )

                # Handle metadata
                if isinstance(metadata, dict):
                    if metadata.get("matter_type"):
                        item["matter_type"] = metadata["matter_type"]
                    if metadata.get("matter_status"):
                        item["matter_status"] = metadata["matter_status"]
                    if metadata.get("sponsors"):
                        item["sponsors"] = metadata["sponsors"]

                # Handle attachments
                if isinstance(attachments, list) and attachments:
                    item["attachments"] = attachments
            else:
                # No matter_id - still fetch votes
                votes = await votes_task

            # Handle votes (keyed by event_item_id, not matter_id)
            if isinstance(votes, list) and votes:
                item["votes"] = votes

            return item

        except (AttributeError, ValueError, TypeError) as e:
            logger.warning("failed to process API item", error=str(e), error_type=type(e).__name__)
            return None

    async def _fetch_matter_metadata_async(self, matter_id: int) -> Dict[str, Any]:
        """Fetch matter_type, lifecycle status, and sponsors from API.

        MatterStatusName rides along on the /matters/{id} response we already
        request for the type, so it costs no extra call. It is the vendor's own
        answer to "is this alive" -- "Placed On File", "Passed", "In Committee".
        Vote tallies cannot substitute: a motion to place a matter on file
        passes 15-0 and kills the matter, so a motion-scoped outcome says
        nothing about the matter's fate.
        """
        metadata: Dict[str, Any] = {
            "matter_type": None,
            "matter_status": None,
            "sponsors": [],
        }

        try:
            # Fetch matter details for type
            matter_url = f"{self.base_url}/matters/{matter_id}"
            params = {"token": self.api_token} if self.api_token else {}
            response = await self._get(matter_url, params=params)

            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                matter_data = await response.json()
                if matter_data:
                    metadata["matter_type"] = matter_data.get("MatterTypeName")
                    metadata["matter_status"] = self._map_status(
                        matter_data.get("MatterStatusName")
                    )
            else:
                # XML fallback - NYC returns XML from Legistar API
                text = await response.text()
                matter_data = self._parse_xml_matter(text)
                if matter_data:
                    metadata["matter_type"] = matter_data.get("MatterTypeName")
                    metadata["matter_status"] = self._map_status(
                        matter_data.get("MatterStatusName")
                    )

            # Fetch sponsors
            sponsors_url = f"{self.base_url}/matters/{matter_id}/sponsors"
            response = await self._get(sponsors_url, params=params)

            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                sponsors_data = await response.json()
            else:
                # XML fallback - NYC returns XML from Legistar API
                text = await response.text()
                sponsors_data = self._parse_xml_sponsors(text)

            if sponsors_data:
                # Extract sponsor names, sorted by sequence
                metadata["sponsors"] = [
                    s.get("MatterSponsorName")
                    for s in sorted(sponsors_data, key=lambda x: x.get("MatterSponsorSequence", 999))
                    if s.get("MatterSponsorName")
                ]

        except (VendorHTTPError, aiohttp.ClientError, JSONDecodeError, ValueError) as e:
            logger.debug("could not fetch matter metadata", matter_id=matter_id, error=str(e))

        return metadata

    async def _fetch_event_item_votes_api(self, event_item_id: int) -> List[Dict[str, Any]]:
        """Fetch votes for a specific event item from API."""
        try:
            votes_url = f"{self.base_url}/EventItems/{event_item_id}/Votes"
            params = {"token": self.api_token} if self.api_token else {}

            response = await self._get(votes_url, params=params)

            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                raw_votes = await response.json()
            else:
                # XML fallback - NYC returns XML from Legistar API
                text = await response.text()
                raw_votes = self._parse_xml_votes(text)

            votes = []
            for vote in raw_votes:
                name = (vote.get("VotePersonName") or "").strip()
                vote_value = (vote.get("VoteValueName") or "").strip()
                person_id = vote.get("VotePersonId")
                sequence = vote.get("VoteSort", 0)

                if not name or not vote_value:
                    continue

                # Normalize vote value to our standard format
                vote_normalized = self._normalize_vote_value(vote_value)

                votes.append({
                    "name": name,
                    "vote": vote_normalized,
                    "sequence": sequence,
                    "person_id": person_id,
                })

            return votes

        except (VendorHTTPError, aiohttp.ClientError, JSONDecodeError, ValueError) as e:
            logger.debug("could not fetch event item votes", event_item_id=event_item_id, error=str(e))
            return []

    def _normalize_vote_value(self, value: str) -> str:
        """Normalize Legistar vote value to standard format"""
        value_lower = value.lower()
        vote_map = {
            "affirmative": "yes",
            "aye": "yes",
            "yea": "yes",
            "yes": "yes",
            "negative": "no",
            "nay": "no",
            "no": "no",
            "absent": "absent",
            "excused": "absent",
            "not present": "absent",
            "abstain": "abstain",
            "abstained": "abstain",
            "present": "present",
            "recused": "recused",
            "recuse": "recused",
            "conflict": "recused",
        }
        return vote_map.get(value_lower, "not_voting")

    def _map_status(self, raw: Optional[str]) -> Optional[str]:
        """Map a Legistar status to engagic's vocabulary, logging misses."""
        mapped = map_matter_status(raw)
        if raw and mapped is None:
            logger.debug(
                "unrecognized vendor matter status",
                slug=self.slug,
                status=raw,
            )
        return mapped

    async def _fetch_matter_attachments_async(self, matter_id: int) -> List[Dict[str, Any]]:
        """Fetch attachments for a specific matter from API."""
        try:
            attachments_url = f"{self.base_url}/matters/{matter_id}/attachments"
            params = {"token": self.api_token} if self.api_token else {}

            response = await self._get(attachments_url, params=params)

            # Parse response (JSON or XML)
            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                raw_attachments = await response.json()
            else:
                text = await response.text()
                raw_attachments = self._parse_xml_attachments(text)

            attachments = []
            for att in raw_attachments:
                name = (att.get("MatterAttachmentName") or "").strip()
                url = (att.get("MatterAttachmentHyperlink") or "").strip()

                if not url:
                    continue

                # Some Legistar instances (e.g., Madison) return just a filename
                # instead of a full URL. Construct the proper URL in that case.
                if not url.startswith("http"):
                    url = f"https://{self.slug}.legistar1.com/{self.slug}/attachments/{url}"

                # Determine file type from URL
                url_lower = url.lower()
                if url_lower.endswith(".pdf"):
                    file_type = "pdf"
                elif url_lower.endswith((".doc", ".docx")):
                    file_type = "doc"
                else:
                    file_type = "unknown"

                attachments.append({"name": name, "url": url, "type": file_type})

            return attachments

        except (VendorHTTPError, aiohttp.ClientError, VendorParsingError) as e:
            logger.debug("failed to fetch matter attachments", matter_id=matter_id, error=str(e))
            return []

    def _parse_xml_attachments(self, xml_text: str) -> List[Dict[str, Any]]:
        """Parse XML response for matter attachments."""
        attachments = []

        try:
            root = ET.fromstring(xml_text)

            # Handle namespace
            ns = {'ns': 'http://schemas.datacontract.org/2004/07/LegistarWebAPI.Models.v1'}

            # Find all GranicusMatterAttachment elements
            for att_elem in root.findall('.//ns:GranicusMatterAttachment', ns):
                attachment = {}

                # Map XML fields to JSON field names
                field_map = {
                    'MatterAttachmentName': 'MatterAttachmentName',
                    'MatterAttachmentHyperlink': 'MatterAttachmentHyperlink',
                }

                for xml_field, json_field in field_map.items():
                    elem = att_elem.find(f'ns:{xml_field}', ns)
                    if elem is not None and elem.text:
                        attachment[json_field] = elem.text

                # Only add attachments that have at least a hyperlink
                if 'MatterAttachmentHyperlink' in attachment:
                    attachments.append(attachment)

            return attachments

        except ET.ParseError as e:
            logger.error("XML parsing error for attachments", error=str(e))
            raise

    def _parse_xml_votes(self, xml_text: str) -> List[Dict[str, Any]]:
        """Parse XML response for votes (NYC returns XML instead of JSON)."""
        votes = []

        try:
            root = ET.fromstring(xml_text)

            # Handle Legistar namespace
            ns = {'ns': 'http://schemas.datacontract.org/2004/07/LegistarWebAPI.Models.v1'}

            # Find all GranicusEventItemVote elements (may also be EventItemVote)
            vote_elements = root.findall('.//ns:GranicusEventItemVote', ns)
            if not vote_elements:
                vote_elements = root.findall('.//ns:EventItemVote', ns)

            for vote_elem in vote_elements:
                vote = {}

                # Map XML fields to JSON field names
                field_map = {
                    'VotePersonName': 'VotePersonName',
                    'VoteValueName': 'VoteValueName',
                    'VotePersonId': 'VotePersonId',
                    'VoteSort': 'VoteSort',
                }

                for xml_field, json_field in field_map.items():
                    elem = vote_elem.find(f'ns:{xml_field}', ns)
                    if elem is not None and elem.text:
                        # Convert numeric fields
                        if xml_field in ('VotePersonId', 'VoteSort'):
                            try:
                                vote[json_field] = int(elem.text)
                            except ValueError:
                                vote[json_field] = 0
                        else:
                            vote[json_field] = elem.text

                # Only add votes that have person name and vote value
                if vote.get('VotePersonName') and vote.get('VoteValueName'):
                    votes.append(vote)

            logger.debug("parsed xml votes", count=len(votes))
            return votes

        except ET.ParseError as e:
            logger.warning("XML parsing error for votes", error=str(e))
            return []

    def _parse_xml_matter(self, xml_text: str) -> Dict[str, Any]:
        """Parse XML response for single matter details."""
        try:
            root = ET.fromstring(xml_text)

            # Handle Legistar namespace
            ns = {'ns': 'http://schemas.datacontract.org/2004/07/LegistarWebAPI.Models.v1'}

            # Find matter element (may be GranicusMatter or Matter)
            matter_elem = root.find('.//ns:GranicusMatter', ns)
            if matter_elem is None:
                matter_elem = root.find('.//ns:Matter', ns)
            if matter_elem is None:
                return {}

            matter = {}
            field_map = {
                'MatterTypeName': 'MatterTypeName',
                'MatterStatusName': 'MatterStatusName',
                'MatterName': 'MatterName',
                'MatterFile': 'MatterFile',
                'MatterId': 'MatterId',
            }

            for xml_field, json_field in field_map.items():
                elem = matter_elem.find(f'ns:{xml_field}', ns)
                if elem is not None and elem.text:
                    matter[json_field] = elem.text

            return matter

        except ET.ParseError as e:
            logger.warning("XML parsing error for matter", error=str(e))
            return {}

    def _parse_xml_sponsors(self, xml_text: str) -> List[Dict[str, Any]]:
        """Parse XML response for matter sponsors."""
        sponsors = []

        try:
            root = ET.fromstring(xml_text)

            # Handle Legistar namespace
            ns = {'ns': 'http://schemas.datacontract.org/2004/07/LegistarWebAPI.Models.v1'}

            # Find all sponsor elements
            sponsor_elements = root.findall('.//ns:GranicusMatterSponsor', ns)
            if not sponsor_elements:
                sponsor_elements = root.findall('.//ns:MatterSponsor', ns)

            for sponsor_elem in sponsor_elements:
                sponsor = {}

                field_map = {
                    'MatterSponsorName': 'MatterSponsorName',
                    'MatterSponsorSequence': 'MatterSponsorSequence',
                }

                for xml_field, json_field in field_map.items():
                    elem = sponsor_elem.find(f'ns:{xml_field}', ns)
                    if elem is not None and elem.text:
                        if xml_field == 'MatterSponsorSequence':
                            try:
                                sponsor[json_field] = int(elem.text)
                            except ValueError:
                                sponsor[json_field] = 999
                        else:
                            sponsor[json_field] = elem.text

                if sponsor.get('MatterSponsorName'):
                    sponsors.append(sponsor)

            logger.debug("parsed xml sponsors", count=len(sponsors))
            return sponsors

        except ET.ParseError as e:
            logger.warning("XML parsing error for sponsors", error=str(e))
            return []

    async def _fetch_meetings_html(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings by scraping HTML calendar (fallback)."""
        # Try common Legistar calendar URL patterns
        calendar_urls = [
            f"https://{self.slug}.legistar.com/Calendar.aspx",
            f"https://webapi.legistar.com/{self.slug}/Calendar.aspx",
        ]

        soup = None
        calendar_url = None
        for url in calendar_urls:
            try:
                response = await self._get(url)
                html = await response.text()
                # Parse HTML in thread pool (BeautifulSoup is CPU-bound)
                soup = await asyncio.to_thread(self._parse_html, html)
                calendar_url = url
                logger.info("legistar found HTML calendar", slug=self.slug, url=url)
                break
            except (VendorHTTPError, aiohttp.ClientError, VendorParsingError) as e:
                logger.debug("calendar not found", slug=self.slug, url=url, error=str(e))
                continue

        if not soup or not calendar_url:
            logger.error("could not find HTML calendar at any known URL", slug=self.slug)
            return []

        # Extract base URL for building absolute URLs
        html_base_url = calendar_url.rsplit('/', 1)[0]

        # Date range filter
        start_date, end_date = self._date_range(days_back, days_forward)

        # Find meeting rows in RadGrid calendar table
        meeting_rows = soup.find_all("tr", class_=["rgRow", "rgAltRow"])

        if not meeting_rows:
            logger.warning("no meeting rows found in HTML calendar", slug=self.slug)
            return []

        logger.info(
            "legistar found meetings in HTML",
            slug=self.slug,
            count=len(meeting_rows)
        )

        # Process meetings concurrently
        meeting_tasks = []
        for row in meeting_rows:
            meeting_tasks.append(
                self._process_html_meeting_row(row, html_base_url, start_date, end_date)
            )

        processed_meetings = await self._bounded_gather(meeting_tasks, max_concurrent=5, return_exceptions=True)

        # Filter out None, errors, and duplicates (calendar has upcoming + all sections)
        seen_ids = set()
        meetings = []
        for meeting in processed_meetings:
            if isinstance(meeting, dict):
                vid = meeting.get("vendor_id")
                if vid and vid in seen_ids:
                    continue
                if vid:
                    seen_ids.add(vid)
                meetings.append(meeting)

        logger.info("legistar yielded meetings from HTML", slug=self.slug, count=len(meetings))

        return meetings

    async def _process_html_meeting_row(
        self,
        row,
        html_base_url: str,
        start_date: datetime,
        end_date: datetime
    ) -> Optional[Dict[str, Any]]:
        """Process a single HTML meeting row"""
        try:
            cells = row.find_all("td")
            if len(cells) < 6:
                return None

            # Capture BOTH links when present.
            # MeetingDetail is preferred by default, but (a) when prefer_aada
            # is set for this slug we skip straight to AADA, and (b) when
            # MeetingDetail returns a stub item list we retry with AADA.
            detail_link = row.find("a", href=lambda x: x and "MeetingDetail.aspx" in x)
            aada_link = row.find("a", href=lambda x: x and "View.ashx" in x and "M=AADA" in x)
            detail_url = None
            aada_url = None
            meeting_id = None

            if detail_link:
                detail_url = urljoin(html_base_url, detail_link["href"])
                meeting_id_match = re.search(r"ID=(\d+)", detail_url)
                if meeting_id_match:
                    meeting_id = meeting_id_match.group(1)

            if aada_link:
                aada_url = urljoin(html_base_url, aada_link["href"])
                if not meeting_id:
                    meeting_id_match = re.search(r"ID=(\d+)", aada_url)
                    if meeting_id_match:
                        meeting_id = meeting_id_match.group(1)

            if not meeting_id:
                return None

            # Skip video clip IDs
            if meeting_id.startswith('clip_'):
                return None

            # Extract title - try multiple strategies
            title = None
            title_link = row.find("a", id=lambda x: x and "hypBody" in x)
            if title_link:
                title = title_link.get_text(strip=True)
            elif cells:
                first_link = cells[0].find("a")
                if first_link:
                    title = first_link.get_text(strip=True)

            if not title and detail_link:
                title = detail_link.get_text(strip=True)
            if not title or title == "Details":
                title = "Meeting"

            # Skip video clip durations (pattern: "01h 49m")
            if re.match(r'^\d+h\s+\d+m\s*$', title):
                return None

            # Skip test/demo/mock meetings
            if should_skip_meeting(title):
                logger.debug("skipping mock meeting", title=title, meeting_id=meeting_id)
                return None

            # Extract date
            meeting_dt = None
            sorted_cell = row.find("td", class_="rgSorted")
            if sorted_cell:
                parsed_date = self._parse_date(sorted_cell.get_text(strip=True))
                if parsed_date:
                    meeting_dt = parsed_date

            if not meeting_dt:
                for cell in cells:
                    cell_text = cell.get_text(strip=True)
                    parsed_date = self._parse_date(cell_text)
                    if parsed_date:
                        meeting_dt = parsed_date
                        break

            if not meeting_dt:
                return None

            # Extract time from lblTime span (separate column in some Legistar instances)
            time_span = row.find("span", id=lambda x: x and "lblTime" in x)
            if time_span:
                time_text = time_span.get_text(strip=True)
                if time_text and time_text.lower() not in ["", "tbd", "n/a"]:
                    # Combine date with time
                    combined = combine_date_time(meeting_dt.isoformat(), time_text)
                    if combined:
                        try:
                            meeting_dt = datetime.fromisoformat(combined)
                        except ValueError:
                            pass  # Keep original date if combining fails

            # Filter by date range
            if not (start_date <= meeting_dt <= end_date):
                return None

            # Extract agenda PDF from calendar row (M=A only, not M=AADA)
            packet_url = None
            agenda_link = row.find("a", href=lambda x: x and "View.ashx" in x and (
                re.search(r'M=A(&|$)', x) or "agenda" in x.lower()
            ))
            if agenda_link:
                packet_url = urljoin(html_base_url, agenda_link["href"])

            # Minutes PDF from calendar row (View.ashx?M=M, not M=A/M=AADA)
            minutes_url = None
            minutes_link = row.find("a", href=lambda x: x and "View.ashx" in x and re.search(r'M=M(&|$)', x))
            if minutes_link:
                minutes_url = urljoin(html_base_url, minutes_link["href"])

            if self._minutes_discovery_only:
                if not minutes_url:
                    return None
                meeting_data = {
                    "vendor_id": meeting_id,
                    "title": title,
                    "start": meeting_dt.isoformat(),
                    "minutes_url": minutes_url,
                }
                meeting_status = self._parse_meeting_status(title)
                if meeting_status:
                    meeting_data["meeting_status"] = meeting_status
                return meeting_data

            # Config-driven short-circuit: some Legistar instances (e.g. LA
            # County) expose a MeetingDetail link that technically resolves
            # but only contains stub rows. Prefer AADA for known offenders.
            if self.prefer_aada and aada_url:
                meeting_data = await self._fetch_aada_agenda_async(
                    meeting_id, meeting_dt, title, aada_url, packet_url
                )
                if meeting_data and minutes_url:
                    meeting_data["minutes_url"] = minutes_url
                return meeting_data

            if detail_url:
                meeting_data = await self._fetch_meeting_detail_html_async(
                    meeting_id, meeting_dt, title, detail_url, packet_url
                )
                # Runtime retry: if MeetingDetail returned a stub and an AADA
                # link was advertised in the calendar row, fetch AADA and keep
                # whichever yielded more items.
                if aada_url and self._is_stub_meeting(meeting_data):
                    logger.info(
                        "meeting detail returned stub, retrying via AADA",
                        slug=self.slug,
                        meeting_id=meeting_id,
                        detail_item_count=len((meeting_data or {}).get("items", [])),
                    )
                    aada_data = await self._fetch_aada_agenda_async(
                        meeting_id, meeting_dt, title, aada_url, packet_url
                    )
                    meeting_data = self._pick_richer_meeting(meeting_data, aada_data)
                if meeting_data and minutes_url:
                    meeting_data["minutes_url"] = minutes_url
                return meeting_data

            if aada_url:
                meeting_data = await self._fetch_aada_agenda_async(
                    meeting_id, meeting_dt, title, aada_url, packet_url
                )
                if meeting_data and minutes_url:
                    meeting_data["minutes_url"] = minutes_url
                return meeting_data

            return None

        except (AttributeError, IndexError, ValueError, TypeError) as e:
            logger.warning("error parsing meeting row", error=str(e))
            return None

    @staticmethod
    def _is_stub_meeting(meeting_data: Optional[Dict[str, Any]]) -> bool:
        """True when MeetingDetail yielded fewer items than AADA_RETRY_MIN_ITEMS.

        A "stub" is a MeetingDetail page that renders but only contains
        procedural filler (Public Comment, Adjournment) because the full
        agenda is published via AADA only. Confidence: 7/10 -- item count
        is a cheap proxy; some genuinely short meetings will trigger a
        redundant AADA fetch, which is acceptable overhead.
        """
        if not meeting_data:
            return True
        items = meeting_data.get("items") or []
        return len(items) < AADA_RETRY_MIN_ITEMS

    @staticmethod
    def _pick_richer_meeting(
        detail_data: Optional[Dict[str, Any]],
        aada_data: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Return whichever meeting carries more items, preferring AADA on ties."""
        detail_items = len((detail_data or {}).get("items") or [])
        aada_items = len((aada_data or {}).get("items") or [])
        if aada_items >= detail_items and aada_data is not None:
            return aada_data
        return detail_data

    async def _fetch_meeting_detail_html_async(
        self,
        meeting_id: str,
        meeting_dt: datetime,
        title: str,
        detail_url: str,
        calendar_packet_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch and parse meeting detail page for agenda items."""
        items = []
        packet_url = calendar_packet_url

        # Extract base URL from detail_url
        parsed = urlparse(detail_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        # Try to fetch detail page
        try:
            response = await self._get(detail_url)
            html = await response.text()
            soup = await asyncio.to_thread(self._parse_html, html)

            # Parse agenda items from detail page using dedicated parser
            items = await asyncio.to_thread(self._parse_html_agenda_items, soup, meeting_id, base_url)

            # Map item_type to matter_type and identify substantive items for attachment fetching
            substantive_items = []

            for item in items:
                item_title = item.get('title', '')
                item_type = item.get('item_type', '')

                # Map item_type to matter_type for HTML-parsed items
                # (API path handles this separately via _fetch_matter_metadata_async)
                if item_type and 'matter_type' not in item:
                    item['matter_type'] = item_type

                # Only fetch attachments for non-procedural items (network optimization)
                if not should_skip_processing(item_title, item_type):
                    substantive_items.append(item)

            # Fetch attachments for substantive items concurrently
            if substantive_items:
                attachment_tasks = [
                    self._fetch_item_attachments_async(item, base_url)
                    for item in substantive_items
                ]
                attachment_results = await self._bounded_gather(attachment_tasks, max_concurrent=5, return_exceptions=True)

                for item, attachments in zip(substantive_items, attachment_results):
                    if isinstance(attachments, list) and attachments:
                        item['attachments'] = attachments

            # Look for agenda PDF link if not provided from calendar
            if not packet_url:
                agenda_links = soup.find_all(
                    "a",
                    href=lambda x: bool(x and ".pdf" in x.lower()),
                )
                for link in agenda_links:
                    link_text = link.get_text(strip=True).lower()
                    if "agenda" in link_text or "packet" in link_text:
                        packet_url = urljoin(base_url, string_attr(link, "href"))
                        break

        except (VendorHTTPError, aiohttp.ClientError, VendorParsingError) as e:
            logger.debug("detail page unavailable", slug=self.slug, meeting_id=meeting_id, error=str(e))

        meeting_data: Dict[str, Any] = {
            "vendor_id": str(meeting_id),
            "title": title,
            "start": meeting_dt.isoformat(),
        }

        # Architecture: items extracted → agenda_url, no items → packet_url
        if items:
            if packet_url:
                meeting_data["agenda_url"] = packet_url
            meeting_data["items"] = items
        elif packet_url:
            meeting_data["packet_url"] = packet_url
        else:
            # No items and no packet - skip this meeting
            return None

        return meeting_data

    async def _fetch_aada_agenda_async(
        self,
        meeting_id: str,
        meeting_dt: datetime,
        title: str,
        aada_url: str,
        calendar_packet_url: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch and parse AADA (Accessible Agenda) page for items.

        Fallback for sites where MeetingDetail.aspx is not publicly viewable
        but an accessible HTML agenda is available (e.g. LA County).
        """
        items = []
        packet_url = calendar_packet_url

        try:
            response = await self._get(aada_url)
            html = await response.text()
            parsed_data = await asyncio.to_thread(
                parse_aada_html, html, meeting_id, aada_url
            )
            items = parsed_data.get('items', [])
            logger.info(
                "parsed AADA agenda",
                slug=self.slug,
                meeting_id=meeting_id,
                item_count=len(items),
            )
        except (VendorHTTPError, aiohttp.ClientError, VendorParsingError) as e:
            logger.debug("AADA page unavailable", slug=self.slug, meeting_id=meeting_id, error=str(e))

        meeting_data: Dict[str, Any] = {
            "vendor_id": str(meeting_id),
            "title": title,
            "start": meeting_dt.isoformat(),
        }

        if items:
            if packet_url:
                meeting_data["agenda_url"] = packet_url
            meeting_data["items"] = items
        elif packet_url:
            meeting_data["packet_url"] = packet_url
        else:
            return None

        return meeting_data

    def _parse_html_agenda_items(
        self, soup, meeting_id: str, base_url: str
    ) -> List[Dict[str, Any]]:
        """Parse agenda items from meeting detail HTML using dedicated parser."""
        # Convert soup back to HTML string for the parser
        html = str(soup)

        # Use dedicated Legistar HTML parser
        parsed_data = parse_html_agenda(html, meeting_id, base_url)
        items = parsed_data.get('items', [])

        return items

    @staticmethod
    def _parse_html(html: str):
        """Parse HTML to BeautifulSoup (for asyncio.to_thread)."""
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser")

    async def _fetch_item_attachments_async(
        self, item: Dict[str, Any], base_url: str
    ) -> List[Dict[str, Any]]:
        """Fetch attachments from LegislationDetail page."""
        legislation_url = item.get('legislation_url')
        if not legislation_url:
            return []

        try:
            response = await self._get(legislation_url)
            html = await response.text()

            # Parse attachments in thread pool
            attachments = await asyncio.to_thread(
                parse_legislation_attachments, html, base_url
            )

            # Filter to include at most one Leg Ver attachment
            attachments = self._filter_leg_ver_attachments(attachments)

            return attachments

        except (VendorHTTPError, aiohttp.ClientError, VendorParsingError) as e:
            logger.warning("failed to fetch item attachments", slug=self.slug, item_id=item.get('item_id'), error=str(e))
            return []

    def _filter_leg_ver_attachments(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter to at most one Leg Ver/Dig attachment (select highest version number)."""
        leg_ver_attachments = []
        other_attachments = []

        # Match "Leg Ver", "Leg Dig Ver", "Legislative Version", etc.
        leg_pattern = re.compile(r'leg(?:islative)?\s*(?:dig(?:est)?)?\s*ver(?:sion)?\s*(\d+)?', re.IGNORECASE)

        for att in attachments:
            name = att.get('name', '')
            match = leg_pattern.search(name)
            if match:
                # Extract version number if present, default to 0
                version = int(match.group(1)) if match.group(1) else 0
                leg_ver_attachments.append((version, att))
            else:
                other_attachments.append(att)

        # Select highest version number
        selected_leg_ver = None
        if leg_ver_attachments:
            # Sort by version descending, pick highest
            leg_ver_attachments.sort(key=lambda x: x[0], reverse=True)
            selected_leg_ver = leg_ver_attachments[0][1]
            logger.debug(
                "selected leg ver attachment",
                name=selected_leg_ver.get('name'),
                version=leg_ver_attachments[0][0],
                alternatives=len(leg_ver_attachments) - 1
            )

        # Combine: at most one Leg Ver + all other attachments
        filtered = other_attachments
        if selected_leg_ver:
            filtered.insert(0, selected_leg_ver)

        return filtered

    # ==================
    # ROSTER FETCHING
    # ==================

    async def fetch_roster_data(self) -> Dict[str, Any]:
        """Fetch committee roster data (bodies and current office records).

        This is intended for one-time population of committee memberships.
        Returns raw API data for processing by the roster sync script.

        Note: Persons API is not used - we get all person info from OfficeRecords.
        This reduces API calls from 3 to 2 per city.

        Returns:
            Dict with 'bodies', 'persons' (empty), 'office_records' lists
        """
        logger.info("fetching roster data", slug=self.slug)

        bodies = await self._fetch_bodies()
        office_records = await self._fetch_office_records()

        logger.info(
            "roster data fetched",
            slug=self.slug,
            bodies=len(bodies),
            office_records=len(office_records),
        )

        return {
            "bodies": bodies,
            "persons": [],  # Not used - info comes from OfficeRecords
            "office_records": office_records,
        }

    async def _fetch_bodies(self) -> List[Dict[str, Any]]:
        """Fetch all Bodies (committees) from Legistar API."""
        try:
            url = f"{self.base_url}/Bodies"
            params: Dict[str, Any] = {"$top": 1000}
            if self.api_token:
                params["token"] = self.api_token

            response = await self._get(url, params=params)

            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                bodies = await response.json()
            else:
                text = await response.text()
                bodies = self._parse_xml_generic(text, "GranicusBody", "Body")

            # Filter to active bodies only
            active_bodies = [
                b for b in bodies
                if b.get("BodyActiveFlag") == 1 or b.get("BodyActiveFlag") == "1"
            ]

            logger.debug("fetched bodies", slug=self.slug, total=len(bodies), active=len(active_bodies))
            return active_bodies

        except (VendorHTTPError, aiohttp.ClientError) as e:
            logger.warning("failed to fetch bodies", slug=self.slug, error=str(e))
            return []

    async def _fetch_persons(self) -> List[Dict[str, Any]]:
        """Fetch active Persons (council members) from Legistar API."""
        try:
            url = f"{self.base_url}/Persons"
            params = {
                "$filter": "PersonActiveFlag eq 1",
                "$top": 1000,
            }
            if self.api_token:
                params["token"] = self.api_token

            response = await self._get(url, params=params)

            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                persons = await response.json()
            else:
                text = await response.text()
                persons = self._parse_xml_generic(text, "GranicusPerson", "Person")

            logger.debug("fetched persons", slug=self.slug, count=len(persons))
            return persons

        except (VendorHTTPError, aiohttp.ClientError) as e:
            logger.warning("failed to fetch persons", slug=self.slug, error=str(e))
            return []

    async def _fetch_office_records(self, current_only: bool = True) -> List[Dict[str, Any]]:
        """Fetch OfficeRecords (committee memberships) from Legistar API.

        Args:
            current_only: Only fetch records where EndDate >= today (default True)

        Returns:
            List of office record dicts with person-to-body mappings
        """
        try:
            url = f"{self.base_url}/OfficeRecords"

            # Filter to current memberships only (EndDate >= today)
            from datetime import datetime
            today = datetime.now().strftime("%Y-%m-%d")

            params: Dict[str, Any] = {"$top": 1000}
            if current_only:
                params["$filter"] = f"OfficeRecordEndDate ge datetime'{today}'"

            if self.api_token:
                params["token"] = self.api_token

            response = await self._get(url, params=params)

            content_type = response.headers.get('content-type', '').lower()
            if 'json' in content_type:
                records = await response.json()
            else:
                text = await response.text()
                records = self._parse_xml_generic(text, "GranicusOfficeRecord", "OfficeRecord")

            logger.debug("fetched office records", slug=self.slug, count=len(records), current_only=current_only)
            return records

        except (VendorHTTPError, aiohttp.ClientError) as e:
            logger.warning("failed to fetch office records", slug=self.slug, error=str(e))
            return []

    def _parse_xml_generic(self, xml_text: str, primary_tag: str, fallback_tag: str) -> List[Dict]:
        """Parse XML response for any Legistar entity type."""
        try:
            root = ET.fromstring(xml_text)
            ns = {'ns': 'http://schemas.datacontract.org/2004/07/LegistarWebAPI.Models.v1'}

            items = []
            elements = root.findall(f'.//ns:{primary_tag}', ns)
            if not elements:
                elements = root.findall(f'.//ns:{fallback_tag}', ns)

            for elem in elements:
                item = {}
                for child in elem:
                    tag = child.tag.split('}')[1] if '}' in child.tag else child.tag
                    if child.text:
                        # Try to parse as int
                        try:
                            item[tag] = int(child.text)
                        except ValueError:
                            item[tag] = child.text
                items.append(item)

            return items

        except ET.ParseError as e:
            logger.warning("XML parsing error", error=str(e))
            return []
