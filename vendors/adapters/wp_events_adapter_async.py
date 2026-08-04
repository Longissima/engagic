"""
Async WordPress Events Adapter - WP REST API + media attachments for bespoke
WordPress municipal sites using a custom `events` post type.

Cities using this pattern: Sebastopol CA, and other WordPress-based municipal
sites where meetings are stored as an `events` CPT and documents are uploaded
as standard WordPress media attachments parented to the event post.

Filename-based classification turns media attachments into structured items:
- "Agenda-Item-Number-{N}-{description}.pdf" -> agenda item with sequence N
- "Resolution_Number_{N}_{year}_{description}.pdf" -> resolution attachment
- "Public-Comment..." or "_Redacted" suffix -> public comment
- "{date}-City-Council-Meeting-Agenda.pdf" -> main agenda PDF

Same date caveat as ProudCity: the WP `date` field is publication time, not
meeting time. Meeting date is extracted from the title.

Domain auto-discovery: probes common patterns against /wp-json/wp/v2/{cpt}.
Optional per-site config in data/wp_events_sites.json for domain and CPT
slug overrides.
"""

import asyncio
import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from exceptions import VendorHTTPError
from pipeline.protocols import MetricsCollector


WP_EVENTS_CONFIG_FILE = "data/wp_events_sites.json"

# Filename patterns for document classification. Checked in order; first match wins.
# Each tuple: (pattern, doc_type, group_name_for_extraction)
_DOC_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Agenda items: "Agenda-Item-Number-3-PSA-Well-4-..."
    (re.compile(r"Agenda[_-]Item[_-]Number[_-](\d+)", re.IGNORECASE), "agenda_item"),
    # Resolutions: "Resolution_Number_6738_2026_..."
    (re.compile(r"Resolution[_-]Number[_-](\d+)[_-](\d{4})", re.IGNORECASE), "resolution"),
    # Main meeting agenda PDF (not an individual item)
    (re.compile(r"(?:FINAL[_-])?.*(?:Council|Meeting|Commission)[_-].*Agenda(?:[_-]|\.)", re.IGNORECASE), "agenda"),
    # Approved minutes
    (re.compile(r"(?:Approved|Draft|DRAFT)[_-].*Minutes", re.IGNORECASE), "minutes"),
    # Public comments: "_Redacted" suffix or "Public-Comment" prefix
    (re.compile(r"(?:_Redacted|Public[_-]Comment)", re.IGNORECASE), "public_comment"),
    # Staff/activity reports
    (re.compile(r"Monthly[_-](?:Activity[_-]Report|Update)", re.IGNORECASE), "staff_report"),
    # City manager reports
    (re.compile(r"(?:Interim[_-])?City[_-]Manager[_-]Report", re.IGNORECASE), "staff_report"),
    # Proclamations
    (re.compile(r"Proclamation", re.IGNORECASE), "proclamation"),
]

# Meeting date in event titles: "Month Day, Year" or "Month Dayth, Year"
_TITLE_DATE_RE = re.compile(
    r'(?:January|February|March|April|May|June|July|August|'
    r'September|October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}'
)


def _load_wp_events_config() -> Dict[str, Any]:
    return AsyncBaseAdapter._load_vendor_config(WP_EVENTS_CONFIG_FILE)


def _slug_to_title(slug: str) -> str:
    """Convert a WP media slug to a human-readable title.

    'agenda-item-number-3-approval-of-services' -> 'Approval Of Services'
    """
    # Strip common prefixes
    cleaned = re.sub(
        r'^(?:agenda-item-number-\d+-|resolution-number-\d+-\d{4}-)', '', slug
    )
    return cleaned.replace("-", " ").strip().title()


