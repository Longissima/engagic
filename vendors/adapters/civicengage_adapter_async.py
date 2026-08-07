"""
Async CivicEngage Archive Center Adapter

CivicEngage Archive Center is a CivicPlus product used by cities on custom .gov domains.
It's a document archive system (not a meeting calendar like CivicPlus AgendaCenter).

Two listing modes (auto-detected with fallback):

  Search mode (lngArchiveMasterID):
    https://{domain}/Archive.aspx?ysnExecuteSearch=1&lngArchiveMasterID=30&dtiStartDate=...&dtiEndDate=...
    Server-side date filtering. Used by most CivicEngage sites.

  AMID mode:
    https://{domain}/Archive.aspx?AMID=100
    No server-side date filtering — returns all documents in category.
    Used by sites like Wichita where lngArchiveMasterID doesn't match AMID.
    Client-side date filtering applied after fetch.

Both modes produce pages with ADID links:
  https://{domain}/Archive.aspx?ADID=8497  (resolves directly to PDF)

The adapter tries search mode first, falls back to AMID if no results.
"""

import re
import json
import os
from datetime import datetime
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector
from config import config

DEFAULT_CATEGORY_ID = 30  # City Council Agendas (common across CivicEngage sites)

# Keywords that identify agenda categories (vs minutes, audio, video, budgets, etc.)
AGENDA_CATEGORY_KEYWORDS = re.compile(
    r'agenda|committee|commission|forum|subcommittee|board\b.*agenda|zoning administrator',
    re.IGNORECASE,
)
# Categories to skip even if they match keywords above
SKIP_CATEGORY_KEYWORDS = re.compile(
    r'minutes|audio|video|budget|audit|scanned|general plan$',
    re.IGNORECASE,
)


