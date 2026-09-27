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
import tempfile
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, urljoin, parse_qs, parse_qsl, urlencode, urlunparse

import aiohttp
from bs4 import BeautifulSoup

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.parsers.civicplus_parser import explode_document_catalog, parse_civicplus_html
from pipeline.protocols import MetricsCollector
from exceptions import VendorHTTPError
from config import config


class AsyncCivicPlusAdapter(AsyncBaseAdapter):
    """Async adapter for cities using CivicPlus CMS (often with external agenda systems)"""

    MINUTES_DISCOVERY_SUPPORTED = True
    ORIGINALS_ARCHIVE_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="civicplus", metrics=metrics)
        self._history_year_cache = {}
        self._history_root = None
        self._archive_html = {}
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

        The lock lives in a stable sidecar file, rather than the config inode:
        atomic replacement changes the config inode, which would otherwise let
        a concurrently opened writer read the old file and clobber a discovery.
        Each writer also gets its own temporary filename.
        """
        config_file = os.path.join(config.DB_DIR, "civicplus_sites.json")
        lock_file = f"{config_file}.lock"
        tmp_path: Optional[str] = None
        try:
            os.makedirs(config.DB_DIR, exist_ok=True)
            with open(lock_file, "a+", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    with open(config_file, encoding="utf-8") as source:
                        raw = source.read()
                except FileNotFoundError:
                    raw = ""
                sites = json.loads(raw) if raw.strip() else {}
                sites.setdefault(self.slug, {}).update(updates)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=config.DB_DIR,
                    prefix=".civicplus_sites.",
                    suffix=".tmp",
                    delete=False,
                ) as tmp:
                    tmp_path = tmp.name
                    json.dump(sites, tmp, indent=2, sort_keys=True)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_path, config_file)
                tmp_path = None
                self._site_config = sites[self.slug]
        except Exception:
            logger.warning("failed to persist civicplus site config", vendor="civicplus", slug=self.slug)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    def _get_candidate_base_urls(self) -> List[str]:
        """Extend base candidates with CivicPlus domain."""
        candidates = [f"https://{self.slug}.civicplus.com"]
        candidates.extend(super()._get_candidate_base_urls())
        if "." in self.slug:
            candidates.insert(0, f"https://{self.slug}")
        return candidates

    async def _find_agenda_url(self) -> Optional[str]:
        """Discover agenda page URL from common CivicPlus patterns across candidate domains."""
        # A manual domain is an explicit retry authority. It must override a
        # stale failed marker so operators can repair a slug without editing
        # two fields or waiting for a code deployment.
        if self._site_config.get("failed") and not self._site_config.get("domain"):
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

        saw_retryable_failure = False
        for base_url in candidates:
            for pattern in patterns:
                test_url = f"{base_url}{pattern}"
                try:
                    # The fetcher retries failed city syncs. A discovery probe
                    # gets one request attempt so a dead DNS name does not pay
                    # the full request retry budget for every candidate path.
                    response = await self._get(test_url, _max_attempts=1)
                    html = await response.text()
                    if response.status == 200 and (
                        "agenda" in html.lower()
                        or "meeting" in html.lower()
                    ):
                        self.base_url = base_url
                        logger.info("found agenda page", vendor="civicplus", slug=self.slug, base_url=base_url, pattern=pattern)
                        domain = base_url.removeprefix("https://").removeprefix("http://")
                        if (
                            self._site_config.get("domain") != domain
                            or self._site_config.get("failed")
                        ):
                            self._update_site_config(
                                {"domain": domain, "failed": False, "failed_at": None}
                            )
                        return test_url
                except VendorHTTPError as error:
                    # ``VendorHTTPError`` deliberately classifies most 4xx
                    # responses as permanent, but 429 means the remote site
                    # is temporarily rate-limiting us.  Do not turn that
                    # operational condition into a durable "site absent"
                    # marker.
                    is_transient = error.is_retryable or error.status_code == 429
                    saw_retryable_failure = saw_retryable_failure or is_transient
                    # A connection-level failure applies to the host, not this
                    # particular path. Keep the remaining domain candidates
                    # open, but do not probe four more paths on the same dead
                    # host.
                    if isinstance(error.__cause__, aiohttp.ClientConnectionError):
                        break
                    continue

        if saw_retryable_failure:
            logger.warning(
                "could not find agenda page because candidate probes failed transiently",
                vendor="civicplus",
                slug=self.slug,
            )
            # Do not collapse a known operational failure into a legitimate
            # zero-meeting result. AsyncBaseAdapter converts this to
            # FetchResult(success=False), so the fetcher retries and does not
            # write a successful lifecycle checkpoint.
            raise VendorHTTPError(
                "CivicPlus agenda discovery failed transiently",
                vendor="civicplus",
                city_slug=self.slug,
            )

        logger.warning("could not find agenda page, tombstoning slug", vendor="civicplus", slug=self.slug)
        self._update_site_config({"failed": True, "failed_at": datetime.now(timezone.utc).isoformat()})
        return None

    async def _historical_meeting_links(self, soup, agenda_url, start_date, end_date):
        """Use AgendaCenter's own category/year endpoint and existing row parser.

        Each response contains the complete annual category table. Unknown paging
        controls fail explicitly rather than turning a partial table into coverage.
        Cache parsed years only during archival; normal sync always sees revisions.
        """
        # Preserve the proven current-year normal sync path. Only historical
        # ranges (or originals mode) need category/year enumeration.
        if (not self._originals_only and not getattr(self, '_explicit_range', None)
                and start_date.year == end_date.year == datetime.now().year):
            return self._extract_meeting_links(soup, agenda_url)
        categories = {}
        for checkbox in soup.select('input[name="chkCategoryID"]'):
            value = checkbox.get('value', '')
            if value.isdigit():
                label = checkbox.find_parent('label')
                categories[int(value)] = label.get_text(' ', strip=True) if label else value
        for section in soup.select('div.listing, div.category'):
            heading = section.find(['h3', 'h2'])
            for match in re.finditer(r'changeYear\(\s*\d{4}\s*,\s*(\d+)', str(section)):
                categories.setdefault(int(match[1]), heading.get_text(' ', strip=True) if heading else match[1])
        if not categories:
            if self._originals_only or getattr(self, '_explicit_range', None):
                raise ValueError('Historical CivicPlus discovery requires an AgendaCenter category listing')
            return self._extract_meeting_links(soup, agenda_url)

        links = []
        for category, body in sorted(categories.items()):
            section = soup.find(id=f'section{category}')
            current = section.select_one('.years .current') if section else None
            current_year = current.get_text(strip=True) if current else None
            for year in range(start_date.year, end_date.year + 1):
                key = (agenda_url, category, year)
                cached = self._history_year_cache.get(key) if self._originals_only else None
                if cached is not None:
                    links.extend(cached)
                    continue
                if section is not None and current_year == str(year):
                    year_soup = BeautifulSoup(str(section), 'html.parser')
                else:
                    response = await self._post(
                        urljoin(agenda_url, '/AgendaCenter/UpdateCategoryList'),
                        data={'year': str(year), 'catID': str(category), 'startDate': '',
                              'endDate': '', 'term': '', 'prevVersionScreen': 'false'},
                        headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': agenda_url},
                    )
                    year_soup = BeautifulSoup(await response.text(), 'html.parser')
                if not year_soup.find(id=f'section{category}') or not year_soup.find(id=f'table{category}'):
                    raise ValueError(f'Unrecognized CivicPlus history response: category={category}, year={year}')
                for pager in year_soup.select('.pagination, .pager, [class*="Pager"], [class*="paging"]'):
                    if pager.select('a[href], button, select'):
                        raise ValueError(f'Untraversed CivicPlus history pagination: category={category}, year={year}')
                rows = year_soup.select('tr.catAgendaRow')
                for row in rows:
                    dated = row.find('a', href=re.compile(r'/ViewFile/(?:Agenda|Minutes)/'))
                    parsed_date = self._extract_date_from_url(dated['href']) if dated else None
                    if parsed_date is None or parsed_date.year != year:
                        raise ValueError(f'Unconfirmed CivicPlus row year: category={category}, requested={year}')
                # The response is a section fragment; wrap it so the same normal
                # row parser retains body attribution, downloads and minutes.
                wrapper = year_soup.new_tag('div', attrs={'class': 'listing'})
                heading = year_soup.new_tag('h2'); heading.string = body
                wrapper.append(heading)
                for child in list(year_soup.contents):
                    wrapper.append(child.extract())
                year_soup.append(wrapper)
                parsed = self._extract_meeting_links(year_soup, agenda_url)
                if self._originals_only and len(parsed) != len(rows):
                    raise ValueError(f'Incomplete CivicPlus category rows: {len(parsed)}/{len(rows)}, category={category}, year={year}')
                if self._originals_only:
                    self._history_year_cache[key] = parsed
                logger.info('civicplus historical category listed', slug=self.slug,
                            category=category, year=year, rows=len(rows))
                links.extend(parsed)
        return links

    def _pair_archive_packets(self, meetings):
        """Pair unambiguous same-date agenda/packet categories, retaining identity."""
        groups = {}
        for meeting in meetings:
            body = re.sub(r"\s+(?:agendas?|packets?)$", "", meeting.get('body_name') or '', flags=re.I).strip().casefold()
            if body:
                groups.setdefault((meeting.get('start'), body), []).append(meeting)
        removed = set()
        for group in groups.values():
            packets = [m for m in group if re.search(r"\bpackets?$", m.get('body_name') or '', re.I)]
            agendas = [m for m in group if re.search(r"\bagendas?$", m.get('body_name') or '', re.I)]
            if len(group) != 2 or len(packets) != 1 or len(agendas) != 1:
                continue  # Multiple sessions or revisions need explicit source evidence.
            agenda, packet = agendas[0], packets[0]
            agenda['published_documents'] = agenda.get('published_documents', []) + packet.get('published_documents', [])
            agenda['related_listing_rows'] = [packet.get('raw_listing_row')]
            agenda['related_vendor_ids'] = [packet['vendor_id']]
            if not agenda.get('minutes_url') and packet.get('minutes_url'):
                agenda['minutes_url'] = packet['minutes_url']
            removed.add(id(packet))
        return [meeting for meeting in meetings if id(meeting) not in removed]

    async def _select_archive_documents(self, meeting):
        """Keep HTML + native attachments, otherwise a packet or agenda original."""
        original_url = meeting.get('packet_url')
        if original_url and '/ViewFile/Agenda/' in original_url:
            parsed = urlparse(original_url)
            original_url = urlunparse(parsed._replace(query=urlencode([
                (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.lower() != 'html'
            ])))
        items = []
        if original_url and '/ViewFile/Agenda/' in original_url:
            items = await self._try_html_agenda(original_url, meeting.get('vendor_id'))
        catalog = explode_document_catalog(items) if items else None
        packet = (catalog or {}).get('packet_url') or self._detect_monolithic_packet(items)
        native_items = (catalog or {}).get('items', items)
        native_items = [item for item in native_items if not self._is_packet_item(item)]
        documents = meeting.get('published_documents', [])
        packet = packet or next((d['url'] for d in documents if d['role'] == 'packet'), None)
        meeting['archive_documents'] = [d for d in documents if d['role'] in {'supplemental', 'minutes'}]
        if native_items and any(i.get('attachments') for i in native_items) and not self._detect_monolithic_packet(items):
            meeting['items'] = native_items
            meeting['agenda_url'] = original_url.split('?')[0] + '?html=true'
            meeting.pop('packet_url', None)
            meeting['archive_selection'] = 'html_and_attachments'
        else:
            meeting['items'] = [{**i, 'attachments': []} for i in native_items]
            meeting['packet_url'] = packet or original_url
            meeting['archive_selection'] = 'full_packet' if packet else 'agenda_document'
        if meeting.get('vendor_id') in self._archive_html:
            meeting['raw_agenda_html'] = self._archive_html.pop(meeting['vendor_id'])

    async def _fetch_meetings_impl(self, days_back: int = 28, days_forward: int = 28) -> List[Dict[str, Any]]:
        """Scrape AgendaCenter HTML and filter meetings by date range."""
        start_date, end_date = self._date_range(days_back, days_forward)

        agenda_url = self._history_root[0] if self._originals_only and self._history_root else await self._find_agenda_url()

        if not agenda_url:
            if self._originals_only:
                raise ValueError('No CivicPlus historical agenda listing discovered')
            logger.error(
                "no agenda page found - cannot fetch meetings",
                vendor="civicplus",
                slug=self.slug
            )
            return []

        try:
            if self._originals_only and self._history_root:
                html = self._history_root[1]
            else:
                response = await self._get(agenda_url)
                html = await response.text()
                if self._originals_only:
                    self._history_root = (agenda_url, html)
            soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')
            meeting_links = await self._historical_meeting_links(soup, agenda_url, start_date, end_date)

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
                if '/ViewFile/Agenda/' in link_data['url'] or (self._originals_only and '/ViewFile/Minutes/' in link_data['url']):
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
            if self._originals_only:
                deduped = self._pair_archive_packets(deduped)

            # Try to parse packet PDFs for structured items
            if self._originals_only:
                pending = iter(deduped)
                async def select_worker():
                    for meeting in pending:
                        await self._select_archive_documents(meeting)
                async with asyncio.TaskGroup() as group:
                    for _ in range(min(3, len(deduped))):
                        group.create_task(select_worker())
            elif not self._minutes_discovery_only:
                pdf_tasks = [
                    self._try_parse_packet_items(meeting)
                    for meeting in deduped
                    if meeting.get("packet_url") and not meeting.get("items")
                ]
                if pdf_tasks:
                    await self._bounded_gather(pdf_tasks, max_concurrent=4, return_exceptions=True)

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
            if self._originals_only:
                raise
            logger.error("failed to fetch meetings", vendor="civicplus", slug=self.slug, error=str(e))
            return []

    def _is_meeting_in_range(
        self, meeting: Dict[str, Any], start_date: datetime, end_date: datetime
    ) -> bool:
        """Check if meeting date is within range. Includes meetings with unparseable dates."""
        meeting_start = meeting.get("start")
        if not meeting_start:
            if self._originals_only:
                raise ValueError('Historical CivicPlus meeting has no date')
            return True

        try:
            meeting_date = datetime.fromisoformat(meeting_start)
            return start_date <= meeting_date <= end_date
        except (ValueError, AttributeError):
            if self._originals_only:
                raise
            return True

    def _attach_supplemental_documents(self, meetings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """An explicitly item-labelled document is a source, not another meeting.

        Only fold it when date, body and meeting title identify one parent.
        Ambiguous/orphan entries stay intact rather than losing their document.
        """
        marker = re.compile(r"^(.+?)\s*\(Item\s+\d+[A-Za-z]?\s*[-–—:]\s*.+\)\s*$", re.I)

        def title_key(title):
            title = re.sub(r"\b(?:city commission|city council|regular|meeting|agenda)\b", "", title, flags=re.I)
            return re.sub(r"[^a-z0-9]", "", title.lower())

        parents = [m for m in meetings if not marker.match(m.get("title", ""))]
        retained = []
        for meeting in meetings:
            match = marker.match(meeting.get("title", ""))
            if not match:
                retained.append(meeting)
                continue
            candidates = [p for p in parents
                          if p.get("start") and p.get("start") == meeting.get("start")
                          and p.get("body_name") == meeting.get("body_name")
                          and title_key(p.get("title", "")) == title_key(match[1])]
            if len(candidates) != 1 or not meeting.get("packet_url"):
                retained.append(meeting)
                continue
            parent = candidates[0]
            sources = parent.setdefault("agenda_sources", [])
            if not any(s.get("url") == meeting["packet_url"] for s in sources):
                sources.append({"type": "supplemental", "url": meeting["packet_url"],
                                "label": meeting["title"]})
        return retained

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
                if self._originals_only:
                    chosen['published_documents'] = list({d['url']: d for d in
                        chosen.get('published_documents', []) + other.get('published_documents', [])}.values())
            else:
                by_key[key] = meeting
        return self._attach_supplemental_documents(list(by_key.values()))

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
                if not self._originals_only and body_name and re.search(
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
                    if not link and self._originals_only:
                        link = row.find('a', href=re.compile(r'/ViewFile/(?:Agenda|Minutes)/'))
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
                    if self._originals_only:
                        entry['raw_listing_row'] = str(row)
                        entry['published_documents'] = []
                        for doc in row.find_all('a', href=True):
                            href = doc['href']
                            if not re.search(r'/ViewFile/(?:Agenda|Minutes|Item)/|/DocumentCenter/(?:View|Home/View)/', href, re.I):
                                continue
                            label = doc.get_text(' ', strip=True)
                            role = ('minutes' if '/ViewFile/Minutes/' in href else
                                    'supplemental' if doc is not link and re.search(r'supplement|addend', label, re.I) else
                                    'packet' if 'packet=true' in href.lower() or re.fullmatch(r'(?:agenda |full |meeting )?packet', label, re.I) or re.search(r'\bpackets?$', body_name or '', re.I) else
                                    'agenda' if '/ViewFile/Agenda/' in href else 'supplemental')
                            entry['published_documents'].append({'url': urljoin(base_url, href), 'role': role, 'label': label})
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

        if self._originals_only:
            result['raw_listing_row'] = link_data.get('raw_listing_row')
            result['published_documents'] = link_data.get('published_documents', [])
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
                # Step 1a: a document catalog (headings per document group,
                # items encoded in staff-report filenames) explodes into one
                # item per file; the packet becomes packet_url, never an item.
                catalog = explode_document_catalog(items)
                if catalog:
                    meeting["items"] = catalog["items"]
                    if catalog["packet_url"]:
                        meeting["packet_url"] = catalog["packet_url"]
                    logger.info(
                        "document catalog agenda exploded into per-file items",
                        vendor="civicplus",
                        slug=self.slug,
                        vendor_id=vendor_id,
                        catalog_headings=len(items),
                        items=len(catalog["items"]),
                    )
                    return
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
            if 'application/pdf' in response.headers.get('Content-Type', '').lower():
                response.release()
                return []  # Some tenants serve their PDF even for ?html=true.
            try:
                html = await response.text()
            except UnicodeDecodeError:
                return []  # A native binary document, not a structured HTML agenda.

            # Verify we got an HTML agenda (not an error page or redirect)
            if '<div id="divItems"' not in html and 'class="item level' not in html:
                logger.debug("html agenda not found", vendor="civicplus", slug=self.slug, vendor_id=vendor_id)
                return []

            if self._originals_only:
                self._archive_html[vendor_id] = html
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
            if self._originals_only and not (isinstance(e, VendorHTTPError) and e.status_code in {400, 404, 410}):
                raise
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
        # Drop only the HTML-view selector. Published packet=true links can
        # be the only working download, and must retain their query parameters.
        pdf_url = packet_url
        if '/ViewFile/Agenda/' in packet_url:
            parsed = urlparse(packet_url)
            query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                     if key.lower() != "html"]
            pdf_url = urlunparse(parsed._replace(query=urlencode(query)))

        items = await self._parse_packet_pdf(pdf_url, vendor_id)
        if items:
            meeting["items"] = items
