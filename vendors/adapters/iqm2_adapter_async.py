"""
Async IQM2 Adapter - HTML scraping for IQM2 platform (Granicus subsidiary)

Cities using IQM2: Boise ID, Santa Monica CA, Cambridge MA, Buffalo NY, and 40+ others
"""

import asyncio
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from urllib.parse import urljoin
import aiohttp

from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector
from bs4 import BeautifulSoup


class AsyncIQM2Adapter(AsyncBaseAdapter):
    """Async adapter for IQM2 platform with item-level extraction."""

    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="iqm2", metrics=metrics)
        self.base_url = f"https://{self.slug}.iqm2.com"

        # Try multiple calendar URL patterns (IQM2 sites vary)
        self.calendar_url_patterns = [
            f"{self.base_url}/Citizens",
            f"{self.base_url}/Citizens/Calendar.aspx",
            f"{self.base_url}/Citizens/Default.aspx",
        ]

        logger.info("initialized async IQM2 adapter", vendor="iqm2", slug=self.slug)

    def _dedupe_attachments(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Deduplicate attachments by file ID extracted from URL.

        IQM2 URLs look like: FileOpen.aspx?Type=4&ID=12345 or FileOpen.aspx?Type=4&ID=12345&MeetingID=999
        We extract the ID parameter and keep only the first occurrence of each unique ID.
        """
        seen_ids = set()
        unique_attachments = []

        for attachment in attachments:
            url = attachment.get("url", "")
            # Extract the file ID from the URL (ignoring MeetingID parameter)
            id_match = re.search(r'[?&]ID=(\d+)', url)
            if id_match:
                file_id = id_match.group(1)
                if file_id not in seen_ids:
                    seen_ids.add(file_id)
                    unique_attachments.append(attachment)
            else:
                # No ID found, keep it (shouldn't happen but be safe)
                unique_attachments.append(attachment)

        return unique_attachments

    def _extract_row_minutes_url(self, row) -> Optional[str]:
        """Extract the minutes document URL from a calendar listing row.

        Only FileOpen.aspx/FileView.ashx hrefs are documents; "Minutes" links
        that point at Detail_Meeting.aspx are viewer pages and are skipped.
        """
        for link in row.find_all("a", href=True):
            if "minutes" not in link.get_text(strip=True).lower():
                continue
            href = link["href"]
            if href and re.search(r"File(?:Open\.aspx|View\.ashx)", href, re.IGNORECASE):
                # Row hrefs are relative to the /Citizens/ calendar pages
                return urljoin(f"{self.base_url}/Citizens/", href)
        return None

    async def _fetch_meetings_impl(
        self, days_back: int = 14, days_forward: int = 14
    ) -> List[Dict[str, Any]]:
        """Scrape meetings from IQM2 calendar page with date filtering."""
        # Try each calendar URL pattern until one works
        soup = None
        working_url = None
        meeting_rows = []

        for calendar_url in self.calendar_url_patterns:
            try:
                logger.info("trying calendar URL", vendor="iqm2", slug=self.slug, url=calendar_url)
                response = await self._get(calendar_url)
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')

                # Check if we got a valid calendar page with meetings
                meeting_rows = soup.find_all("div", class_="MeetingRow")
                if meeting_rows:
                    working_url = calendar_url
                    logger.info(
                        "found meetings on calendar",
                        vendor="iqm2",
                        slug=self.slug,
                        url=calendar_url,
                        count=len(meeting_rows)
                    )
                    break
                else:
                    logger.debug(
                        "no meetings found at URL",
                        vendor="iqm2",
                        slug=self.slug,
                        url=calendar_url
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.debug(
                    "failed to fetch calendar URL",
                    vendor="iqm2",
                    slug=self.slug,
                    url=calendar_url,
                    error=str(e)
                )
                continue

        if not soup or not working_url or not meeting_rows:
            logger.error(
                "could not find working calendar URL",
                vendor="iqm2",
                slug=self.slug,
                tried_urls=self.calendar_url_patterns
            )
            return []

        # Date range filter
        start_date, end_date = self._date_range(days_back, days_forward)

        meetings = []

        for row in meeting_rows:
            # Check if meeting is cancelled
            cancelled_elem = row.find("span", class_="MeetingCancelled")
            if cancelled_elem:
                continue

            # Extract meeting link
            link_elem = row.find("a", href=re.compile(r"Detail_Meeting\.aspx\?ID="))
            if not link_elem:
                continue

            meeting_url = urljoin(self.base_url, link_elem["href"])
            meeting_id_match = re.search(r"ID=(\d+)", meeting_url)
            if not meeting_id_match:
                continue

            meeting_id = meeting_id_match.group(1)

            # Extract date/time from link text
            # Format: "Jan 28, 2025 5:30 PM"
            datetime_text = link_elem.get_text(strip=True)
            try:
                meeting_dt = datetime.strptime(datetime_text, "%b %d, %Y %I:%M %p")
            except ValueError:
                logger.warning(
                    "could not parse datetime",
                    vendor="iqm2",
                    slug=self.slug,
                    datetime_text=datetime_text
                )
                continue

            # Filter by date range
            if not (start_date <= meeting_dt <= end_date):
                continue

            # Extract title from row details
            title_elem = row.find("div", class_="RowDetails")
            title = title_elem.get_text(strip=True) if title_elem else "Meeting"

            minutes_url = self._extract_row_minutes_url(row)

            if self._minutes_discovery_only:
                if minutes_url:
                    meetings.append({
                        "vendor_id": meeting_id,
                        "title": title,
                        "start": meeting_dt.isoformat(),
                        "minutes_url": minutes_url,
                    })
                continue

            # Fetch Detail_Meeting page to extract items
            logger.info(
                "fetching meeting details",
                vendor="iqm2",
                slug=self.slug,
                meeting_id=meeting_id
            )
            meeting_data = await self._fetch_meeting_details(
                meeting_id, meeting_dt, title, minutes_url=minutes_url
            )

            if meeting_data:
                meetings.append(meeting_data)

        logger.info(
            "collected meetings in date range",
            vendor="iqm2",
            slug=self.slug,
            count=len(meetings)
        )

        return meetings

    async def _fetch_meeting_details(
        self,
        meeting_id: str,
        meeting_dt: datetime,
        title: str,
        minutes_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch Detail_Meeting page and extract agenda items."""
        detail_url = f"{self.base_url}/Citizens/Detail_Meeting.aspx?ID={meeting_id}"
        response = await self._get(detail_url)
        html = await response.text()
        soup = BeautifulSoup(html, 'html.parser')

        # Extract items from MeetingDetail table
        items = await self._parse_agenda_items(soup, meeting_id, detail_url)

        # Extract packet URL if available
        packet_url = None
        packet_link = soup.find("a", id=re.compile(r"hlFullAgendaFile"))
        if packet_link and packet_link.get("href"):
            packet_url = urljoin(self.base_url, packet_link["href"])

        meeting_data = {
            "vendor_id": meeting_id,
            "title": title,
            "start": meeting_dt.isoformat(),
            "agenda_url": detail_url,  # HTML agenda source (Detail_Meeting.aspx)
            "packet_url": packet_url,  # Full PDF packet (optional)
            "minutes_url": minutes_url,  # Minutes document from listing row (optional)
            "items": items,
        }

        logger.info(
            "extracted items from meeting",
            vendor="iqm2",
            slug=self.slug,
            meeting_id=meeting_id,
            item_count=len(items)
        )

        return meeting_data

    async def _fetch_matter_metadata(self, legifile_id: str) -> Dict[str, Any]:
        """Fetch matter_type, sponsors, department, attachments from Detail_LegiFile page."""
        detail_url = f"{self.base_url}/Citizens/Detail_LegiFile.aspx?ID={legifile_id}"

        try:
            response = await self._get(detail_url)
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            # Find the LegiFileInfo table
            info_table = soup.find("table", id="tblLegiFileInfo")
            if not info_table:
                return {}

            metadata = {}

            # Parse table rows for metadata
            rows = info_table.find_all("tr")
            for row in rows:
                cells = row.find_all(["th", "td"])

                # Process cell pairs (label: value)
                i = 0
                while i < len(cells) - 1:
                    label = cells[i].get_text(strip=True).lower().replace(":", "")
                    value = cells[i + 1].get_text(strip=True)

                    if "category" in label and value:
                        metadata["matter_type"] = value
                    elif "sponsor" in label and value:
                        # Split sponsors by comma or semicolon
                        sponsors = [s.strip() for s in re.split(r'[,;]', value) if s.strip()]
                        metadata["sponsors"] = sponsors
                    elif "department" in label and value:
                        metadata["department"] = value

                    i += 2

            # Extract attachments from Attachments section
            # Look for divAttachments or similar container
            attachments = []
            attachment_links = soup.find_all("a", href=re.compile(r'FileOpen\.aspx'))
            for link in attachment_links:
                # Extract attachment name and URL
                attachment_name = link.get_text(strip=True)
                attachment_url = urljoin(detail_url, link.get("href", ""))

                if attachment_name and attachment_url:
                    # Determine file type from URL or name
                    file_type = "pdf"
                    if ".doc" in attachment_url.lower() or ".doc" in attachment_name.lower():
                        file_type = "doc"
                    elif ".xls" in attachment_url.lower() or ".xls" in attachment_name.lower():
                        file_type = "xls"

                    attachments.append({
                        "name": attachment_name,
                        "url": attachment_url,
                        "type": file_type
                    })

            if attachments:
                metadata["attachments"] = attachments
                logger.debug(
                    "found attachments for legifile",
                    vendor="iqm2",
                    slug=self.slug,
                    legifile_id=legifile_id,
                    count=len(attachments)
                )

            logger.debug(
                "fetched matter metadata",
                vendor="iqm2",
                slug=self.slug,
                legifile_id=legifile_id,
                metadata=metadata
            )
            return metadata

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(
                "failed to fetch matter metadata",
                vendor="iqm2",
                slug=self.slug,
                legifile_id=legifile_id,
                error=str(e)
            )
            return {}
        except (ValueError, AttributeError, KeyError) as e:
            logger.warning(
                "failed to parse matter metadata",
                vendor="iqm2",
                slug=self.slug,
                legifile_id=legifile_id,
                error=str(e)
            )
            return {}

    async def _parse_agenda_items(
        self, soup: BeautifulSoup, meeting_id: str, base_url: str
    ) -> List[Dict[str, Any]]:
        """Parse agenda items from MeetingDetail table."""
        items = []

        table = soup.find("table", id="MeetingDetail")
        if not table:
            logger.warning("no MeetingDetail table found", vendor="iqm2", slug=self.slug)
            return items

        rows = table.find_all("tr")

        current_section = None
        current_item = None
        item_counter = 0

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            # Check if this is a section header (strong tag in first cell)
            first_cell_strong = cells[0].find("strong")
            title_cell = cells[1] if len(cells) > 1 else None

            # Section header
            if first_cell_strong and title_cell:
                title_strong = title_cell.find("strong")
                if title_strong:
                    section_text = title_strong.get_text(strip=True)
                    if section_text:
                        current_section = section_text
                        logger.debug(
                            "found section",
                            vendor="iqm2",
                            slug=self.slug,
                            section=current_section
                        )
                    continue

            # Pattern 2: nested items (2 empty cells before Num)
            if len(cells) >= 4 and not cells[0].get_text(strip=True) and not cells[1].get_text(strip=True):
                num_cell = cells[2]
                title_cell = cells[3]

                if num_cell.get("class") == ["Num"]:
                    num_text = num_cell.get_text(strip=True)

                    if re.match(r"^[0-9]+\.\s*$", num_text):
                        title_link = title_cell.find("a", href=lambda x: x and "Detail_LegiFile.aspx" in x)

                        if current_item:
                            items.append(current_item)

                        item_counter += 1
                        item_number = num_text.strip()

                        if title_link:
                            item_title = title_link.get_text(strip=True)
                            href = title_link.get("href", "")
                            id_match = re.search(r"[?&]ID=(\d+)", href)
                            legifile_id = id_match.group(1) if id_match else None
                        else:
                            item_title = title_cell.get_text(strip=True)
                            legifile_id = None

                        current_item = {
                            "vendor_item_id": legifile_id,  # Raw vendor ID, orchestrator generates final item_id
                            "title": item_title,
                            "sequence": item_counter,
                            "agenda_number": item_number,
                            "section": current_section,
                            "description": "",
                            "attachments": [],
                        }

                        if legifile_id:
                            current_item["matter_id"] = legifile_id

                            matter_file = None
                            if " / " in item_title:
                                matter_file = item_title.split(" / ", 1)[0].strip()
                            elif ":" in item_title:
                                prefix = item_title.split(":", 1)[0].strip()
                                matter_file = re.sub(r'\s+#\s*', '-', prefix)
                                matter_file = re.sub(r'\s+', '-', matter_file)

                            current_item["matter_file"] = matter_file if matter_file else legifile_id

                            metadata = await self._fetch_matter_metadata(legifile_id)
                            if metadata.get("matter_type"):
                                current_item["matter_type"] = metadata["matter_type"]
                            if metadata.get("sponsors"):
                                current_item["sponsors"] = metadata["sponsors"]
                            if metadata.get("attachments"):
                                current_item["attachments"].extend(metadata["attachments"])

                        continue

            # Pattern 1: top-level items (1 empty cell before Num)
            if len(cells) >= 3:
                num_cell = cells[1]
                title_cell = cells[2]

                if num_cell.get("class") == ["Num"]:
                    num_text = num_cell.get_text(strip=True)

                    title_link = title_cell.find("a", href=lambda x: x and "Detail_LegiFile.aspx" in x)

                    # Letter/number or empty Num with LegiFile link
                    if re.match(r"^[A-Z0-9]+\.\s*$", num_text) or (not num_text and title_link):
                        # Skip section headers (strong tags, no LegiFile link)
                        title_strong = title_cell.find("strong")
                        if title_strong and not title_link:
                            continue

                        # Empty Num cell: extract matter number from title
                        if not num_text and title_link:
                            item_title_full = title_link.get_text(strip=True)
                            matter_num_match = re.match(r"^([A-Z]+\s+\d+\s+#\d+)\s*:", item_title_full)
                            num_text = matter_num_match.group(1) if matter_num_match else str(item_counter + 1)

                        legifile_id = None
                        if title_link:
                            href = title_link.get("href", "")
                            id_match = re.search(r"[?&]ID=(\d+)", href)
                            if id_match:
                                legifile_id = id_match.group(1)

                        if current_item:
                            items.append(current_item)

                        item_counter += 1
                        item_number = num_text.strip()

                        if title_link:
                            item_title = title_link.get_text(strip=True)
                        else:
                            item_title = title_cell.get_text(strip=True)

                        current_item = {
                            "vendor_item_id": legifile_id,  # Raw vendor ID, orchestrator generates final item_id
                            "title": item_title,
                            "sequence": item_counter,
                            "agenda_number": item_number,
                            "section": current_section,
                            "description": "",
                            "attachments": [],
                        }

                        if legifile_id:
                            current_item["matter_id"] = legifile_id

                            # Extract clean matter_file (case number preferred over full title)
                            matter_file = None

                            # Case number: DRH25-00335, CUP25-00022, etc.
                            case_match = re.search(r'\b([A-Z]{2,5}\d{2}-\d{4,5})\b', item_title)
                            if case_match:
                                matter_file = case_match.group(1)
                            else:
                                # Cambridge-style: "COF 2025 #141" -> "COF-2025-141"
                                compound_match = re.match(r'^([A-Z]{2,5})\s+(\d{4})\s+#(\d+)', item_title)
                                if compound_match:
                                    matter_file = f"{compound_match.group(1)}-{compound_match.group(2)}-{compound_match.group(3)}"
                                elif " / " in item_title:
                                    matter_file = item_title.split(" / ", 1)[0].strip()
                                elif ":" in item_title:
                                    prefix = item_title.split(":", 1)[0].strip()
                                    matter_file = re.sub(r'\s+#\s*', '-', prefix)
                                    matter_file = re.sub(r'\s+', '-', matter_file)

                            current_item["matter_file"] = matter_file if matter_file else legifile_id

                            metadata = await self._fetch_matter_metadata(legifile_id)
                            if metadata.get("matter_type"):
                                current_item["matter_type"] = metadata["matter_type"]
                            if metadata.get("sponsors"):
                                current_item["sponsors"] = metadata["sponsors"]
                            if metadata.get("attachments"):
                                current_item["attachments"].extend(metadata["attachments"])

                        continue

                if title_cell.get("class") == ["Comments"]:
                    if current_item:
                        desc_text = title_cell.get_text(strip=True)
                        current_item["description"] = desc_text
                    continue

            if len(cells) >= 4 and (
                not cells[0].get_text(strip=True)
                and not cells[1].get_text(strip=True)
                and cells[2].get("class") == ["Num"]
            ):
                num_cell = cells[2]
                title_cell = cells[3]
                num_text = num_cell.get_text(strip=True)

                if re.match(r"^[a-z]\.\s*$", num_text) or num_cell.find("img"):
                    if current_item:
                        pdf_link = title_cell.find("a", href=True)
                        if pdf_link:
                            pdf_url = urljoin(base_url, pdf_link["href"])
                            pdf_name = pdf_link.get_text(strip=True)

                            file_type = "pdf"
                            if ".doc" in pdf_url.lower() or ".doc" in pdf_name.lower():
                                file_type = "doc"
                            elif ".xls" in pdf_url.lower() or ".xls" in pdf_name.lower():
                                file_type = "xls"

                            current_item["attachments"].append(
                                {"name": pdf_name, "url": pdf_url, "type": file_type}
                            )

        if current_item:
            items.append(current_item)

        # Deduplicate attachments for each item (same files appear in both
        # Detail_LegiFile page and inline agenda with different URL formats)
        for item in items:
            if item.get("attachments"):
                original_count = len(item["attachments"])
                item["attachments"] = self._dedupe_attachments(item["attachments"])
                if len(item["attachments"]) < original_count:
                    logger.debug(
                        "deduped attachments",
                        vendor="iqm2",
                        slug=self.slug,
                        item_title=item.get("title", "")[:50],
                        original=original_count,
                        deduped=len(item["attachments"])
                    )

        logger.info(
            "parsed items from MeetingDetail table",
            vendor="iqm2",
            slug=self.slug,
            item_count=len(items)
        )

        return items