class AsyncCivicEngageAdapter(AsyncBaseAdapter):
    """Async adapter for CivicPlus CivicEngage Archive Center sites."""

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="civicengage", metrics=metrics)
        self._site_config = self._load_site_config()
        # category_ids populated at fetch time via auto-discovery, unless config overrides
        if "category_ids" in self._site_config:
            self.category_ids: List[int] = self._site_config["category_ids"]
        elif "category_id" in self._site_config:
            self.category_ids = [self._site_config["category_id"]]
        else:
            self.category_ids = []  # Will be discovered from Archive.aspx
        # Allow domain override for cities on civicplus.com with state prefixes etc.
        domain_override = self._site_config.get("domain")
        self.base_url: Optional[str] = f"https://{domain_override}" if domain_override else None

    def _get_candidate_base_urls(self) -> List[str]:
        """Extend base candidates with CivicPlus domain."""
        candidates = super()._get_candidate_base_urls()
        candidates.append(f"https://{self.slug}.civicplus.com")
        if "." in self.slug:
            candidates.insert(0, f"https://{self.slug}")
        return candidates

    async def _discover_base_url(self) -> Optional[str]:
        async def _validate(response):
            html = await response.text()
            return response.status == 200 and "archive" in html.lower()

        return await super()._discover_base_url(
            probe_path="/Archive.aspx",
            validate=_validate,
        )

    def _load_site_config(self) -> Dict[str, Any]:
        """Load site-specific config (category_id, domain override, etc)."""
        config_file = os.path.join(config.DB_DIR, "civicengage_sites.json")
        if os.path.exists(config_file):
            try:
                with open(config_file) as f:
                    sites = json.load(f)
                    return sites.get(self.slug, {})
            except Exception:
                pass
        return {}

    async def _discover_category_ids(self, html: str) -> List[int]:
        """Discover agenda-related category IDs from the Archive.aspx search form.

        Parses the <select name="lngArchiveMasterID"> dropdown and filters
        for categories whose labels suggest agendas (not minutes, audio, etc.).
        """
        soup = BeautifulSoup(html, "html.parser")
        select = soup.find("select", {"name": "lngArchiveMasterID"})
        if not select:
            return [DEFAULT_CATEGORY_ID]

        discovered = []
        # Track agenda vs packet categories per body so we can prefer packets.
        # "City Council Agendas" and "City Council Packets" share body "City Council".
        body_categories: Dict[str, List[tuple]] = {}

        for option in select.find_all("option"):
            value = option.get("value")
            label = option.get_text(strip=True)
            if not isinstance(value, str) or not value.isdigit() or value == "0":
                continue
            # Filter: must look like an agenda category, not minutes/audio/video
            if SKIP_CATEGORY_KEYWORDS.search(label):
                continue
            if AGENDA_CATEGORY_KEYWORDS.search(label):
                # Extract body name by stripping trailing type keywords
                body = re.sub(
                    r'\s*(packets?|agendas?|action agendas?)\s*:?\s*$',
                    '', label, flags=re.IGNORECASE
                ).strip()
                is_packet = bool(re.search(r'packet', label, re.IGNORECASE))
                if body not in body_categories:
                    body_categories[body] = []
                body_categories[body].append((int(value), is_packet, label))

        # For each body, prefer packet categories over plain agendas
        for body, entries in body_categories.items():
            packets = [e for e in entries if e[1]]
            agendas = [e for e in entries if not e[1]]
            if packets:
                for cat_id, _, _ in packets:
                    discovered.append(cat_id)
            else:
                for cat_id, _, _ in agendas:
                    discovered.append(cat_id)

        if discovered:
            logger.info(
                "discovered agenda categories",
                vendor="civicengage",
                slug=self.slug,
                count=len(discovered),
                category_ids=discovered,
            )
            return discovered

        logger.warning(
            "no agenda categories found, using default",
            vendor="civicengage",
            slug=self.slug,
        )
        return [DEFAULT_CATEGORY_ID]

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 14
    ) -> List[Dict[str, Any]]:
        """Fetch agendas from CivicEngage Archive Center.

        Auto-discovers agenda category IDs from the archive page if not configured.
        Iterates over categories, tries search mode first, falls back to AMID.
        """
        if not self.base_url:
            self.base_url = await self._discover_base_url()
        if not self.base_url:
            logger.error(
                "no archive page found - cannot fetch meetings",
                vendor="civicengage",
                slug=self.slug,
            )
            return []

        # Auto-discover category IDs if not configured
        if not self.category_ids:
            archive_html = await (await self._get(f"{self.base_url}/Archive.aspx")).text()
            self.category_ids = await self._discover_category_ids(archive_html)

        start_date, end_date = self._date_range(days_back, days_forward)

        all_meetings = []
        seen_adids = set()

        for category_id in self.category_ids:
            meetings = await self._fetch_category_meetings(
                category_id, start_date, end_date
            )
            # Dedup across categories by vendor_id
            for m in meetings:
                vid = m.get("vendor_id", "")
                if vid not in seen_adids:
                    seen_adids.add(vid)
                    all_meetings.append(m)

        # Parse packet PDFs for structured items
        await self._bounded_gather(
            [self._enrich_meeting_from_pdf(m) for m in all_meetings],
            max_concurrent=3,
        )

        logger.info(
            "parsed meetings from listing",
            vendor="civicengage",
            slug=self.slug,
            count=len(all_meetings),
            with_items=sum(1 for m in all_meetings if m.get("items")),
            categories=len(self.category_ids),
        )

        return all_meetings

    async def _fetch_category_meetings(
        self,
        category_id: int,
        start_date: datetime,
        end_date: datetime,
    ) -> List[Dict[str, Any]]:
        """Fetch meetings for a single archive category.

        Tries search mode (lngArchiveMasterID with date filtering) first.
        Falls back to AMID mode if search returns no results.
        """
        listing_html = await self._fetch_listing_search(category_id, start_date, end_date)
        meetings = self._parse_listing_html(listing_html)

        if not meetings:
            logger.info(
                "search mode returned no results, trying AMID mode",
                vendor="civicengage",
                slug=self.slug,
                category_id=category_id,
            )
            listing_html = await self._fetch_listing_amid(category_id)
            all_in_category = self._parse_listing_html(listing_html)

            # Client-side date filtering — skip items with no parseable date
            for m in all_in_category:
                if m.get("start"):
                    try:
                        meeting_date = datetime.fromisoformat(m["start"])
                        if start_date <= meeting_date <= end_date:
                            meetings.append(m)
                    except ValueError:
                        pass

            logger.info(
                "AMID mode results",
                vendor="civicengage",
                slug=self.slug,
                category_id=category_id,
                total=len(all_in_category),
                in_range=len(meetings),
            )

        return meetings

    async def _fetch_listing_search(
        self, category_id: int, start_date: datetime, end_date: datetime
    ) -> str:
        """Fetch Archive.aspx listing with lngArchiveMasterID search (server-side date filter)."""
        url = (
            f"{self.base_url}/Archive.aspx"
            f"?ysnExecuteSearch=1"
            f"&txtKeywords="
            f"&lngArchiveMasterID={category_id}"
            f"&txtDateRange="
            f"&dtiStartDate={start_date.strftime('%m/%d/%Y')}"
            f"&dtiEndDate={end_date.strftime('%m/%d/%Y')}"
        )

        response = await self._get(url)
        return await response.text()

    async def _fetch_listing_amid(self, category_id: int) -> str:
        """Fetch Archive.aspx listing with AMID parameter (all documents in category)."""
        url = f"{self.base_url}/Archive.aspx?AMID={category_id}"
        response = await self._get(url)
        return await response.text()

    def _parse_listing_html(self, html: str) -> List[Dict[str, Any]]:
        """Parse ADID links from listing page into meeting dicts.

        Each ADID link provides:
        - title: from link text (e.g., "City Council Agenda - February 24, 2026")
        - packet_url: the ADID URL itself (resolves to PDF)
        - vendor_id: ce_adid_{ADID}
        - start: parsed from the title text
        """
        soup = BeautifulSoup(html, "html.parser")
        results = []
        seen_adids = set()

        for link in soup.find_all("a", href=re.compile(r"ADID=\d+")):
            href = link.get("href", "")
            title = link.get_text(strip=True)

            adid_match = re.search(r"ADID=(\d+)", str(href))
            if not adid_match or not title:
                continue

            adid = adid_match.group(1)
            if adid in seen_adids:
                continue
            seen_adids.add(adid)

            # Extract date from title
            date_str = self._extract_date_from_title(title)
            parsed_date = self._parse_date(date_str) if date_str else None

            # Detect meeting status
            meeting_status = self._parse_meeting_status(title, date_str)

            packet_url = urljoin(
                f"{self.base_url}/", str(href)
            )

            meeting: Dict[str, Any] = {
                "vendor_id": f"ce_adid_{adid}",
                "title": title,
                "start": parsed_date.isoformat() if parsed_date else "",
                "packet_url": packet_url,
            }

            if meeting_status:
                meeting["meeting_status"] = meeting_status

            results.append(meeting)

        return results

    async def _enrich_meeting_from_pdf(self, meeting: Dict[str, Any]) -> None:
        """Download packet PDF and parse for structured agenda items."""
        packet_url = meeting.get("packet_url")
        if not packet_url:
            return

        items = await self._parse_packet_pdf(packet_url, meeting.get("vendor_id"))
        if items:
            meeting["items"] = items

    def _extract_date_from_title(self, title: str) -> Optional[str]:
        """Extract date from titles like 'City Council Agenda - February 24, 2026'.

        Supports formats:
        - Full month: "February 24, 2026"
        - Slash numeric: "02/24/2026"
        - Dash numeric: "03-24-2026" (Wichita style)
        """
        # Full month name: "February 24, 2026"
        month_match = re.search(
            r"\b(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
            title,
            re.IGNORECASE,
        )
        if month_match:
            return month_match.group(0)

        # Slash numeric: "02/24/2026"
        slash_match = re.search(r"\b\d{1,2}/\d{1,2}/\d{4}\b", title)
        if slash_match:
            return slash_match.group(0)

        # Dash numeric: "03-24-2026" (e.g., Wichita)
        dash_match = re.search(r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", title)
        if dash_match:
            # Normalize to slash format for _parse_date
            return f"{dash_match.group(1)}/{dash_match.group(2)}/{dash_match.group(3)}"

        return None
