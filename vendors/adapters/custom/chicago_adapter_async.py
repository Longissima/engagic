"""
Async Chicago City Council Adapter - REST API integration

API Base URL: https://api.chicityclerkelms.chicago.gov
API Docs: Swagger 2.0 spec available at /swagger.json

URL patterns:
- Meetings list: /meeting-agenda (with OData filtering)
- Meeting detail: /meeting-agenda/{meetingId}
- Matter details: /matter/{matterId}
- Matter by record: /matter/recordNumber/{recordNumber}
- Votes: /meeting-agenda/{meetingId}/matter/{lineId}/votes

API structure (verified Dec 2025):
- OData-style filtering: filter=date gt datetime'2025-01-01T00:00:00Z'
- Pagination: top (max 500), skip
- Sorting: sort=date desc
- Meeting agenda has nested structure: agenda.groups[].items[]
- NOTE: Chicago API returns empty agenda.groups[] for most meetings
- Fallback: Extract record numbers from agenda PDF, fetch via /matter/recordNumber/
- Minutes: NOT in the meeting-agenda payload. files[] attachmentTypes observed
  across 81 meetings (Jan-Jun 2026): Agenda, Notice, Summary, Other,
  Miscellaneous Business, Monthly Rule 45. Committee "Summary" files are
  summaries of reports, not minutes; the Journal of Proceedings lives behind
  a separate chicityclerk surface -- deferred until a new endpoint is agreed.

Item extraction hierarchy:
1. API agenda.groups[].items[] (primary - rarely populated)
2. PDF extraction (fallback - parse agenda PDF for record numbers)
3. packet_url (last resort - monolithic LLM processing)

Async version with:
- aiohttp for async HTTP requests
- Concurrent matter fetches with asyncio.gather
- Non-blocking I/O for faster processing
"""

import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

import aiohttp

from parsing.chicago_pdf import parse_chicago_agenda_pdf
from parsing.pdf import PdfExtractor
from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector


