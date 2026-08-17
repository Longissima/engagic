"""
Async Granicus Adapter - Concurrent HTML scraping for Granicus platform

Two-step fetching:
1. ViewPublisher.php - List of meetings
2. AgendaViewer.php -> follows redirect -> actual agenda HTML

Supports multiple HTML formats:
- AgendaOnline (meetings.{city}.org) - parsed with parse_agendaonline_html
- Original Granicus format - parsed with parse_agendaviewer_html
- S3/CloudFront-hosted grid HTML (e.g. Bozeman) - parsed with parse_granicus_s3_html

Cities using Granicus: Cambridge MA, Santa Monica CA, Redwood City CA, Bozeman MT, and many others
"""

import json
import os
import re
import asyncio
import tempfile
from copy import deepcopy
from difflib import SequenceMatcher
from datetime import datetime
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin, urlparse, unquote, parse_qs

from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.parsers.granicus_parser import (
    parse_viewpublisher_listing,
    parse_granicus_html,
)
from pipeline.document_artifacts import DocumentFormat
from pipeline.protocols import MetricsCollector


def _ensure_attachment_portal_urls(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Set attachments[].portal_url = url when missing.

    Granicus PDF link annotations are author-provided URIs (legistar-download
    wrappers, cloudfront staff reports, unsigned S3). PyMuPDF extracts the raw
    annotation URI verbatim — no redirect following — so these are already
    durable. Mirror to portal_url for UI consistency with the CivicClerk schema.
    """
    for item in items:
        for att in item.get("attachments") or []:
            url = att.get("url")
            if url and not att.get("portal_url"):
                att["portal_url"] = url
    return items


def _normalized_item_text(value: Any) -> str:
    """Normalize human labels for conservative cross-document matching."""
    folded = str(value or "").casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in folded).split()
    )


def _item_section(item: Dict[str, Any]) -> str:
    metadata = item.get("metadata") or {}
    return _normalized_item_text(
        item.get("section")
        or metadata.get("section")
        or metadata.get("section_path")
    )


def _item_number_keys(item: Dict[str, Any]) -> set[str]:
    """Return only explicit/leading agenda identifiers, never arbitrary numbers."""
    keys: set[str] = set()
    for raw in (item.get("vendor_item_id"), item.get("agenda_number")):
        normalized = re.sub(r"[^a-z0-9]+", "", str(raw or "").casefold())
        if normalized:
            keys.add(normalized)

    title = str(item.get("title") or "").strip()
    leading = re.match(
        r"^(?:item\s+)?((?:[a-z]\d*|\d+)(?:[.\-][a-z0-9]+)*)(?:\s*[-.:)]|\s+)",
        title,
        flags=re.IGNORECASE,
    )
    if leading:
        normalized = re.sub(r"[^a-z0-9]+", "", leading.group(1).casefold())
        if normalized:
            keys.add(normalized)
    return keys


def _packet_match_index(
    packet_item: Dict[str, Any],
    agenda_items: List[Dict[str, Any]],
    agenda_key_counts: Dict[str, int],
) -> Optional[int]:
    """Find one unambiguous agenda owner for a packet-derived evidence entry.

    Packet TOCs frequently contain several entries per real agenda item.  This
    deliberately permits many packet entries to enrich one agenda item, while
    refusing to manufacture agenda shape from ambiguous section-local labels.
    """
    packet_title = _normalized_item_text(packet_item.get("title"))
    packet_section = _item_section(packet_item)
    packet_keys = _item_number_keys(packet_item)

    exact_title = [
        idx
        for idx, agenda_item in enumerate(agenda_items)
        if packet_title
        and packet_title == _normalized_item_text(agenda_item.get("title"))
    ]
    if len(exact_title) == 1:
        return exact_title[0]

    keyed = [
        idx
        for idx, agenda_item in enumerate(agenda_items)
        if packet_keys & _item_number_keys(agenda_item)
    ]
    if len(keyed) == 1:
        # A key is safe only when it is unique in the authoritative agenda.
        shared = packet_keys & _item_number_keys(agenda_items[keyed[0]])
        if any(agenda_key_counts.get(key) == 1 for key in shared):
            return keyed[0]

    if len(keyed) > 1 and packet_section:
        section_matches = [
            idx for idx in keyed if _item_section(agenda_items[idx]) == packet_section
        ]
        if len(section_matches) == 1:
            return section_matches[0]
        if section_matches:
            keyed = section_matches

    # Titles can bridge agendas whose item number is missing from one PDF, but
    # only at a high threshold with a clear winner.  Short generic headings are
    # intentionally excluded.
    if len(packet_title) < 18:
        return None
    candidates = keyed or list(range(len(agenda_items)))
    scored = sorted(
        (
            SequenceMatcher(
                None,
                packet_title,
                _normalized_item_text(agenda_items[idx].get("title")),
                autojunk=False,
            ).ratio(),
            idx,
        )
        for idx in candidates
    )
    if not scored:
        return None
    best_score, best_idx = scored[-1]
    runner_up = scored[-2][0] if len(scored) > 1 else 0.0
    if best_score >= 0.92 and best_score - runner_up >= 0.06:
        return best_idx
    return None


def _attachment_identity(attachment: Dict[str, Any]) -> tuple[str, ...]:
    document_identity = (
        str(attachment.get("url") or "").strip(),
        str(attachment.get("portal_url") or "").strip(),
        str(attachment.get("history_id") or "").strip(),
        str(attachment.get("meta_id") or "").strip(),
    )
    if any(document_identity):
        return ("document", *document_identity)
    return ("label", _normalized_item_text(attachment.get("name")))


def _merge_packet_evidence(
    agenda_items: List[Dict[str, Any]],
    packet_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep agenda-defined shape and merge confidently matched packet evidence.

    An empty agenda has no authoritative shape, so the packet remains the
    fallback.  Otherwise unmatched packet TOC entries (exhibits, clauses,
    cover pages) never become standalone agenda items.
    """
    if not agenda_items:
        return deepcopy(packet_items)
    if not packet_items:
        return deepcopy(agenda_items)

    merged = deepcopy(agenda_items)
    agenda_key_counts: Dict[str, int] = {}
    for agenda_item in agenda_items:
        for key in _item_number_keys(agenda_item):
            agenda_key_counts[key] = agenda_key_counts.get(key, 0) + 1

    for packet_item in packet_items:
        match_idx = _packet_match_index(
            packet_item, agenda_items, agenda_key_counts
        )
        if match_idx is None:
            continue

        target = merged[match_idx]
        existing_attachments = target.get("attachments")
        if not isinstance(existing_attachments, list):
            existing_attachments = []
            target["attachments"] = existing_attachments
        seen_attachments = {
            _attachment_identity(attachment)
            for attachment in existing_attachments
        }
        for attachment in packet_item.get("attachments") or []:
            # Empty URLs are packet-internal page markers, not downloadable
            # attachments.  The meeting-level packet_url remains the durable
            # link to those pages.
            if not str(attachment.get("url") or "").strip():
                continue
            identity = _attachment_identity(attachment)
            if identity in seen_attachments:
                continue
            existing_attachments.append(deepcopy(attachment))
            seen_attachments.add(identity)

        packet_body = str(packet_item.get("body_text") or "").strip()
        agenda_body = str(target.get("body_text") or "").strip()
        if packet_body:
            normalized_packet = _normalized_item_text(packet_body)
            normalized_agenda = _normalized_item_text(agenda_body)
            if not agenda_body:
                target["body_text"] = packet_body
            elif normalized_packet and normalized_packet not in normalized_agenda:
                target["body_text"] = (
                    f"{agenda_body}\n\n[Packet evidence]\n{packet_body}"
                )

        for field in ("matter_id", "matter_file", "matter_type"):
            if not target.get(field) and packet_item.get(field):
                target[field] = packet_item[field]

        packet_metadata = packet_item.get("metadata") or {}
        metadata = target.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            target["metadata"] = metadata
        if packet_metadata.get("page_start") is not None:
            metadata.setdefault("packet_page_start", packet_metadata["page_start"])
        if packet_metadata.get("page_end") is not None:
            metadata.setdefault("packet_page_end", packet_metadata["page_end"])
        if packet_metadata.get("parse_method"):
            metadata.setdefault(
                "packet_parse_method", packet_metadata["parse_method"]
            )
        metadata["packet_evidence_items"] = (
            int(metadata.get("packet_evidence_items") or 0) + 1
        )

    return merged


def _translate_downloadfile_to_viewdocument(url: str) -> str:
    """Translate AgendaOnline DownloadFile URL to ViewDocument URL.

    DownloadFile requires auth dance; ViewDocument serves PDFs directly.
    """
    if "/AgendaOnline/Documents/DownloadFile/" not in url:
        return url

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    params = parse_qs(parsed.query)

    path_parts = parsed.path.split("/DownloadFile/")
    if len(path_parts) < 2:
        return url
    doc_name = path_parts[1]

    return (
        f"{base_url}/AgendaOnline/Documents/ViewDocument/{doc_name}"
        f"?meetingId={params.get('meetingId', [''])[0]}"
        f"&documentType={params.get('documentType', ['1'])[0]}"
        f"&itemId={params.get('itemId', [''])[0]}"
        f"&publishId={params.get('publishId', [''])[0]}"
        f"&isSection={params.get('isSection', ['false'])[0].lower()}"
    )


class AsyncGranicusAdapter(AsyncBaseAdapter):
    """Async adapter for cities using Granicus platform."""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        """city_slug is the Granicus subdomain (e.g., "redwoodcity-ca"). Raises ValueError if view_id not configured."""
        super().__init__(city_slug, vendor="granicus", metrics=metrics)
        self.base_url = f"https://{self.slug}.granicus.com"
        self.view_ids_file = "data/granicus_view_ids.json"

        # Load view_id(s) from static configuration (fail-fast if not configured)
        mappings = self._load_static_view_id_config()
        if self.base_url not in mappings:
            raise ValueError(
                f"view_id not configured for {self.base_url}. "
                f"Add mapping to {self.view_ids_file}"
            )

        raw = mappings[self.base_url]

        # Support both formats:
        #   int:  33  (single view, backward compatible)
        #   list: [{"view_id": 33, "body": "Board of Supervisors"}, ...]
        if isinstance(raw, int):
            self.views: List[Dict[str, Any]] = [{"view_id": raw}]
        elif isinstance(raw, list):
            self.views = raw
        else:
            raise ValueError(f"Invalid view_id config for {self.base_url}: expected int or list")

        if not self.views:
            raise ValueError(
                f"Empty views list for {self.base_url}. "
                f"Config in {self.view_ids_file} must contain at least one view_id."
            )

        # Keep self.view_id for backward compat (first/primary view)
        self.view_id: int = self.views[0]["view_id"]
        self.list_url: str = f"{self.base_url}/ViewPublisher.php?view_id={self.view_id}"

        logger.info(
            "adapter initialized",
            vendor="granicus",
            slug=self.slug,
            view_ids=[v["view_id"] for v in self.views],
        )

    def _load_static_view_id_config(self) -> Dict[str, Any]:
        """Load view_id mappings from data/granicus_view_ids.json."""
        if not os.path.exists(self.view_ids_file):
            raise FileNotFoundError(
                f"Granicus view_id configuration not found: {self.view_ids_file}"
            )

        try:
            with open(self.view_ids_file, "r") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in {self.view_ids_file}: {e}")

    async def _read_text(self, response) -> str:
        """Read response as text with encoding fallback.

        Granicus servers often misreport Content-Type as UTF-8 when actual content
        is ISO-8859-1. Try UTF-8 first, fall back to latin-1 which never fails.
        """
        data = await response.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("latin-1")

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Fetch meetings from all configured view_ids, then fetch detail pages."""
        start_date, end_date = self._date_range(days_back, days_forward)

        # Fetch listings from all views concurrently
        listing_tasks = [self._fetch_view_listing(v) for v in self.views]
        listing_results = await self._bounded_gather(listing_tasks, max_concurrent=5, return_exceptions=True)

        meetings_in_range = []
        for idx, result in enumerate(listing_results):
            if isinstance(result, BaseException):
                logger.warning(
                    "view listing failed",
                    vendor="granicus",
                    slug=self.slug,
                    view_id=self.views[idx].get("view_id"),
                    error=str(result),
                )
                continue
            if not isinstance(result, list) or not result:
                continue
            for meeting_data in result:
                date_str = meeting_data.get("start", "")
                if not date_str:
                    continue
                try:
                    meeting_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    meeting_date = meeting_date.replace(tzinfo=None)
                    if start_date <= meeting_date <= end_date:
                        meetings_in_range.append(meeting_data)
                except (ValueError, AttributeError):
                    logger.debug("skipping meeting with unparseable date", slug=self.slug, date=date_str)

        logger.debug(
            "filtered meetings by date",
            vendor="granicus",
            slug=self.slug,
            views=len(self.views),
            in_range=len(meetings_in_range),
        )

        if not meetings_in_range:
            return []

        if self._minutes_discovery_only:
            return [
                {
                    "vendor_id": meeting["event_id"],
                    "title": meeting.get("title", "Meeting"),
                    "start": meeting["start"],
                    "minutes_url": meeting["minutes_url"],
                }
                for meeting in meetings_in_range
                if meeting.get("event_id") and meeting.get("minutes_url")
            ]

        detail_tasks = [
            self._fetch_meeting_detail(meeting_data)
            for meeting_data in meetings_in_range
        ]

        results = await asyncio.gather(*detail_tasks, return_exceptions=True)

        meetings = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.warning(
                    "failed to fetch meeting detail",
                    vendor="granicus",
                    slug=self.slug,
                    event_id=meetings_in_range[i].get("event_id"),
                    error=str(result)
                )
            elif result is not None:
                # Propagate metadata from listing (body name from multi-view config)
                listing_meta = meetings_in_range[i].get("metadata", {})
                if listing_meta:
                    result.setdefault("metadata", {}).update(listing_meta)
                # Minutes link discovered on the ViewPublisher row (post-meeting)
                minutes_url = meetings_in_range[i].get("minutes_url")
                if minutes_url:
                    result.setdefault("minutes_url", minutes_url)
                meetings.append(result)

        logger.info(
            "meetings fetched",
            vendor="granicus",
            slug=self.slug,
            count=len(meetings),
            with_items=sum(1 for m in meetings if m.get("items"))
        )

        return meetings

    async def _fetch_view_listing(self, view_config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch and parse a single ViewPublisher page. Injects body name into metadata."""
        view_id = view_config["view_id"]
        body = view_config.get("body")
        url = f"{self.base_url}/ViewPublisher.php?view_id={view_id}"

        response = await self._get(url)
        html = await self._read_text(response)
        listing = await asyncio.to_thread(parse_viewpublisher_listing, html, self.base_url)

        if not listing:
            logger.warning("no meetings found in listing", vendor="granicus", slug=self.slug, view_id=view_id)
            return []

        # Tag each meeting with body name from config
        if body:
            for meeting_data in listing:
                meta = meeting_data.setdefault("metadata", {})
                meta["body"] = body

        return listing

    async def _fetch_meeting_detail(self, meeting_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fetch and parse individual meeting agenda, choosing parser based on redirect destination."""
        agenda_viewer_url = meeting_data.get("agenda_viewer_url")
        event_id = meeting_data.get("event_id")

        if not agenda_viewer_url:
            # No AgendaViewer link -- try direct packet PDF if available
            packet_url = meeting_data.get("packet_url")
            if packet_url:
                meeting = {
                    "vendor_id": event_id,
                    "title": meeting_data.get("title", ""),
                    "start": meeting_data.get("start", ""),
                    "packet_url": packet_url,
                }
                items = await self._chunk_agenda_then_packet(packet_url=packet_url, vendor_id=event_id)
                if items:
                    meeting["items"] = items
                return meeting
            logger.debug(
                "no agenda viewer url",
                vendor="granicus",
                slug=self.slug,
                event_id=event_id
            )
            return None

        try:
            response = await self._get(agenda_viewer_url)
            final_url = str(response.url)
            content_type = response.headers.get("Content-Type", "")

            # DocumentViewer.php redirects serve raw PDF — run chunker directly.
            # Force v1 ("url" path): for Granicus URL-anchored agendas, v1 groups
            # items by boldness/formatting cues correctly where v2 misaligns.
            if "application/pdf" in content_type:
                logger.info(
                    "pdf redirect detected",
                    vendor="granicus",
                    slug=self.slug,
                    event_id=event_id,
                    final_url=final_url[:120],
                )
                pdf_items = await self._parse_pdf_response(response, event_id, force_method="url")
                resolved_packet_url = final_url
                # Thin-agenda enrichment: if the agenda PDF produced nothing
                # useful -- either zero items or items with no attachments
                # (motion-boilerplate body_text doesn't count; Winder GA's
                # "Consideration of a Motion to accept..." items have 400-char
                # bodies but no linked documents, while the corresponding
                # packet has 24 TOC entries with real staff reports) -- try
                # the meatier "Agenda Packet" URL captured from ViewPublisher.
                # Force TOC (v2_toc) since Granicus packets are bookmark-anchored.
                agenda_has_attachments = bool(pdf_items) and any(
                    item.get("attachments") for item in pdf_items
                )
                if not agenda_has_attachments:
                    listing_packet = meeting_data.get("packet_url")
                    if listing_packet and listing_packet != final_url:
                        logger.info(
                            "agenda pdf hollow, enriching from listing packet",
                            vendor="granicus",
                            slug=self.slug,
                            event_id=event_id,
                            agenda_item_count=len(pdf_items) if pdf_items else 0,
                            packet_url=listing_packet[:120],
                        )
                        packet_items = await self._parse_packet_pdf(
                            listing_packet, event_id, force_method="toc"
                        )
                        if packet_items:
                            pdf_items = _merge_packet_evidence(
                                pdf_items or [], packet_items
                            )
                            resolved_packet_url = listing_packet
                if pdf_items:
                    pdf_items = _ensure_attachment_portal_urls(pdf_items)
                meeting = {
                    "vendor_id": event_id,
                    "title": meeting_data.get("title", ""),
                    "start": meeting_data.get("start", ""),
                    "agenda_url": agenda_viewer_url,  # durable portal URL
                    "packet_url": resolved_packet_url,
                }
                if pdf_items:
                    meeting["items"] = pdf_items
                return meeting

            # Google Docs viewer wraps PDF in HTML — extract real URL and download
            if "docs.google.com/gview" in final_url:
                real_pdf_url = parse_qs(urlparse(final_url).query).get("url", [None])[0]
                if real_pdf_url:
                    logger.info(
                        "google viewer redirect, fetching actual pdf",
                        vendor="granicus",
                        slug=self.slug,
                        event_id=event_id,
                    )
                    pdf_items = await self._parse_packet_pdf(real_pdf_url, event_id, force_method="url")
                    resolved_packet_url = real_pdf_url
                    # Same thin-agenda enrichment as the direct-PDF branch:
                    # NPR's gview-wrapped agenda PDFs are 2-page thin front
                    # matter. The real meaty packet is a separate cloudfront
                    # URL captured from ViewPublisher and has a clean TOC
                    # labelled "Item 6.a - Cover Page" etc. that v2_toc
                    # handles cleanly.
                    agenda_has_attachments = bool(pdf_items) and any(
                        item.get("attachments") for item in pdf_items
                    )
                    if not agenda_has_attachments:
                        listing_packet = meeting_data.get("packet_url")
                        if listing_packet and listing_packet != real_pdf_url:
                            logger.info(
                                "gview agenda hollow, enriching from listing packet",
                                vendor="granicus",
                                slug=self.slug,
                                event_id=event_id,
                                agenda_item_count=len(pdf_items) if pdf_items else 0,
                                packet_url=listing_packet[:120],
                            )
                            packet_items = await self._parse_packet_pdf(
                                listing_packet, event_id, force_method="toc"
                            )
                            if packet_items:
                                pdf_items = _merge_packet_evidence(
                                    pdf_items or [], packet_items
                                )
                                resolved_packet_url = listing_packet
                    if pdf_items:
                        pdf_items = _ensure_attachment_portal_urls(pdf_items)
                    meeting = {
                        "vendor_id": event_id,
                        "title": meeting_data.get("title", ""),
                        "start": meeting_data.get("start", ""),
                        "agenda_url": agenda_viewer_url,  # durable portal URL
                        "packet_url": resolved_packet_url,
                    }
                    if pdf_items:
                        meeting["items"] = pdf_items
                    return meeting

            html = await self._read_text(response)

            # AgendaOnline ViewMeeting loads items via JS - use accessible view instead
            if "AgendaOnline" in final_url and "/Meetings/ViewMeeting" in final_url:
                if meeting_id_match := re.search(r'[?&]id=(\d+)', final_url):
                    parsed_url = urlparse(final_url)
                    accessible_url = f"{parsed_url.scheme}://{parsed_url.netloc}/AgendaOnline/Meetings/ViewMeetingAgenda?meetingId={meeting_id_match.group(1)}&type=agenda"
                    logger.debug("fetching accessible agenda view", vendor="granicus", slug=self.slug, event_id=event_id)
                    accessible_response = await self._get(accessible_url)
                    html = await self._read_text(accessible_response)
                    final_url = accessible_url

            # Dialect dispatch lives in the parser (parse_granicus_html);
            # the pattern it chose rides the meeting's html audit.
            parsed = await asyncio.to_thread(parse_granicus_html, html, final_url)
            html_pattern = parsed.get("html_pattern")

            items = parsed.get("items", [])

            # Fetch attachments for AgendaOnline items
            if items and "AgendaOnline" in final_url:
                parsed_url = urlparse(final_url)
                base_host = f"{parsed_url.scheme}://{parsed_url.netloc}"
                meeting_id_match = re.search(r'meetingId=(\d+)', final_url)
                if meeting_id_match:
                    agendaonline_meeting_id = meeting_id_match.group(1)
                    items = await self._fetch_agendaonline_attachments(
                        items, agendaonline_meeting_id, base_host
                    )

            # Fetch attachments from S3 staff report PDFs (Bozeman/Carson City style)
            if items and ("s3.amazonaws.com" in final_url or "cloudfront.net" in final_url):
                items = await self._fetch_s3_pdf_attachments(items, event_id)

            # Fetch actual PDF attachments from Questys Documents.htm pages
            if items and ("questys" in final_url or "MsoNormal" in html[:2000]):
                items = await self._fetch_questys_attachments(items, event_id)

            meeting = {
                "vendor_id": event_id,
                "title": meeting_data.get("title", ""),
                "start": meeting_data.get("start", ""),
                # AgendaViewer.php is the durable portal URL regardless of
                # whether the HTML parse, PDF fallback, or packet URL wins.
                "agenda_url": agenda_viewer_url,
            }

            if items:
                items = _ensure_attachment_portal_urls(items)
                meeting["items"] = items
                attachment_count = sum(len(item.get("attachments", [])) for item in items)
                self._record_html_audit(event_id, html_pattern, items)
                logger.debug(
                    "parsed meeting with items",
                    vendor="granicus",
                    slug=self.slug,
                    event_id=event_id,
                    html_pattern=html_pattern,
                    item_count=len(items),
                    attachment_count=attachment_count
                )
            else:
                # No HTML items — try PDF path: agenda PDF first, then packet PDF
                agenda_pdf_url, packet_url = self._find_agenda_and_packet_urls(html, final_url)
                logger.info(
                    "no html items, trying pdf path",
                    vendor="granicus",
                    slug=self.slug,
                    event_id=event_id,
                    has_agenda_pdf=bool(agenda_pdf_url),
                    has_packet_pdf=bool(packet_url),
                )

                pdf_items = None
                used_url = None

                # Agenda PDFs are URL-anchored (hyperlinks). v1 groups boldness-
                # and formatting-marked items correctly here; v2 misaligns.
                if agenda_pdf_url:
                    pdf_items = await self._parse_packet_pdf(agenda_pdf_url, event_id, force_method="url")
                    items_have_content = pdf_items and any(
                        item.get("body_text") or item.get("attachments")
                        for item in pdf_items
                    )
                    if items_have_content:
                        used_url = agenda_pdf_url
                    else:
                        logger.debug(
                            "agenda pdf yielded hollow items, trying packet",
                            vendor="granicus",
                            slug=self.slug,
                            event_id=event_id,
                            agenda_item_count=len(pdf_items) if pdf_items else 0,
                        )
                        pdf_items = None  # Reset — try packet next

                # Packet PDFs are TOC-based; keep auto dispatch (v2-first).
                if not pdf_items and packet_url and packet_url != agenda_pdf_url:
                    pdf_items = await self._parse_packet_pdf(packet_url, event_id)
                    items_have_content = pdf_items and any(
                        item.get("body_text") or item.get("attachments")
                        for item in pdf_items
                    )
                    if items_have_content:
                        used_url = packet_url
                    else:
                        pdf_items = None

                if pdf_items and used_url:
                    pdf_items = _ensure_attachment_portal_urls(pdf_items)
                    meeting["items"] = pdf_items
                    meeting["packet_url"] = used_url
                    parse_method = pdf_items[0].get("metadata", {}).get("parse_method", "")
                    logger.info(
                        "parsed items from pdf",
                        vendor="granicus",
                        slug=self.slug,
                        event_id=event_id,
                        item_count=len(pdf_items),
                        parse_method=parse_method,
                        source="agenda_pdf" if used_url == agenda_pdf_url else "packet_pdf",
                    )
                elif packet_url:
                    # Neither PDF gave usable items — keep packet_url for
                    # monolithic packet-level processing by the processor
                    meeting["packet_url"] = packet_url
                    logger.info(
                        "no items from pdfs, using packet fallback",
                        vendor="granicus",
                        slug=self.slug,
                        event_id=event_id,
                    )
                else:
                    logger.info(
                        "no items or packet found",
                        vendor="granicus",
                        slug=self.slug,
                        event_id=event_id,
                    )

            return meeting

        except Exception as e:
            logger.warning(
                "error fetching meeting detail",
                vendor="granicus",
                slug=self.slug,
                event_id=event_id,
                error=str(e)
            )
            return None

    async def _fetch_agendaonline_attachments(
        self, items: List[Dict[str, Any]], meeting_id: str, base_host: str
    ) -> List[Dict[str, Any]]:
        """Fetch attachments for each item from AgendaOnline item detail pages."""
        async def fetch_item_attachments(item: Dict[str, Any]) -> Dict[str, Any]:
            if not (item_id := item.get("vendor_item_id")):
                return item
            detail_url = f"{base_host}/AgendaOnline/Meetings/ViewMeetingAgendaItem?meetingId={meeting_id}&itemId={item_id}&isSection=false&type=agenda"
            try:
                response = await self._get(detail_url)
                html = await self._read_text(response)
                if attachments := self._parse_agendaonline_attachments(html, base_host):
                    item["attachments"] = attachments
            except Exception as e:
                logger.debug("failed to fetch item attachments", vendor="granicus", slug=self.slug, item_id=item_id, error=str(e))
            return item

        return list(await asyncio.gather(*[fetch_item_attachments(item) for item in items]))

    def _parse_agendaonline_attachments(self, html: str, base_host: str) -> List[Dict[str, Any]]:
        """Parse attachment links from AgendaOnline item detail page.

        Translates DownloadFile URLs to ViewDocument URLs for direct fetching.
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = []
        for link in soup.find_all(
            "a",
            href=lambda value: isinstance(value, str)
            and "DownloadFile" in value
            and "isAttachment=True" in value,
        ):
            href = str(link.get("href") or "")
            name = link.get_text(strip=True)
            full_url = base_host + href if href.startswith("/") else href
            # Translate to ViewDocument URL for direct fetching
            full_url = _translate_downloadfile_to_viewdocument(full_url)
            if not name and (m := re.search(r'/DownloadFile/([^?]+)', href)):
                name = unquote(m.group(1))
            attachments.append({
                "name": name,
                "url": full_url,
                "type": "pdf" if ".pdf" in href.lower() else "unknown",
            })
        return attachments

    async def _fetch_s3_pdf_attachments(
        self, items: List[Dict[str, Any]], event_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch staff report PDFs from S3 items and extract embedded attachment links.

        Each item from the S3 HTML parser may have a CloudFront PDF (the staff report).
        That PDF contains hyperlinked Legistar S3 attachment URLs. We download each
        staff report, extract links with PyMuPDF, and add them as item attachments.
        The staff report itself is kept as the first attachment.
        """
        import fitz

        async def extract_from_pdf(item: Dict[str, Any]) -> Dict[str, Any]:
            # Find the staff report PDF among existing attachments
            pdf_attachments = [
                a for a in item.get("attachments", [])
                if a.get("type") == "pdf" and a.get("url")
            ]
            if not pdf_attachments:
                return item

            staff_report_url = pdf_attachments[0]["url"]
            tmp_path = None
            try:
                artifact = await self._document_acquirer.acquire(
                    staff_report_url,
                    banana=self.banana,
                )
                if artifact.document_format is not DocumentFormat.PDF:
                    return item
                pdf_bytes = artifact.data

                if len(pdf_bytes) < 500:
                    return item

                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                    tmp_path = tmp.name
                    tmp.write(pdf_bytes)
                # PyMuPDF reopens the tempfile; do not retain the parent-side
                # artifact while link extraction runs in its worker thread.
                del artifact
                pdf_bytes = b""

                def _extract_links():
                    doc = fitz.open(tmp_path)
                    links = []
                    seen_urls = set()
                    for page in doc:
                        for link in page.get_links():
                            if link.get("kind") != 2:
                                continue
                            uri = link.get("uri", "")
                            if not uri or uri in seen_urls:
                                continue
                            # Only keep attachment-like URLs (S3, PDFs, etc.)
                            if not any(pat in uri.lower() for pat in [
                                "s3.amazonaws.com", ".pdf", "/uploads/attachment",
                                "/attachments/", "cloudfront.net",
                            ]):
                                continue
                            # Skip if it's the staff report URL itself
                            if uri == staff_report_url:
                                continue
                            seen_urls.add(uri)
                            # Extract display text for the link
                            bbox = link.get("from", fitz.Rect())
                            name = self._get_pdf_link_text(page, fitz.Rect(bbox))
                            links.append({
                                "name": name or "Attachment",
                                "url": uri,
                                "type": "pdf" if ".pdf" in uri.lower() else "unknown",
                            })
                    doc.close()
                    return links

                embedded_attachments = await asyncio.to_thread(_extract_links)

                if embedded_attachments:
                    # Staff report stays as first attachment, embedded links added after
                    item["attachments"] = item["attachments"] + embedded_attachments
                    logger.debug(
                        "extracted attachments from staff report pdf",
                        vendor="granicus",
                        slug=self.slug,
                        item=item.get("agenda_number"),
                        embedded_count=len(embedded_attachments),
                    )

            except Exception as e:
                logger.debug(
                    "failed to extract pdf attachments",
                    vendor="granicus",
                    slug=self.slug,
                    item=item.get("agenda_number"),
                    error=str(e),
                )
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            return item

        return list(await asyncio.gather(*[extract_from_pdf(item) for item in items]))

    async def _fetch_questys_attachments(
        self, items: List[Dict[str, Any]], event_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch actual PDF links from Questys Documents.htm pages.

        Each item has a placeholder attachment pointing at a Documents.htm page.
        That page lists the real PDFs (Staff Report, Resolutions, Exhibits).
        Replace the placeholder with the actual PDF attachments.
        """
        semaphore = asyncio.Semaphore(5)

        async def resolve_item(item: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                html_atts = [
                    a for a in item.get("attachments", [])
                    if a.get("type") == "html" and (a.get("url") or "").endswith("Documents.htm")
                ]
                if not html_atts:
                    return item

                docs_url = html_atts[0]["url"]
                try:
                    response = await self._get(docs_url)
                    html = await self._read_text(response)
                    soup = BeautifulSoup(html, "html.parser")

                    pdf_attachments = []
                    for link in soup.find_all("a", href=True):
                        href = str(link.get("href") or "")
                        name = link.get_text(strip=True)
                        if not href or not name:
                            continue
                        full_url = urljoin(docs_url, href)
                        pdf_attachments.append({
                            "name": name,
                            "url": full_url,
                            "type": "pdf" if href.lower().endswith(".pdf") else "unknown",
                        })

                    if pdf_attachments:
                        item["attachments"] = pdf_attachments
                        logger.debug(
                            "resolved questys attachments",
                            vendor="granicus",
                            slug=self.slug,
                            item=item.get("agenda_number"),
                            count=len(pdf_attachments),
                        )
                except Exception as e:
                    logger.debug(
                        "failed to fetch questys documents page",
                        vendor="granicus",
                        slug=self.slug,
                        item=item.get("agenda_number"),
                        docs_url=docs_url,
                        error=str(e),
                    )
                return item

        return list(await asyncio.gather(*[resolve_item(item) for item in items]))

    @staticmethod
    def _get_pdf_link_text(page, link_rect) -> str:
        """Extract display text for a hyperlink by intersecting span bboxes."""
        import fitz
        td = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        parts = []
        for block in td.get("blocks", []):
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    span_rect = fitz.Rect(span["bbox"])
                    intersection = span_rect & link_rect
                    if intersection.is_empty or intersection.width < 1:
                        continue
                    span_y_center = (span_rect.y0 + span_rect.y1) / 2
                    if link_rect.y0 <= span_y_center <= link_rect.y1:
                        text = span["text"].strip()
                        if text:
                            parts.append(text)
        return " ".join(parts) if parts else ""

    async def _parse_pdf_response(
        self,
        response,
        event_id: Optional[str] = None,
        force_method: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Parse a PDF from an already-fetched response (e.g. DocumentViewer redirect)."""
        try:
            pdf_bytes = await response.read()
            parse = self._parse_pdf_bytes(
                pdf_bytes,
                vendor_id=event_id,
                force_method=force_method,
            )
            # Transfer the buffer to the parser coroutine instead of retaining
            # a second reference for the duration of guarded extraction.
            pdf_bytes = b""
            return await parse
        except Exception as e:
            logger.warning(
                "pdf redirect parse failed",
                vendor="granicus",
                slug=self.slug,
                event_id=event_id,
                error=str(e),
            )
            return []

    def _find_agenda_and_packet_urls(self, html: str, base_url: str) -> tuple[Optional[str], Optional[str]]:
        """Find agenda PDF and packet PDF URLs from the HTML page.

        Returns (agenda_pdf_url, packet_pdf_url). Either or both may be None.
        The agenda PDF is the item listing (possibly hyperlinked).
        The packet PDF is the full bundled document (may have TOC with embedded memos).
        """
        soup = BeautifulSoup(html, 'html.parser')
        agenda_url = None
        packet_url = None

        # MetaViewer links are typically the full packet
        meta_link = soup.find(
            "a",
            href=lambda value: isinstance(value, str) and "MetaViewer" in value,
        )
        if meta_link:
            href = str(meta_link['href'])
            if href.startswith('//'):
                href = 'https:' + href
            else:
                href = urljoin(base_url, href)
            packet_url = href

        # Scan all PDF links for agenda vs packet distinction
        for link in soup.find_all('a', href=True):
            link_href = str(link['href']).lower()
            text = link.get_text(strip=True).lower()
            if '.pdf' not in link_href:
                continue

            full_href = str(link['href'])
            if full_href.startswith('//'):
                full_href = 'https:' + full_href
            else:
                full_href = urljoin(base_url, full_href)

            # Skip if it's the same as MetaViewer we already found
            if full_href == packet_url:
                continue

            if 'packet' in text or 'packet' in link_href:
                if not packet_url:
                    packet_url = full_href
            elif 'agenda' in text or 'agenda' in link_href:
                if not agenda_url:
                    agenda_url = full_href

        # If we only found one PDF and couldn't distinguish, use it as the agenda
        if not agenda_url and not packet_url:
            pdf_link = soup.find(
                "a",
                href=lambda value: isinstance(value, str)
                and ".pdf" in value.lower(),
            )
            if pdf_link:
                href = str(pdf_link['href'])
                if href.startswith('//'):
                    agenda_url = 'https:' + href
                else:
                    agenda_url = urljoin(base_url, href)

        return agenda_url, packet_url
