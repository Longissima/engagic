"""
Async CivicPlus Adapter - Discovery and scraping for CivicPlus sites

CivicPlus cities use varied hosting:
- *.civicplus.com (standard)
- *.gov / *.org (custom domains)
- Arbitrary domains (e.g., www.kingcity.com)

Domain resolution order:
1. Config override from data/civicplus_sites.json (if present)
2. {slug}.civicplus.com
3. www.{slug}.gov / .org
4. {slug}.gov / .org
"""

import fcntl
import json
import os
import re
import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, urljoin, parse_qs

import aiohttp
from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.parsers.civicplus_parser import parse_civicplus_html
from pipeline.protocols import MetricsCollector
from exceptions import VendorHTTPError
from config import config


class AsyncCivicPlusAdapter(AsyncBaseAdapter):
    """Async adapter for cities using CivicPlus CMS (often with external agenda systems)"""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="civicplus", metrics=metrics)
        self._site_config = self._load_site_config()
        domain_override = self._site_config.get("domain")
        self.base_url = f"https://{domain_override}" if domain_override else None

    def _load_site_config(self) -> Dict[str, Any]:
        """Load site-specific config (domain override, etc) from civicplus_sites.json."""
        config_file = os.path.join(config.DB_DIR, "civicplus_sites.json")
        if os.path.exists(config_file):
            try:
                with open(config_file) as f:
                    sites = json.load(f)
                    return sites.get(self.slug, {})
            except Exception:
                pass
        return {}

    def _update_site_config(self, updates: Dict[str, Any]) -> None:
        """Merge updates into this slug's entry in civicplus_sites.json.

        Locked read-modify-write plus atomic replace so concurrent adapters
        discovering different slugs cannot clobber each other's entries.
        A manually maintained file otherwise never learns what the matrix
        search already paid to find (or already exhausted).

        A "failed" tombstone is permanent until a human removes it (or adds
        a working "domain") -- _find_agenda_url short-circuits on it before
        ever reaching the matrix search again, so there is no automatic
        self-healing path here. This matches how civicplus_sites.json,
        visioninternet_sites.json, and granicus_view_ids.json already work:
        machine-discovered or machine-exhausted, human-corrected.
        """
        config_file = os.path.join(config.DB_DIR, "civicplus_sites.json")
        try:
            with open(config_file, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read()
                sites = json.loads(raw) if raw.strip() else {}
                sites.setdefault(self.slug, {}).update(updates)
                tmp_path = f"{config_file}.tmp"
                with open(tmp_path, "w") as tmp:
                    json.dump(sites, tmp, indent=2, sort_keys=True)
                os.replace(tmp_path, config_file)
                self._site_config = sites[self.slug]
        except Exception:
            logger.warning("failed to persist civicplus site config", vendor="civicplus", slug=self.slug)

    def _get_candidate_base_urls(self) -> List[str]:
        """Extend base candidates with CivicPlus domain."""
        candidates = [f"https://{self.slug}.civicplus.com"]
        candidates.extend(super()._get_candidate_base_urls())
        if "." in self.slug:
            candidates.insert(0, f"https://{self.slug}")
        return candidates

    async def _find_agenda_url(self) -> Optional[str]:
        """Discover agenda page URL from common CivicPlus patterns across candidate domains."""
        if self._site_config.get("failed"):
            logger.warning(
                "civicplus slug previously exhausted the candidate matrix, skipping search. "
                "Remove the failed entry (or add a working domain) in data/civicplus_sites.json to retry.",
                vendor="civicplus", slug=self.slug,
            )
            return None

        patterns = [
            "/AgendaCenter",
            "/Calendar.aspx",
            "/calendar",
            "/meetings",
            "/agendas",
        ]

        # Config override narrows search to just the configured domain
        if self.base_url:
            candidates = [self.base_url]
        else:
            candidates = self._get_candidate_base_urls()

        for base_url in candidates:
            for pattern in patterns:
                test_url = f"{base_url}{pattern}"
                try:
                    response = await self._get(test_url)
                    html = await response.text()
                    if response.status == 200 and (
                        "agenda" in html.lower()
                        or "meeting" in html.lower()
                    ):
                        self.base_url = base_url
                        logger.info("found agenda page", vendor="civicplus", slug=self.slug, base_url=base_url, pattern=pattern)
                        domain = base_url.removeprefix("https://").removeprefix("http://")
                        if self._site_config.get("domain") != domain:
                            self._update_site_config({"domain": domain})
                        return test_url
                except VendorHTTPError:
                    continue

        logger.warning("could not find agenda page, tombstoning slug", vendor="civicplus", slug=self.slug)
        self._update_site_config({"failed": True, "failed_at": datetime.now(timezone.utc).isoformat()})
        return None

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Scrape AgendaCenter HTML and filter meetings by date range."""
        start_date, end_date = self._date_range(days_back, days_forward)

        agenda_url = await self._find_agenda_url()

        if not agenda_url:
            logger.error(
                "no agenda page found - cannot fetch meetings",
                vendor="civicplus",
                slug=self.slug
            )
            return []

        try:
            response = await self._get(agenda_url)
            html = await response.text()
            soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')
            meeting_links = self._extract_meeting_links(soup, agenda_url)

            logger.info(
                "found meeting links",
                vendor="civicplus",
                slug=self.slug,
                count=len(meeting_links)
            )

            results = []
            for link_data in meeting_links:
                if self._minutes_discovery_only and not link_data.get("minutes_url"):
                    continue
                if '/ViewFile/Agenda/' in link_data['url']:
                    meeting = self._create_meeting_from_viewfile_link(link_data)
                    if meeting and self._is_meeting_in_range(meeting, start_date, end_date):
                        results.append(meeting)
                else:
                    meeting = await self._scrape_meeting_page(
                        link_data["url"], link_data["title"],
                        body_name=link_data.get("body_name"),
                        minutes_url=link_data.get("minutes_url"),
                    )
                    if meeting and self._is_meeting_in_range(meeting, start_date, end_date):
                        results.append(meeting)

            # Dedupe by date - keep the last one (packet is typically uploaded after agenda)
            deduped = self._dedupe_by_date(results)

            # Try to parse packet PDFs for structured items
            if not self._minutes_discovery_only:
                pdf_tasks = [
                    self._try_parse_packet_items(meeting)
                    for meeting in deduped
                    if meeting.get("packet_url") and not meeting.get("items")
                ]
                if pdf_tasks:
                    await asyncio.gather(*pdf_tasks, return_exceptions=True)

            logger.info(
                "filtered meetings in date range",
                vendor="civicplus",
                slug=self.slug,
                count=len(deduped),
                before_dedupe=len(results),
                with_items=sum(1 for m in deduped if m.get("items")),
                start_date=str(start_date.date()),
                end_date=str(end_date.date())
            )

            return deduped

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error("failed to fetch meetings", vendor="civicplus", slug=self.slug, error=str(e))
            return []

    def _is_meeting_in_range(
        self, meeting: Dict[str, Any], start_date: datetime, end_date: datetime
    ) -> bool:
        """Check if meeting date is within range. Includes meetings with unparseable dates."""
        meeting_start = meeting.get("start")
        if not meeting_start:
            return True

        try:
            meeting_date = datetime.fromisoformat(meeting_start)
            return start_date <= meeting_date <= end_date
        except (ValueError, AttributeError):
            return True

    def _dedupe_by_date(self, meetings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Dedupe meetings, keeping one per logical meeting.

        Dedup hierarchy:
        1. packet_url — same packet PDF means same meeting regardless of title
        2. vendor_id — same vendor_id means same meeting regardless of title
        3. date + body_name — same committee on same date is one meeting
        4. date + title — fallback for meetings without body_name

        Multiple committees can meet on the same date — those are distinct.
        """
        by_key: Dict[str, Dict[str, Any]] = {}
        # Track packet URLs separately for cross-key dedup
        seen_packet_urls: Dict[str, str] = {}  # packet_url -> key

        for meeting in meetings:
            vendor_id = meeting.get("vendor_id")
            date = meeting.get("start", "unknown")
            body_name = meeting.get("body_name")
            packet_url = meeting.get("packet_url", "")

            # Same packet URL = same meeting, strongest signal
            if packet_url and packet_url in seen_packet_urls:
                key = seen_packet_urls[packet_url]
            elif vendor_id:
                key = f"vid|{vendor_id}"
            elif body_name:
                key = f"{date}|{body_name}"
            else:
                title = meeting.get("title", "unknown")
                key = f"{date}|{title}"

            if packet_url:
                seen_packet_urls[packet_url] = key

            existing = by_key.get(key)
            if existing:
                # Prefer master agenda / packet over plain agenda --
                # master agendas have the full packet PDF we can chunk.
                new_title = (meeting.get("title") or "").lower()
                old_title = (existing.get("title") or "").lower()
                new_is_master = bool(re.search(r"master\s+agenda|agenda\s+packet|full\s+packet", new_title))
                old_is_master = bool(re.search(r"master\s+agenda|agenda\s+packet|full\s+packet", old_title))
                if new_is_master and not old_is_master:
                    by_key[key] = meeting
                elif not new_is_master and old_is_master:
                    pass  # keep existing master
                elif len(meeting.get("title", "")) > len(existing.get("title", "")):
                    by_key[key] = meeting
                # Whichever copy wins, don't lose the minutes link the other carried
                chosen = by_key[key]
                other = meeting if chosen is existing else existing
                if other.get("minutes_url") and not chosen.get("minutes_url"):
                    chosen["minutes_url"] = other["minutes_url"]
            else:
                by_key[key] = meeting
        return list(by_key.values())

    def _extract_meeting_links(
        self, soup: BeautifulSoup, base_url: str
    ) -> List[Dict[str, str]]:
        """Extract meeting links from AgendaCenter, associating each with its committee section.

        CivicPlus AgendaCenter pages use this structure:
          div.listing#cat{N} > h2 (committee name) > table > tr.catAgendaRow
        Each row has a primary meeting link in a <p> tag and duplicate links
        inside download dropdowns (div.popoutContainer) that must be skipped.

        Falls back to flat link scanning for non-standard CivicPlus layouts.
        """
        links = []
        seen_urls = set()

        # Strategy 1: Parse structured AgendaCenter sections (h2 + table rows).
        # Two-tier sites (e.g. Kenosha County WI) wrap committees in nested
        # div.category > h3 blocks under each div.listing > h2 group; prefer
        # the inner h3 for committee attribution when present.
        category_divs = soup.find_all("div", class_="listing")
        if category_divs:
            sections: List[tuple] = []
            for cat_div in category_divs:
                nested = cat_div.find_all("div", class_="category")
                if nested:
                    for nc in nested:
                        sections.append((nc, nc.find("h3")))
                else:
                    sections.append((cat_div, cat_div.find("h2")))

            for section_div, heading in sections:
                body_name = heading.get_text(strip=True) if heading else None

                # Skip notice-only categories -- these are announcements,
                # not meetings with agendas worth summarizing.
                if body_name and re.search(
                    r"public\s+notice|notice\s+of\s+(?:quorum|posting)|"
                    r"legal\s+notice|press\s+release",
                    body_name, re.IGNORECASE
                ):
                    continue

                for row in section_div.find_all("tr", class_="catAgendaRow"):
                    # Primary meeting link is in a <p> inside the first <td>
                    td = row.find("td")
                    if not td:
                        continue
                    p = td.find("p")
                    link = p.find("a", href=True) if p else None
                    if not link:
                        continue

                    href = link["href"]
                    text = link.get_text(strip=True)
                    if len(text) < 5:
                        continue

                    absolute_url = urljoin(base_url, href)
                    if absolute_url in seen_urls:
                        continue
                    seen_urls.add(absolute_url)

                    entry = {"url": absolute_url, "title": text}
                    if body_name:
                        entry["body_name"] = body_name
                    # The same row pairs the agenda with its minutes document
                    # (posted after the meeting); ViewFile/Minutes serves the PDF
                    minutes_link = row.find(
                        "a", href=re.compile(r"/AgendaCenter/ViewFile/Minutes/")
                    )
                    if minutes_link:
                        entry["minutes_url"] = urljoin(base_url, minutes_link["href"])
                    links.append(entry)

            if links:
                return links

        # Strategy 2: Flat link scan for non-standard layouts
        for link in soup.find_all("a", href=True):
            # Skip links inside download dropdowns
            if link.find_parent("div", class_="popoutContainer"):
                continue
            if link.find_parent("div", class_="popout"):
                continue

            text = link.get_text(strip=True)
            href_value = link.get("href")
            if not isinstance(href_value, str):
                continue
            href = href_value

            skip_patterns = [
                "<<<", "◄", "Back to", "back to",
                "Agendas & Minutes", "agendas & minutes",
                "Calendar", "All Agendas", "all agendas",
            ]
            if any(text.startswith(p) or text == p for p in skip_patterns):
                continue
            if len(text) < 5:
                continue

            is_viewfile = "/ViewFile/Agenda/" in href or "/ViewFile/Item/" in href
            has_date = bool(re.search(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? \d{4}\b', text, re.I))
            has_numeric_date = bool(re.search(r'\b\d{1,2}/\d{1,2}/\d{4}\b', text))

            if is_viewfile or has_date or has_numeric_date:
                absolute_url = urljoin(base_url, href)
                if absolute_url in seen_urls:
                    continue
                seen_urls.add(absolute_url)
                links.append({"url": absolute_url, "title": text})

        return links

    def _extract_date_from_url(self, url: str) -> Optional[datetime]:
        """Extract date from CivicPlus ViewFile URL pattern _MMDDYYYY-ID."""
        # Pattern: /ViewFile/Agenda/_12042025-786 = December 4, 2025
        match = re.search(r'_(\d{2})(\d{2})(\d{4})-\d+', url)
        if match:
            month, day, year = match.groups()
            try:
                return datetime(int(year), int(month), int(day))
            except ValueError:
                return None
        return None

    def _create_meeting_from_viewfile_link(self, link_data: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Create meeting dict directly from ViewFile link without scraping."""
        url = link_data["url"]
        title = link_data["title"]

        # Try to extract date from URL first (more reliable for CivicPlus)
        parsed_date = self._extract_date_from_url(url)
        if not parsed_date:
            date_text = self._extract_date_from_title(title)
            parsed_date = self._parse_date(date_text) if date_text else None

        meeting_id = self._extract_meeting_id(url)

        # Build better title if we have a date
        if parsed_date and title in ["Agenda", "View Meeting Agenda", "View Agenda Packet"]:
            title = f"Meeting - {parsed_date.strftime('%B %d, %Y')}"

        meeting_status = self._parse_meeting_status(title, None)

        result = {
            "vendor_id": meeting_id,
            "title": title,
            "start": parsed_date.isoformat() if parsed_date else None,
            "packet_url": url,
        }

        if meeting_status:
            result["meeting_status"] = meeting_status

        body_name = link_data.get("body_name")
        if body_name:
            result["body_name"] = body_name

        minutes_url = link_data.get("minutes_url")
        if minutes_url:
            result["minutes_url"] = minutes_url

        return result

    async def _scrape_meeting_page(
        self,
        url: str,
        title: str,
        body_name: Optional[str] = None,
        minutes_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Scrape individual meeting page for metadata and PDF links."""
        try:
            response = await self._get(url)
            html = await response.text()
            soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')

            date_text = self._extract_date_from_page(soup)
            if not date_text:
                date_text = self._extract_date_from_title(title)
            parsed_date = self._parse_date(date_text) if date_text else None

            meeting_id = self._extract_meeting_id(url)
            meeting_status = self._parse_meeting_status(title, date_text)

            pdfs = []
            if not self._minutes_discovery_only:
                pdfs = await self._discover_pdfs_async(url, soup)

            if not pdfs:
                logger.debug("no PDFs found for meeting", vendor="civicplus", slug=self.slug, title=title)

            result = {
                "vendor_id": meeting_id,
                "title": title,
                "start": parsed_date.isoformat() if parsed_date else None,
                "packet_url": pdfs[0] if pdfs else None,
            }

            if meeting_status:
                result["meeting_status"] = meeting_status

            if body_name:
                result["body_name"] = body_name

            if minutes_url:
                result["minutes_url"] = minutes_url

            return result

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("failed to scrape meeting page", vendor="civicplus", slug=self.slug, url=url, error=str(e))
            return None

    async def _discover_pdfs_async(
        self, url: str, soup: BeautifulSoup, keywords: Optional[List[str]] = None
    ) -> List[str]:
        """Discover PDF links on a page, optionally filtering by keywords."""
        if keywords is None:
            keywords = ["agenda", "packet"]

        pdfs = []

        for link in soup.find_all("a", href=True):
            href_value = link.get("href")
            if not isinstance(href_value, str):
                continue
            href = href_value
            type_value = link.get("type")
            media_type = type_value if isinstance(type_value, str) else ""
            text = link.get_text().lower()
            is_pdf = (
                ".pdf" in href.lower()
                or "pdf" in media_type.lower()
                or any(kw in text for kw in keywords)
            )

            if is_pdf:
                pdfs.append(urljoin(url, href))

        logger.debug("found PDFs", vendor="civicplus", slug=self.slug, pdf_count=len(pdfs), url=url[:100])
        return pdfs

    def _extract_date_from_page(self, soup: BeautifulSoup) -> Optional[str]:
        """Extract meeting date from page using common patterns."""
        date_patterns = [
            r"\b\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*[APap][Mm]\b",  # MM/DD/YYYY HH:MM AM/PM
            r"\b\d{1,2}/\d{1,2}/\d{4}\b",  # MM/DD/YYYY
            r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\s+\d{1,2}:\d{2}\s*[APap][Mm]\b",  # Month DD, YYYY HH:MM AM/PM
            r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\b",  # Month DD, YYYY
        ]

        text = soup.get_text()
        for pattern in date_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(0)

        return None

    def _extract_date_from_title(self, title: str) -> Optional[str]:
        """Extract date from meeting title like 'October 22, 2025 Regular Meeting'"""
        date_patterns = [
            r"\b([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})\b",  # Month DD, YYYY or Month DD YYYY
            r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",  # MM/DD/YYYY
        ]

        for pattern in date_patterns:
            match = re.search(pattern, title)
            if match:
                return match.group(0)

        return None

    def _extract_meeting_id(self, url: str) -> str:
        """Extract meeting ID from URL or generate hash fallback.

        Confidence: 8/10 - Normalized URL hash is stable across syncs.
        Strips tracking params (session, utm_*) before hashing.
        """
        parsed = urlparse(url)

        # Prefer explicit id parameter
        if "id=" in parsed.query.lower():
            match = re.search(r"id=(\d+)", parsed.query, re.IGNORECASE)
            if match:
                return f"civic_{match.group(1)}"

        # Fallback: Hash normalized URL (strip tracking params for stability)
        # Keep only path and meaningful params, ignore session/tracking
        tracking_params = {'session', 'sessionid', 'sid', 'utm_source', 'utm_medium',
                          'utm_campaign', 'utm_content', 'utm_term', 'fbclid', 'gclid'}

        query_params = parse_qs(parsed.query)
        stable_params = {k: v for k, v in query_params.items()
                        if k.lower() not in tracking_params}

        # Build canonical URL for hashing
        canonical = f"{parsed.netloc}{parsed.path}"
        if stable_params:
            sorted_params = sorted(stable_params.items())
            canonical += "?" + "&".join(f"{k}={v[0]}" for k, v in sorted_params)

        return f"civic_{hashlib.md5(canonical.encode()).hexdigest()[:8]}"

    async def _try_parse_packet_items(self, meeting: Dict[str, Any]) -> None:
        """Try to extract structured items from a meeting via HTML → PDF → monolithic.

        Mutates the meeting dict in-place: adds 'items' if extraction succeeds.
        Falls back gracefully — any error just leaves the meeting as monolithic.

        Priority:
        1. HTML agenda (?html=true) — structured, best quality
        2. If HTML items exist but are mostly attachment-less with a monolithic
           "agenda packet" PDF, run the chunker on that packet for TOC-based
           body_text extraction
        3. PDF agenda chunker — extracts items from PDF
        4. Monolithic packet_url — no items, just the PDF reference
        """
        packet_url = meeting.get("packet_url")
        if not packet_url:
            return

        vendor_id = meeting.get("vendor_id")

        # Step 1: Try HTML agenda if this is a ViewFile URL
        if '/ViewFile/Agenda/' in packet_url:
            items = await self._try_html_agenda(packet_url, vendor_id)
            if items:
                # Step 1b: Check for monolithic packet pattern — HTML items
                # exist with good structure but no per-item attachments, and
                # one "item" is actually the full agenda packet PDF.
                monolithic_url = self._detect_monolithic_packet(items)
                if monolithic_url:
                    # Strip the fake packet item from the HTML items
                    html_items = [
                        item for item in items
                        if not self._is_packet_item(item)
                    ]
                    # Run chunker on the packet PDF for TOC-based body_text
                    packet_meeting: Dict[str, Any] = {}
                    await self._try_pdf_agenda(packet_meeting, monolithic_url, vendor_id)
                    pdf_items = packet_meeting.get("items")

                    if pdf_items and any(
                        item.get("body_text") for item in pdf_items
                    ):
                        # Packet chunker gave items with body_text — use them
                        meeting["items"] = pdf_items
                        meeting["packet_url"] = monolithic_url
                        logger.info(
                            "monolithic packet detected, using chunked items",
                            vendor="civicplus",
                            slug=self.slug,
                            vendor_id=vendor_id,
                            html_items=len(html_items),
                            pdf_items=len(pdf_items),
                        )
                        return
                    else:
                        # Packet chunker didn't produce body_text — keep
                        # HTML items (they at least have titles/descriptions)
                        meeting["items"] = html_items
                        meeting["packet_url"] = monolithic_url
                        logger.debug(
                            "monolithic packet detected but chunker gave no body_text, keeping html items",
                            vendor="civicplus",
                            slug=self.slug,
                            vendor_id=vendor_id,
                            html_items=len(html_items),
                        )
                        return

                meeting["items"] = items
                return

        # Step 2: Fall back to PDF parsing (non-ViewFile URL or no HTML agenda)
        logger.info(
            "trying pdf chunker",
            vendor="civicplus",
            slug=self.slug,
            vendor_id=vendor_id,
        )
        await self._try_pdf_agenda(meeting, packet_url, vendor_id)

    _PACKET_PATTERNS = re.compile(
        r'agenda\s+packet|council\s+agenda\s+packet|meeting\s+packet'
        r'|board\s+agenda\s+packet|commission\s+agenda\s+packet',
        re.IGNORECASE,
    )

    def _is_packet_item(self, item: Dict[str, Any]) -> bool:
        """Check if an item is a monolithic agenda packet reference."""
        title = item.get("title", "")
        if self._PACKET_PATTERNS.search(title):
            return True
        for att in item.get("attachments", []):
            if self._PACKET_PATTERNS.search(att.get("name", "")):
                return True
        return False

    def _detect_monolithic_packet(self, items: List[Dict[str, Any]]) -> Optional[str]:
        """Detect if HTML items contain a monolithic agenda packet instead of per-item attachments.

        Returns the packet PDF URL if the pattern matches, None otherwise.

        Pattern: most substantive items have no attachments, but one item is
        a full "Agenda Packet" PDF covering the entire meeting.
        """
        packet_url = None
        items_with_own_attachments = 0
        substantive_items = 0

        for item in items:
            if self._is_packet_item(item):
                # This is the monolithic packet — extract its PDF URL
                for att in item.get("attachments", []):
                    if att.get("url"):
                        packet_url = att["url"]
                        break
                continue

            # Count substantive items (skip section headers / procedural)
            substantive_items += 1
            if item.get("attachments"):
                items_with_own_attachments += 1

        if not packet_url:
            return None

        # Trigger if most substantive items lack their own attachments
        if substantive_items > 0 and items_with_own_attachments <= substantive_items * 0.3:
            logger.debug(
                "monolithic packet pattern detected",
                vendor="civicplus",
                slug=self.slug,
                substantive_items=substantive_items,
                items_with_attachments=items_with_own_attachments,
                packet_url=packet_url[:100],
            )
            return packet_url

        return None

    async def _try_html_agenda(self, viewfile_url: str, vendor_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch and parse the HTML version of a CivicPlus agenda.

        Constructs ?html=true URL from the ViewFile base and parses the structured HTML.
        Returns list of item dicts, or empty list on failure.
        """
        try:
            # Strip existing query params and add ?html=true
            base_viewfile = viewfile_url.split('?')[0]
            html_url = base_viewfile + '?html=true'

            response = await self._get(html_url)
            html = await response.text()

            # Verify we got an HTML agenda (not an error page or redirect)
            if '<div id="divItems"' not in html and 'class="item level' not in html:
                logger.debug("html agenda not found", vendor="civicplus", slug=self.slug, vendor_id=vendor_id)
                return []

            parsed = await asyncio.to_thread(parse_civicplus_html, html, self.base_url or "")
            items = parsed.get("items", [])

            if items:
                self._record_html_audit(vendor_id, parsed.get("html_pattern"), items)
                attachment_count = sum(len(item.get("attachments", [])) for item in items)
                logger.info(
                    "parsed items from html agenda",
                    vendor="civicplus",
                    slug=self.slug,
                    vendor_id=vendor_id,
                    html_pattern=parsed.get("html_pattern"),
                    item_count=len(items),
                    attachment_count=attachment_count,
                )

            return items

        except Exception as e:
            logger.debug(
                "html agenda parse failed",
                vendor="civicplus",
                slug=self.slug,
                vendor_id=vendor_id,
                error=str(e),
            )
            return []

    async def _try_pdf_agenda(self, meeting: Dict[str, Any], packet_url: str, vendor_id: Optional[str] = None) -> None:
        """Download and parse a PDF agenda for structured items.

        Mutates meeting dict in-place if items are found.
        """
        # For ViewFile URLs, use the bare URL (no query params) for the PDF
        if '/ViewFile/Agenda/' in packet_url:
            pdf_url = packet_url.split('?')[0]
        else:
            pdf_url = packet_url

        items = await self._parse_packet_pdf(pdf_url, vendor_id)
        if items:
            meeting["items"] = items