class AsyncChicagoAdapter(AsyncBaseAdapter):
    """Async Chicago City Council - REST API adapter"""

    # Default matter data returned when fetch fails or matter_id is absent
    _EMPTY_MATTER: Dict[str, Any] = {
        "attachments": [],
        "sponsors": [],
        "matter_status": None,
        "vote_outcome": None,
        "votes": [],
    }

    # Map Chicago status strings to vote outcome
    _STATUS_TO_OUTCOME: Dict[str, Optional[str]] = {
        "adopted": "passed",
        "approved": "passed",
        "passed": "passed",
        "failed": "failed",
        "rejected": "failed",
        "withdrawn": "withdrawn",
        "tabled": "tabled",
        "deferred": "tabled",
        "referred": None,
        "pending": None,
    }

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="chicago", metrics=metrics)
        self.base_url = "https://api.chicityclerkelms.chicago.gov"
        self.pdf_extractor = PdfExtractor.shared()
        self._sync_stats: Dict[str, int] = {}

    def _reset_stats(self) -> None:
        """Reset stats for a new sync operation."""
        self._sync_stats = {
            "meetings_total": 0,
            "meetings_with_items": 0,
            "meetings_with_votes": 0,
            "meetings_with_deadline": 0,
            "total_items": 0,
            "total_attachments": 0,
            "total_votes": 0,
            "api_requests": 0,
        }

    def _normalize_vote_value(self, value: str) -> str:
        """
        Normalize Chicago vote value to standard format.

        Chicago API returns: "Yea", "Nay", "Abstain", "Absent", etc.
        System expects lowercase: "yes", "no", "abstain", "absent", etc.
        """
        value_lower = value.lower()
        vote_map = {
            "yea": "yes",
            "yes": "yes",
            "aye": "yes",
            "nay": "no",
            "no": "no",
            "abstain": "abstain",
            "abstained": "abstain",
            "absent": "absent",
            "excused": "absent",
            "present": "present",
            "recused": "recused",
            "conflict": "recused",
        }
        return vote_map.get(value_lower, "not_voting")

    def _extract_attachments(self, raw_attachments: List[Dict[str, Any]]) -> List[Dict[str, str]]:
        """Extract and normalize attachment metadata from Chicago API response."""
        attachments = []
        for att in raw_attachments:
            file_name = (att.get("fileName") or "").strip()
            path = (att.get("path") or "").strip()
            attachment_type = (att.get("attachmentType") or "").strip()

            if not path:
                continue

            path_lower = path.lower()
            if path_lower.endswith(".pdf"):
                file_type = "pdf"
            elif path_lower.endswith((".doc", ".docx")):
                file_type = "doc"
            elif path_lower.endswith((".xls", ".xlsx")):
                file_type = "spreadsheet"
            else:
                file_type = "unknown"

            attachments.append({
                "name": file_name or attachment_type or "Attachment",
                "url": path,
                "type": file_type,
            })
        return attachments

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings in moving window from Chicago's REST API."""
        self._reset_stats()

        # Build date range based on parameters
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start_date_dt = today - timedelta(days=days_back)
        end_date_dt = today + timedelta(days=days_forward)

        # Format dates for OData filter
        start_date = start_date_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_date = end_date_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build OData filter (no datetime wrapper needed per API docs)
        filter_str = f"date ge {start_date} and date lt {end_date}"

        # Fetch meetings with pagination (API max 500 per page)
        api_url = f"{self.base_url}/meeting-agenda"
        logger.info("fetching meetings", vendor="chicago", slug=self.slug, api_url=api_url)

        all_meetings = []
        skip = 0
        top = 500
        max_meetings = 2000  # Safety cap

        while len(all_meetings) < max_meetings:
            params = {
                "filter": filter_str,
                "sort": "date desc",
                "top": top,
                "skip": skip,
            }

            try:
                response = await self._get(api_url, params=params)
                self._sync_stats["api_requests"] += 1
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # Return empty list on pagination failure - partial data is misleading
                # Base contract: runtime errors should log and return []
                logger.error("network error fetching meetings", vendor="chicago", slug=self.slug, error=str(e), skip=skip)
                return []

            try:
                response_data = await response.json(content_type=None)
            except ValueError as e:
                # Return empty list on parse failure - partial data is misleading
                logger.error("invalid json response", vendor="chicago", slug=self.slug, error=str(e), skip=skip)
                return []

            page_meetings = response_data.get("data", [])
            all_meetings.extend(page_meetings)

            # Check if more pages exist
            meta = response_data.get("meta", {})
            total_count = meta.get("count", len(page_meetings))

            logger.debug(
                "fetched meeting page",
                vendor="chicago",
                slug=self.slug,
                skip=skip,
                page_count=len(page_meetings),
                total_so_far=len(all_meetings),
                api_total=total_count
            )

            if len(page_meetings) < top or len(all_meetings) >= total_count:
                break

            skip += top

        if len(all_meetings) >= max_meetings:
            logger.warning("hit pagination cap", vendor="chicago", slug=self.slug, cap=max_meetings)

        self._sync_stats["meetings_total"] = len(all_meetings)
        logger.info("retrieved meetings", vendor="chicago", slug=self.slug, count=len(all_meetings))

        results = []
        for meeting in all_meetings:
            try:
                result = await self._process_meeting(meeting)
                if result:
                    results.append(result)
                    # Track stats from processed meeting
                    if result.get("items"):
                        self._sync_stats["meetings_with_items"] += 1
                        self._sync_stats["total_items"] += len(result["items"])
                        # Count items with votes
                        items_with_votes = sum(1 for item in result["items"] if item.get("votes"))
                        if items_with_votes > 0:
                            self._sync_stats["meetings_with_votes"] += 1
                            total_votes = sum(len(item.get("votes", [])) for item in result["items"])
                            self._sync_stats["total_votes"] += total_votes
                        # Count attachments
                        for item in result["items"]:
                            self._sync_stats["total_attachments"] += len(item.get("attachments", []))
                    if result.get("participation"):
                        self._sync_stats["meetings_with_deadline"] += 1
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, AttributeError, TypeError, ValueError) as e:
                logger.error(
                    "error processing meeting",
                    vendor="chicago",
                    slug=self.slug,
                    meeting_id=meeting.get('meetingId'),
                    error=str(e),
                    error_type=type(e).__name__,
                )
                continue

        # Log comprehensive stats summary
        logger.info(
            "chicago adapter sync complete",
            vendor="chicago",
            slug=self.slug,
            meetings_total=self._sync_stats["meetings_total"],
            meetings_processed=len(results),
            meetings_with_items=self._sync_stats["meetings_with_items"],
            meetings_with_votes=self._sync_stats["meetings_with_votes"],
            meetings_with_deadline=self._sync_stats["meetings_with_deadline"],
            total_items=self._sync_stats["total_items"],
            total_attachments=self._sync_stats["total_attachments"],
            total_votes=self._sync_stats["total_votes"],
            api_requests=self._sync_stats["api_requests"],
        )

        return results

    async def _process_meeting(self, meeting: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process a single meeting, fetching detail and extracting items."""
        # Extract basic meeting data
        meeting_id = meeting.get("meetingId")
        body = meeting.get("body", "")
        date_str = meeting.get("date")
        location = meeting.get("location")

        if not meeting_id or not date_str:
            logger.warning("meeting missing id or date", vendor="chicago", slug=self.slug, meeting_id=meeting.get('meetingId'))
            return None

        # Parse date using base adapter's parser
        meeting_date = self._parse_date(date_str)
        if not meeting_date:
            logger.warning("meeting invalid date", vendor="chicago", slug=self.slug, meeting_id=meeting_id, date_str=date_str)
            return None

        # Fetch full meeting detail to get agenda structure
        meeting_detail = await self._fetch_meeting_detail(meeting_id)
        if not meeting_detail:
            logger.warning("could not fetch meeting detail", vendor="chicago", slug=self.slug, meeting_id=meeting_id)
            return None

        # Extract agenda items from meeting detail
        items = await self._extract_agenda_items(meeting_detail)

        # Get agenda file URL (prefer 'Agenda' type, fallback to first file)
        files = meeting_detail.get("files", [])
        agenda_url = next(
            (f.get("path") for f in files if f.get("attachmentType") == "Agenda"),
            files[0].get("path") if files else None
        )

        result = {
            "vendor_id": str(meeting_id),
            "title": body or "City Council Meeting",
            "start": meeting_date.isoformat() if meeting_date else None,
        }

        if location:
            result["location"] = location

        # Extract meeting status from API status field
        api_status = (meeting_detail.get("status") or "").lower()
        status_map = {
            "canceled": "cancelled",
            "cancelled": "cancelled",
            "postponed": "postponed",
            "draft": None,
            "final": None,
        }
        meeting_status = status_map.get(api_status)

        # Fallback: Check title for status keywords
        if not meeting_status:
            meeting_status = self._parse_meeting_status(body, date_str)

        if meeting_status:
            result["meeting_status"] = meeting_status

        # Architecture: items extracted -> agenda_url, no items -> packet_url
        if items:
            if agenda_url:
                result["agenda_url"] = agenda_url
            result["items"] = items
            logger.info("meeting items extracted", vendor="chicago", slug=self.slug, meeting_id=meeting_id, item_count=len(items))
        elif agenda_url:
            result["packet_url"] = agenda_url
            logger.info("meeting fallback to packet url", vendor="chicago", slug=self.slug, meeting_id=meeting_id)
        else:
            logger.debug("meeting no data skipping", vendor="chicago", slug=self.slug, meeting_id=meeting_id)
            return None

        # Extract public comment deadline for civic engagement
        public_comment_deadline = meeting_detail.get("publicCommentDeadline")
        comment_text = meeting_detail.get("comment", "")
        if public_comment_deadline or comment_text:
            participation = {}
            if public_comment_deadline:
                participation["public_comment_deadline"] = public_comment_deadline
            if comment_text:
                participation["instructions"] = comment_text
            result["participation"] = participation

        # Build metadata with video/transcript links and body info
        metadata = {}
        video_links = meeting_detail.get("videoLink", [])
        transcript_links = meeting_detail.get("transcriptLink", [])
        if video_links:
            metadata["video_links"] = video_links
        if transcript_links:
            metadata["transcript_links"] = transcript_links

        # Add committee/body identifiers for tracking
        body_id = meeting_detail.get("bodyId")
        body_abbreviation = meeting_detail.get("bodyAbbreviation")
        if body_id:
            metadata["body_id"] = str(body_id)
        if body_abbreviation:
            metadata["body_abbreviation"] = body_abbreviation

        # Add related meetings for longitudinal tracking
        related_meetings = meeting_detail.get("relatedMeetings", [])
        if related_meetings:
            metadata["related_meetings"] = [
                str(rm.get("meetingId") if isinstance(rm, dict) else rm)
                for rm in related_meetings
            ]

        if metadata:
            result["metadata"] = metadata

        return result

    async def _fetch_meeting_detail(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Fetch full meeting detail with agenda.groups[].items[] structure."""
        detail_url = f"{self.base_url}/meeting-agenda/{meeting_id}"
        logger.debug("fetching meeting detail", vendor="chicago", slug=self.slug, detail_url=detail_url)

        try:
            response = await self._get(detail_url)
            self._sync_stats["api_requests"] += 1
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("network error fetching meeting detail", vendor="chicago", slug=self.slug, meeting_id=meeting_id, error=str(e))
            return None

        try:
            # content_type=None: Chicago API sometimes returns text/plain with JSON body
            return await response.json(content_type=None)
        except ValueError as e:
            logger.warning("invalid json in meeting detail", vendor="chicago", slug=self.slug, meeting_id=meeting_id, error=str(e))
            return None

    async def _extract_agenda_items(self, meeting_detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract agenda items; fetches matter data concurrently for items with matterId."""
        items = []
        item_counter = 0

        # Get meeting ID for vote fetching
        meeting_id = meeting_detail.get("meetingId")

        # Get agenda structure
        agenda = meeting_detail.get("agenda", {})
        groups = agenda.get("groups", [])

        if not groups:
            # Fallback: Extract items from agenda PDF
            pdf_items = await self._extract_items_from_pdf(meeting_detail)
            if pdf_items:
                logger.info(
                    "items extracted from pdf",
                    vendor="chicago",
                    slug=self.slug,
                    meeting_id=meeting_id,
                    item_count=len(pdf_items)
                )
                return pdf_items
            logger.debug("no agenda groups and pdf extraction failed", vendor="chicago", slug=self.slug)
            return items

        # Collect items that need matter data
        items_needing_matter = []
        preliminary_items = []

        for group in groups:
            group_title = group.get("title", "")
            group_items = group.get("items", [])

            for item in group_items:
                # Extract item data
                matter_id = item.get("matterId")
                comment_id = item.get("commentId")
                title = (item.get("matterTitle") or "").strip()
                sequence = int(item.get("sort") or 0)
                record_number = item.get("recordNumber")
                matter_type = item.get("matterType")
                action_name = item.get("actionName")
                display_id = item.get("displayId")
                has_votes = item.get("hasVotes", False)
                vote_type = item.get("voteType")

                # Use matterId or commentId as item_id
                item_id = matter_id or comment_id
                if not item_id:
                    logger.debug("item missing id skipping", vendor="chicago", slug=self.slug, title_prefix=title[:60])
                    continue

                # Increment counter for non-filtered items
                item_counter += 1

                # Fallback: Use manual counter if API's sort field is 0 or missing
                if sequence == 0:
                    sequence = item_counter

                # Fallback: Generate displayId if missing
                if not display_id:
                    display_id = str(item_counter)

                preliminary_items.append({
                    "vendor_item_id": str(item_id),  # Raw vendor ID
                    "title": title,
                    "sequence": sequence,
                    "matter_id": matter_id,
                    "record_number": record_number,
                    "matter_type": matter_type,
                    "action_name": action_name,
                    "display_id": display_id,
                    "group_title": group_title,
                    "has_votes": has_votes,
                    "vote_type": vote_type,
                })

                if matter_id:
                    items_needing_matter.append(matter_id)

        # Fetch matter data concurrently
        matter_data_map = {}
        if items_needing_matter:
            matter_results = await asyncio.gather(
                *[self._fetch_matter_data(mid) for mid in items_needing_matter],
                return_exceptions=True
            )
            for mid, result in zip(items_needing_matter, matter_results):
                if isinstance(result, Exception):
                    logger.debug("matter fetch failed", vendor="chicago", slug=self.slug, matter_id=mid, error=str(result))
                    matter_data_map[mid] = self._EMPTY_MATTER
                else:
                    matter_data_map[mid] = result

        # Build final items - votes now come from matter data (no separate API calls)
        for pitem in preliminary_items:
            matter_id = pitem["matter_id"]
            matter_data = matter_data_map.get(matter_id, self._EMPTY_MATTER) if matter_id else self._EMPTY_MATTER

            item_data = {
                "vendor_item_id": pitem["vendor_item_id"],  # Orchestrator generates final item_id
                "title": pitem["title"],
                "sequence": pitem["sequence"],
                "attachments": matter_data["attachments"],
            }

            # Add optional fields (following AgendaItem schema)
            if matter_id:
                item_data["matter_id"] = str(matter_id)
            if pitem["record_number"]:
                item_data["matter_file"] = pitem["record_number"]
            if pitem["matter_type"]:
                item_data["matter_type"] = pitem["matter_type"]
            if pitem["display_id"]:
                item_data["agenda_number"] = pitem["display_id"]
            if matter_data["sponsors"]:
                item_data["sponsors"] = matter_data["sponsors"]

            # Build metadata (Chicago-specific fields + matter status for closed-loop)
            metadata = {}
            if pitem["action_name"]:
                metadata["action_name"] = pitem["action_name"]
            if pitem["group_title"]:
                metadata["section"] = pitem["group_title"]
            if pitem.get("vote_type"):
                metadata["vote_type"] = pitem["vote_type"]
            if matter_data.get("matter_status"):
                metadata["matter_status"] = matter_data["matter_status"]
            if matter_data.get("vote_outcome"):
                metadata["vote_outcome"] = matter_data["vote_outcome"]

            if metadata:
                item_data["metadata"] = metadata

            # Attach votes from matter data (embedded in actions[].votes[])
            if matter_data.get("votes"):
                item_data["votes"] = matter_data["votes"]

            items.append(item_data)

        logger.debug("extracted items", vendor="chicago", slug=self.slug, item_count=len(items))
        return items

    async def _extract_items_from_pdf(self, meeting_detail: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Fallback: Extract items from agenda PDF when API agenda.groups is empty.

        Strategy:
        1. Find Agenda PDF in meeting files
        2. Extract text from PDF
        3. Parse record numbers (e.g., O2025-0019668)
        4. Fetch matter data for each via /matter/recordNumber/
        5. Build items with title, sponsors, attachments
        """
        meeting_id = meeting_detail.get("meetingId")
        files = meeting_detail.get("files", [])

        # Find Agenda file (prefer "Agenda" type, fallback to first PDF)
        agenda_file = next(
            (f for f in files if f.get("attachmentType") == "Agenda"),
            next((f for f in files if (f.get("path") or "").lower().endswith(".pdf")), None)
        )

        if not agenda_file:
            logger.debug("no agenda pdf found", vendor="chicago", slug=self.slug, meeting_id=meeting_id)
            return []

        pdf_url = agenda_file.get("path")
        if not pdf_url:
            return []

        # Extract PDF text in thread (sync operation)
        try:
            pdf_result = await asyncio.to_thread(
                self.pdf_extractor.extract_from_url,
                pdf_url,
                False  # extract_links not needed
            )
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("pdf extraction failed", vendor="chicago", slug=self.slug, meeting_id=meeting_id, error=str(e))
            return []

        if not pdf_result.get("success") or not pdf_result.get("text"):
            logger.debug("pdf extraction empty", vendor="chicago", slug=self.slug, meeting_id=meeting_id)
            return []

        # Parse record numbers from PDF text
        parsed = parse_chicago_agenda_pdf(pdf_result["text"])
        pdf_items = parsed.get("items", [])

        if not pdf_items:
            logger.debug("no record numbers found in pdf", vendor="chicago", slug=self.slug, meeting_id=meeting_id)
            return []

        logger.debug(
            "record numbers extracted from pdf",
            vendor="chicago",
            slug=self.slug,
            meeting_id=meeting_id,
            record_count=len(pdf_items)
        )

        # Fetch matter data for each record number concurrently
        record_numbers = [item["record_number"] for item in pdf_items]
        matter_results = await asyncio.gather(
            *[self._fetch_matter_by_record_number(rn) for rn in record_numbers],
            return_exceptions=True
        )

        # Build items from matter data
        items = []
        for pdf_item, matter_result in zip(pdf_items, matter_results):
            if isinstance(matter_result, Exception) or not matter_result:
                logger.debug(
                    "matter fetch failed for record",
                    vendor="chicago",
                    slug=self.slug,
                    record_number=pdf_item["record_number"]
                )
                continue

            matter_data = matter_result

            # Build item with all key identifiers:
            # - matter_file: public record number (O2025-0019668)
            # - matter_id: backend UUID from Chicago API
            # - agenda_number: item position within this meeting (1, 2, 3...)
            item_data = {
                "vendor_item_id": matter_data.get("matter_id") or pdf_item["record_number"],  # Orchestrator generates final item_id
                "title": matter_data.get("title") or pdf_item.get("title_hint", ""),
                "sequence": pdf_item["sequence"],
                "agenda_number": str(pdf_item["sequence"]),  # Position within meeting
                "matter_file": pdf_item["record_number"],  # Public file number
                "attachments": matter_data.get("attachments", []),
            }

            if matter_data.get("matter_id"):
                item_data["matter_id"] = matter_data["matter_id"]  # Backend UUID

            if matter_data.get("sponsors"):
                item_data["sponsors"] = matter_data["sponsors"]

            if matter_data.get("matter_type"):
                item_data["matter_type"] = matter_data["matter_type"]

            # Build metadata
            metadata = {}
            if matter_data.get("matter_status"):
                metadata["matter_status"] = matter_data["matter_status"]
            if matter_data.get("vote_outcome"):
                metadata["vote_outcome"] = matter_data["vote_outcome"]
            if matter_data.get("controlling_body"):
                metadata["controlling_body"] = matter_data["controlling_body"]

            if metadata:
                item_data["metadata"] = metadata

            items.append(item_data)

        return items

    async def _fetch_matter_by_record_number(self, record_number: str) -> Optional[Dict[str, Any]]:
        """Fetch full matter data by record number (e.g., O2025-0019668)."""
        matter_url = f"{self.base_url}/matter/recordNumber/{record_number}"

        try:
            response = await self._get(matter_url)
            self._sync_stats["api_requests"] += 1
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("network error fetching matter by record", vendor="chicago", slug=self.slug, record_number=record_number, error=str(e))
            return None

        try:
            matter_data = await response.json(content_type=None)
        except ValueError as e:
            logger.debug("invalid json for matter record", vendor="chicago", slug=self.slug, record_number=record_number, error=str(e))
            return None

        # Check for API error responses
        if not matter_data or matter_data.get("error"):
            return None

        # Extract and normalize matter data
        matter_id = matter_data.get("matterId")
        title = matter_data.get("title", "")
        matter_type = matter_data.get("type")
        matter_status = matter_data.get("status") or None
        controlling_body = matter_data.get("controllingBody")

        # Map status to vote outcome
        status_lower = matter_status.lower() if matter_status else ""
        vote_outcome = self._STATUS_TO_OUTCOME.get(status_lower)

        # Extract attachments
        attachments = self._extract_attachments(matter_data.get("attachments", []))

        # Extract sponsors
        raw_sponsors = matter_data.get("sponsors", [])
        sponsors = [s.get("sponsorName") for s in raw_sponsors if s.get("sponsorName")]

        return {
            "matter_id": str(matter_id) if matter_id else None,
            "title": title,
            "matter_type": matter_type,
            "matter_status": matter_status,
            "vote_outcome": vote_outcome,
            "controlling_body": controlling_body,
            "attachments": attachments,
            "sponsors": sponsors,
        }

    async def _fetch_matter_data(self, matter_id: str) -> Dict[str, Any]:
        """Fetch matter attachments, sponsors, and status. Returns _EMPTY_MATTER on failure."""
        matter_url = f"{self.base_url}/matter/{matter_id}"

        try:
            response = await self._get(matter_url)
            self._sync_stats["api_requests"] += 1
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.debug("network error fetching matter", vendor="chicago", slug=self.slug, matter_id=matter_id, error=str(e))
            return self._EMPTY_MATTER

        try:
            # content_type=None: Chicago API sometimes returns text/plain with JSON body
            matter_data = await response.json(content_type=None)
        except ValueError as e:
            logger.debug("invalid json in matter", vendor="chicago", slug=self.slug, matter_id=matter_id, error=str(e))
            return self._EMPTY_MATTER

        # Extract attachments
        attachments = self._extract_attachments(matter_data.get("attachments", []))

        # Extract sponsors
        sponsors = [s.get("sponsorName") for s in matter_data.get("sponsors", []) if s.get("sponsorName")]

        # Extract matter status for closed-loop tracking (convert empty string to None)
        matter_status = matter_data.get("status") or None

        # Map status to vote outcome
        status_lower = matter_status.lower() if matter_status else ""
        vote_outcome = self._STATUS_TO_OUTCOME.get(status_lower)

        # Extract votes from actions - eliminates need for separate vote API calls
        votes = []
        raw_actions = matter_data.get("actions", [])
        for action in raw_actions:
            action_votes = action.get("votes", [])
            for idx, vote_record in enumerate(action_votes, 1):
                voter_name = vote_record.get("voterName")
                vote_value = vote_record.get("vote")
                if not voter_name or not vote_value:
                    continue
                votes.append({
                    "name": voter_name,
                    "vote": self._normalize_vote_value(vote_value),
                    "sequence": idx,
                    "metadata": {
                        "person_id": str(vote_record.get("personId")) if vote_record.get("personId") else None,
                        "action_name": action.get("actionName"),
                        "meeting_id": action.get("meetingId"),
                    }
                })

        logger.debug(
            "matter data fetched",
            vendor="chicago",
            slug=self.slug,
            matter_id=matter_id,
            attachment_count=len(attachments),
            sponsor_count=len(sponsors),
            matter_status=matter_status,
            vote_outcome=vote_outcome,
            vote_count=len(votes)
        )
        return {
            "attachments": attachments,
            "sponsors": sponsors,
            "matter_status": matter_status,
            "vote_outcome": vote_outcome,
            "votes": votes,
        }

