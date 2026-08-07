"""
Async NovusAgenda Adapter - HTML scraping for NovusAgenda platform

Cities using NovusAgenda: Hagerstown MD, Houston TX, and others
"""

import asyncio
import re
from datetime import datetime
from typing import Dict, Any, List, Optional
import aiohttp
from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from vendors.adapters.html_attrs import string_attr
from vendors.adapters.parsers.novusagenda_parser import parse_html_agenda
from pipeline.protocols import MetricsCollector
from bs4 import BeautifulSoup


class AsyncNovusAgendaAdapter(AsyncBaseAdapter):
    """Async adapter for cities using NovusAgenda platform.

    NovusAgenda portals use Telerik RadGrid with varying column layouts.
    Some have a leading checkbox/empty cell, some don't. We detect the date
    cell by content rather than assuming a fixed position.
    """

    MINUTES_DISCOVERY_SUPPORTED = True

    _DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
    _TIME_RE = re.compile(r"^\d{1,2}:\d{2}")

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="novusagenda", metrics=metrics)
        self.base_url = f"https://{self.slug}.novusagenda.com"

    def _detect_row_layout(self, cells) -> Optional[Dict[str, int]]:
        """Identify cell indices by content, not position.

        Returns mapping of field names to cell indices, or None if the row
        doesn't contain a recognizable date cell. Handles layouts with or
        without a leading empty/checkbox column.
        """
        for i, cell in enumerate(cells):
            text = cell.get_text(strip=True)
            if self._DATE_RE.match(text):
                layout = {"date": i, "type": i + 1}
                # Scan remaining cells for a time-like value
                for j in range(i + 2, len(cells)):
                    candidate = cells[j].get_text(strip=True)
                    if self._TIME_RE.match(candidate):
                        layout["time"] = j
                        break
                return layout
        return None

    async def _fetch_meetings_impl(self, days_back: int = 14, days_forward: int = 14) -> List[Dict[str, Any]]:
        """Scrape meetings from NovusAgenda /agendapublic page."""
        response = await self._get(f"{self.base_url}/agendapublic")
        html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')

        start_date, end_date = self._date_range(days_back, days_forward)

        meeting_rows = soup.find_all("tr", class_=["rgRow", "rgAltRow"])
        logger.info("found meeting rows", vendor="novusagenda", slug=self.slug, count=len(meeting_rows))

        meetings = []
        detected_layout = None

        for row in meeting_rows:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            layout = self._detect_row_layout(cells)
            if layout is None:
                # Log once per slug to avoid spam
                logger.warning(
                    "no date cell found in row",
                    vendor="novusagenda",
                    slug=self.slug,
                    cell_texts=[c.get_text(strip=True)[:30] for c in cells[:6]]
                )
                continue

            # Log layout detection once per fetch
            if detected_layout is None:
                detected_layout = layout
                logger.info(
                    "detected row layout",
                    vendor="novusagenda",
                    slug=self.slug,
                    date_col=layout["date"],
                    type_col=layout["type"],
                    time_col=layout.get("time"),
                )

            date_str = cells[layout["date"]].get_text(strip=True)
            meeting_type = cells[layout["type"]].get_text(strip=True) if layout["type"] < len(cells) else ""

            try:
                meeting_date = datetime.strptime(date_str, "%m/%d/%y")
                if meeting_date < start_date or meeting_date > end_date:
                    logger.debug("skipping meeting outside date range", vendor="novusagenda", slug=self.slug, meeting_type=meeting_type, date=date_str)
                    continue
            except ValueError:
                logger.warning("could not parse date", vendor="novusagenda", slug=self.slug, date=date_str, meeting_type=meeting_type)
                continue

            time_idx = layout.get("time")
            time_field = cells[time_idx].get_text(strip=True) if time_idx and time_idx < len(cells) else ""
            meeting_status = self._parse_meeting_status(meeting_type, time_field)

            # Find PDF link and HTML agenda link
            pdf_link = row.find("a", href=re.compile(r"DisplayAgendaPDF\.ashx"))
            all_agenda_links = row.find_all("a", onclick=re.compile(r"MeetingView\.aspx"))

            packet_url = None
            agenda_url = None
            meeting_id = None

            if pdf_link:
                # Extract meeting ID
                pdf_href = string_attr(pdf_link, "href")
                meeting_id_match = re.search(r"MeetingID=(\d+)", pdf_href)
                if meeting_id_match:
                    meeting_id = meeting_id_match.group(1)
                    packet_url = f"{self.base_url}/agendapublic/{pdf_href}"

            # Prioritize parsable HTML agendas over summaries
            best_agenda_link = None
            best_score = 0

            for link in all_agenda_links:
                link_text = link.get_text(strip=True).lower()
                img = link.find("img")
                if img:
                    alt_text = string_attr(img, "alt").lower()
                    link_text = f"{link_text} {alt_text}".strip()

                score = 0
                if "html agenda" in link_text or "online agenda" in link_text:
                    score = 3
                elif ("view agenda" in link_text or "agenda" in link_text) and "summary" not in link_text:
                    score = 2

                if score > best_score:
                    best_score = score
                    best_agenda_link = link

            if best_agenda_link:
                onclick = string_attr(best_agenda_link, "onclick")
                url_match = re.search(r"MeetingView\.aspx\?[^'\"]+", onclick)
                if url_match:
                    agenda_relative_url = url_match.group(0)
                    agenda_url = f"{self.base_url}/agendapublic/{agenda_relative_url}"

                    if not meeting_id:
                        meeting_id_match = re.search(r"MeetingID=(\d+)", agenda_relative_url)
                        if meeting_id_match:
                            meeting_id = meeting_id_match.group(1)

            # Minutes publish post-meeting: a DisplayMinutesPDF handler when the
            # portal exposes a direct PDF, else the MeetingView doctype=Minutes
            # viewer. Rows carry MinutesMeetingID=-1 until minutes exist, so
            # neither pattern appears pre-publication.
            minutes_url = None
            minutes_pdf_link = row.find("a", href=re.compile(r"DisplayMinutesPDF\.ashx", re.IGNORECASE))
            if minutes_pdf_link:
                minutes_url = (
                    f"{self.base_url}/agendapublic/"
                    f"{string_attr(minutes_pdf_link, 'href')}"
                )
            else:
                minutes_view_link = row.find("a", onclick=re.compile(r"doctype=Minutes"))
                if minutes_view_link:
                    minutes_match = re.search(
                        r"MeetingView\.aspx\?[^'\"]+",
                        string_attr(minutes_view_link, "onclick"),
                    )
                    if minutes_match:
                        minutes_url = f"{self.base_url}/agendapublic/{minutes_match.group(0)}"

            if not meeting_id:
                meeting_id = self._generate_fallback_vendor_id(
                    title=meeting_type,
                    date=meeting_date
                )

            if self._minutes_discovery_only:
                if minutes_url:
                    result = {
                        "vendor_id": meeting_id,
                        "title": meeting_type,
                        "start": date_str,
                        "minutes_url": minutes_url,
                    }
                    if meeting_status:
                        result["meeting_status"] = meeting_status
                    meetings.append(result)
                continue

            if not packet_url and not agenda_url:
                logger.debug(
                    "no packet or agenda found",
                    vendor="novusagenda",
                    slug=self.slug,
                    meeting_type=meeting_type,
                    date=date_str
                )

            items = []
            if agenda_url:
                try:
                    response = await self._get(agenda_url)
                    agenda_html = await response.text()
                    parsed = parse_html_agenda(agenda_html)
                    items = parsed.get('items', [])

                    # Fetch attachments from CoverSheet detail pages
                    if items:
                        items = await self._fetch_coversheet_details(items, meeting_id)

                    logger.info(
                        "extracted items from HTML agenda",
                        vendor="novusagenda",
                        slug=self.slug,
                        meeting_id=meeting_id,
                        item_count=len(items),
                        items_with_attachments=sum(1 for i in items if i.get("attachments"))
                    )
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(
                        "failed to fetch HTML agenda",
                        vendor="novusagenda",
                        slug=self.slug,
                        meeting_id=meeting_id,
                        error=str(e)
                    )
                except (ValueError, KeyError, AttributeError) as e:
                    logger.warning(
                        "failed to parse HTML agenda",
                        vendor="novusagenda",
                        slug=self.slug,
                        meeting_id=meeting_id,
                        error=str(e)
                    )

            result = {
                "vendor_id": meeting_id,
                "title": meeting_type,
                "start": date_str,
                "packet_url": packet_url,
            }

            if agenda_url:
                result["agenda_url"] = agenda_url

            if minutes_url:
                result["minutes_url"] = minutes_url

            if items:
                result["items"] = items

            if meeting_status:
                result["meeting_status"] = meeting_status

            meetings.append(result)

        logger.info(
            "collected meetings in date range",
            vendor="novusagenda",
            slug=self.slug,
            count=len(meetings)
        )

        return meetings

    # NovusAgenda coversheets are server-rendered ASP.NET pages that can be
    # legitimately slow. Longer timeout + single retry prevents silent data loss.
    _COVERSHEET_TIMEOUT = aiohttp.ClientTimeout(total=45)
    _COVERSHEET_CONCURRENCY = 3
    _COVERSHEET_RETRY_DELAY = 2.0

    async def _fetch_coversheet_details(
        self, items: List[Dict[str, Any]], meeting_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch attachments and body text from CoverSheet.aspx detail pages.

        NovusAgenda hosts item documents behind CoverSheet pages. Each page
        contains AttachmentViewer.ashx links pointing to actual PDFs, plus
        the item's description/staff report as HTML body text.
        """
        sem = asyncio.Semaphore(self._COVERSHEET_CONCURRENCY)

        async def fetch_one(item: Dict[str, Any]) -> Dict[str, Any]:
            item_id = item.get("vendor_item_id")
            if not item_id:
                return item
            url = f"{self.base_url}/agendapublic/CoverSheet.aspx?ItemID={item_id}&MeetingID={meeting_id}"
            title = item.get("title", "unknown")
            async with sem:
                for attempt in range(2):
                    try:
                        response = await self._get(url, timeout=self._COVERSHEET_TIMEOUT)
                        html = await response.text()
                        attachments = self._parse_coversheet_attachments(html)
                        if attachments:
                            item["attachments"] = attachments
                        body_text = self._extract_coversheet_text(html)
                        if body_text:
                            item["body_text"] = body_text
                        return item
                    except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as e:
                        if attempt == 0:
                            logger.debug(
                                "coversheet timeout, retrying",
                                vendor="novusagenda",
                                slug=self.slug,
                                item_id=item_id,
                            )
                            await asyncio.sleep(self._COVERSHEET_RETRY_DELAY)
                            continue
                        logger.warning(
                            "coversheet fetch failed after retry",
                            vendor="novusagenda",
                            slug=self.slug,
                            item_id=item_id,
                            title=title,
                            error=str(e),
                        )
                    except Exception as e:
                        logger.warning(
                            "coversheet fetch failed",
                            vendor="novusagenda",
                            slug=self.slug,
                            item_id=item_id,
                            title=title,
                            error=str(e),
                        )
                        break
            return item

        return list(await asyncio.gather(*[fetch_one(item) for item in items]))

    def _parse_coversheet_attachments(self, html: str) -> List[Dict[str, str]]:
        """Extract attachment links from a CoverSheet.aspx page.

        Looks for AttachmentViewer.ashx links which are the standard
        NovusAgenda pattern for hosted documents.
        """
        soup = BeautifulSoup(html, "html.parser")
        attachments = []
        seen_ids = set()

        for link in soup.find_all("a", href=re.compile(r"AttachmentViewer\.ashx", re.IGNORECASE)):
            href = string_attr(link, "href")
            att_id_match = re.search(r"AttachmentID=(\d+)", href)
            if not att_id_match:
                continue
            att_id = att_id_match.group(1)
            if att_id in seen_ids:
                continue
            seen_ids.add(att_id)

            name = link.get_text(strip=True)
            if not name:
                parent = link.find_parent("td") or link.find_parent("div")
                if parent:
                    name = parent.get_text(strip=True)
            if not name:
                name = f"Attachment {att_id}"

            full_url = href if href.startswith("http") else f"{self.base_url}/agendapublic/{href}"
            file_type = "pdf" if ".pdf" in name.lower() or ".pdf" in href.lower() else "unknown"

            attachments.append({"name": name, "url": full_url, "type": file_type})

        return attachments

    def _extract_coversheet_text(self, html: str) -> str:
        """Extract body text from a CoverSheet.aspx page.

        NovusAgenda coversheets are ASP.NET pages with item descriptions,
        staff reports, and recommendations in #ns-ContentArea. The text
        is useful as-is for items that have no PDF attachments.
        """
        soup = BeautifulSoup(html, "html.parser")
        content = soup.find(id="ns-ContentArea")
        if not content:
            content = soup.find("body")
        if not content:
            return ""

        # Strip elements that add noise
        for tag in content.find_all(["script", "style", "input", "select", "button", "noscript"]):
            tag.decompose()
        # Strip attachment links -- those are handled separately
        for tag in content.find_all("a", href=re.compile(r"AttachmentViewer\.ashx", re.IGNORECASE)):
            tag.decompose()

        text = content.get_text(separator="\n", strip=True)
        # Collapse runs of blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Skip if too short to be meaningful (just a header or empty page)
        if len(text) < 50:
            return ""
        return text