class AsyncWPEventsAdapter(AsyncBaseAdapter):
    """Async adapter for WordPress sites using a custom events post type.

    Slug is a clean identifier (e.g. 'cityofsebastopol'). Domain is
    auto-discovered or overridden via data/wp_events_sites.json.
    """

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="wp_events", metrics=metrics)

        site_config = _load_wp_events_config().get(self.slug, {})

        domain_override = site_config.get("domain")
        if domain_override:
            self.base_url: Optional[str] = f"https://{domain_override}"
        else:
            self.base_url = None

        # CPT slug: most sites use 'events', some register 'meetings' etc.
        self.cpt_slug = site_config.get("cpt_slug", "events")

    async def _discover_base_url(self) -> Optional[str]:
        async def _validate(response):
            data = await response.json()
            return isinstance(data, list)

        return await super()._discover_base_url(
            probe_path=f"/wp-json/wp/v2/{self.cpt_slug}?per_page=1",
            validate=_validate,
        )

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 14
    ) -> List[Dict[str, Any]]:
        """Fetch events via WP REST API, then enrich with media attachments."""
        if not self.base_url:
            self.base_url = await self._discover_base_url()
        if not self.base_url:
            return []

        start_date, end_date = self._date_range(days_back, days_forward)

        events = await self._fetch_events(start_date, end_date)

        logger.info(
            "wp_events retrieved",
            slug=self.slug,
            count=len(events),
            start_date=str(start_date.date()),
            end_date=str(end_date.date()),
        )

        # Enrich each event with media attachments (concurrent)
        tasks = [self._enrich_event(e) for e in events]
        enriched = await asyncio.gather(*tasks, return_exceptions=True)

        results = []
        for idx, meeting in enumerate(enriched):
            if isinstance(meeting, Exception):
                logger.warning(
                    "event enrichment failed",
                    vendor="wp_events",
                    slug=self.slug,
                    error=str(meeting),
                    event_index=idx,
                )
            elif isinstance(meeting, dict):
                results.append(meeting)

        return results

    # ------------------------------------------------------------------
    # Event listing with WP pagination
    # ------------------------------------------------------------------

    async def _fetch_events(
        self, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch events with pagination. Filters by meeting date from title.

        Some cities bulk-create events months in advance (publication date
        in Sept for April meetings). Pagination cutoff uses meeting dates
        from titles, not publication dates, so pre-created events are found.
        """
        all_events: List[Dict[str, Any]] = []
        page = 1
        per_page = 100
        max_pages = 20  # Safety cap to prevent unbounded pagination
        api_url = f"{self.base_url}/wp-json/wp/v2"

        while True:
            url = f"{api_url}/{self.cpt_slug}"
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
                    logger.error(
                        "events endpoint unreachable",
                        vendor="wp_events",
                        slug=self.slug,
                        error=str(e),
                    )
                    return []
                break

            try:
                data = await response.json()
            except ValueError:
                break

            if not data:
                break

            all_events.extend(data)

            # Stop paginating when the most recent page's events all have
            # meeting dates before our window. Parse meeting dates from
            # titles (not publication dates) since some cities bulk-create
            # events far in advance.
            page_meeting_dates = []
            for event in data:
                title = self._extract_rendered_text(event.get("title", {}))
                md = self._extract_date_from_title(title)
                parsed = self._parse_date(md) if md else None
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
                    vendor="wp_events",
                    slug=self.slug,
                    max_pages=max_pages,
                    events_so_far=len(all_events),
                )
                break

            page += 1

        # Filter by MEETING date (from title), not publication date
        filtered = []
        for event in all_events:
            title = self._extract_rendered_text(event.get("title", {}))
            meeting_date = self._extract_date_from_title(title)
            m_date = self._parse_date(meeting_date) if meeting_date else None
            if m_date is None:
                filtered.append(event)
            elif start_date <= m_date <= end_date:
                filtered.append(event)

        return filtered

    # ------------------------------------------------------------------
    # Per-event enrichment
    # ------------------------------------------------------------------

    async def _enrich_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Convert WP event to meeting dict, fetch and classify media."""
        event_id = event.get("id", 0)
        title = self._extract_rendered_text(event.get("title", {}))

        # Meeting date from title, fallback to post date
        meeting_date = self._extract_date_from_title(title) or event.get("date", "")
        meeting_url = event.get("link", "")
        meeting_status = self._parse_meeting_status(title, meeting_date)

        # Body name from tax_additional (no separate API call needed)
        body = self._extract_body_from_tax(event.get("tax_additional", {}))

        result: Dict[str, Any] = {
            "vendor_id": str(event_id),
            "title": title,
            "start": meeting_date,
        }

        if meeting_status:
            result["meeting_status"] = meeting_status

        meta: Dict[str, Any] = {}
        if meeting_url:
            meta["meeting_url"] = meeting_url
        if body:
            meta["body"] = body

        # Fetch and classify media attachments
        media = await self._fetch_media(event_id)

        if media:
            classified = self._classify_media(media)

            # Agenda item PDFs -> structured items with attachments
            if classified.get("agenda_item"):
                result["items"] = classified["agenda_item"]

            # Main agenda PDF
            if classified.get("agenda"):
                result["agenda_url"] = classified["agenda"][0]["url"]

            # Approved/draft minutes PDF
            if classified.get("minutes"):
                result["minutes_url"] = classified["minutes"][0]["url"]

            # No structured items from media -- try agenda (url) then packet (toc)
            if not classified.get("agenda_item") and classified.get("agenda"):
                chunked = await self._chunk_agenda_then_packet(
                    agenda_url=classified["agenda"][0]["url"],
                    packet_url=None,
                    vendor_id=str(event_id),
                )
                if chunked:
                    result["items"] = chunked

            # Non-item docs go into metadata
            for doc_type in ("resolution", "public_comment", "staff_report", "proclamation", "other"):
                if classified.get(doc_type):
                    meta[f"{doc_type}s"] = [
                        {"name": d["name"], "url": d["url"]}
                        for d in classified[doc_type]
                    ]

        if meta:
            result["metadata"] = meta

        return result

    # ------------------------------------------------------------------
    # Media fetching and classification
    # ------------------------------------------------------------------

    async def _fetch_media(self, event_id: int) -> List[Dict[str, Any]]:
        """Fetch all media items parented to an event post."""
        all_media: List[Dict[str, Any]] = []
        page = 1
        max_pages = 20  # Safety cap to prevent unbounded pagination
        api_url = f"{self.base_url}/wp-json/wp/v2"

        while True:
            url = f"{api_url}/media"
            params = {
                "parent": str(event_id),
                "per_page": "100",
                "page": str(page),
            }

            try:
                response = await self._get(url, params=params)
            except VendorHTTPError as e:
                logger.debug(
                    "failed to fetch media",
                    vendor="wp_events",
                    slug=self.slug,
                    event_id=event_id,
                    error=str(e),
                )
                break

            try:
                data = await response.json()
            except ValueError:
                break

            if not data:
                break

            all_media.extend(data)

            try:
                total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
            except (ValueError, TypeError):
                total_pages = 1
            if page >= total_pages:
                break

            if page >= max_pages:
                logger.warning(
                    "media pagination cap reached",
                    vendor="wp_events",
                    slug=self.slug,
                    event_id=event_id,
                    max_pages=max_pages,
                    media_so_far=len(all_media),
                )
                break

            page += 1

        return all_media

    def _classify_media(
        self, media_items: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Classify media attachments by filename pattern.

        Agenda items are shaped as proper AgendaItemSchema dicts with
        the PDF as a nested attachment. Other doc types are flat lists.
        """
        classified: Dict[str, List[Dict[str, Any]]] = {}

        for media in media_items:
            source_url = media.get("source_url", "")
            if not source_url:
                continue

            slug = media.get("slug", "")
            mime_type = media.get("mime_type", "")
            media_id = media.get("id")
            filename = source_url.rsplit("/", 1)[-1]
            match_text = slug or filename

            # Classify
            doc_type = "other"
            item_number = None
            resolution_number = None
            resolution_year = None

            for pattern, dtype in _DOC_PATTERNS:
                m = pattern.search(match_text)
                if m:
                    doc_type = dtype
                    if dtype == "agenda_item" and m.lastindex and m.lastindex >= 1:
                        item_number = m.group(1)
                    elif dtype == "resolution" and m.lastindex and m.lastindex >= 2:
                        resolution_number = m.group(1)
                        resolution_year = m.group(2)
                    break

            file_type = self._mime_to_type(mime_type)
            name = _slug_to_title(slug) if slug else filename

            if doc_type == "agenda_item" and item_number:
                # Build proper AgendaItemSchema dict
                item: Dict[str, Any] = {
                    "vendor_item_id": str(media_id),
                    "title": name,
                    "sequence": int(item_number),
                    "agenda_number": item_number,
                    "attachments": [{
                        "name": name,
                        "url": source_url,
                        "type": file_type,
                    }],
                }
                classified.setdefault("agenda_item", []).append(item)
            elif doc_type == "resolution" and resolution_number:
                entry: Dict[str, Any] = {
                    "name": name,
                    "url": source_url,
                    "type": file_type,
                    "matter_file": f"RES-{resolution_number}-{resolution_year}",
                }
                classified.setdefault("resolution", []).append(entry)
            else:
                classified.setdefault(doc_type, []).append({
                    "name": name,
                    "url": source_url,
                    "type": file_type,
                })

        # Sort agenda items by sequence
        if "agenda_item" in classified:
            classified["agenda_item"].sort(key=lambda x: x.get("sequence", 0))

        return classified

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
        """Extract meeting date from event title as ISO string.

        Handles:
        - 'City Council Regular Meeting May 3, 2022'
        - 'Special Meeting; Closed Session -- Tuesday April 14th, 2026 6:00 pm'
        - 'Planning Commission March 17, 2026'
        """
        match = _TITLE_DATE_RE.search(title)
        if match:
            date_text = match.group(0)
            # Strip ordinal suffixes: 14th -> 14, 1st -> 1
            date_text = re.sub(r'(\d+)(?:st|nd|rd|th)', r'\1', date_text)
            date_text = date_text.replace(",", "")
            try:
                dt = datetime.strptime(date_text, "%B %d %Y")
                return dt.isoformat()
            except ValueError:
                pass
        return None

    def _extract_body_from_tax(self, tax_additional: Dict[str, Any]) -> Optional[str]:
        """Extract meeting body name from tax_additional field.

        The tax_additional.meeting_event_type.unlinked array has entries like:
        '<span class="advgb-post-tax-term">City Council</span>'
        Filter out the generic "Meetings" and "Events" tags.
        """
        met = tax_additional.get("meeting_event_type", {})
        unlinked = met.get("unlinked", [])
        generic = {"meetings", "events", "other"}

        for html_span in unlinked:
            # Extract text from <span>...</span>
            name_match = re.search(r">([^<]+)<", html_span)
            if name_match:
                name = name_match.group(1).strip()
                if name.lower() not in generic:
                    return name
        return None

    def _mime_to_type(self, mime_type: str) -> str:
        """Map MIME type to simple file type string."""
        if "pdf" in mime_type:
            return "pdf"
        if "word" in mime_type or "document" in mime_type:
            return "doc"
        if "image" in mime_type:
            return "image"
        if "video" in mime_type:
            return "video"
        return "unknown"

