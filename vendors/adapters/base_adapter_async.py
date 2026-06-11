"""Async Base Adapter - Shared HTTP, date parsing, PDF discovery for vendor adapters."""

import asyncio
import hashlib
import json
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import aiohttp

from config import config, get_logger
from pipeline.protocols import MetricsCollector, NullMetrics
from vendors.adapters.parsers.agenda_chunker import _normalize_link_url
from vendors.adapters.parsers.router import (
    ChunkResult,
    DOWNLOAD_FAILED,
    ENGINE_ERROR,
    MIN_PDF_BYTES,
    TOO_SMALL,
    Attempt,
    chunk_pdf,
    get_city_hint,
    ladder_for_force_method,
    set_city_hint,
    summarize_runs,
)
from vendors.rate_limiter_async import get_rate_limiter
from vendors.session_manager_async import AsyncSessionManager
from exceptions import VendorHTTPError

logger = get_logger(__name__).bind(component="vendor")


def _get_pdf_link_display_text(page, link_rect) -> str:
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


@dataclass
class FetchResult:
    """Result of fetch_meetings() - distinguishes success from failure.

    Allows callers to detect adapter failures vs "city has no meetings".
    """
    meetings: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    error_type: Optional[str] = None


class AsyncBaseAdapter:
    """Async base adapter. Subclasses implement _fetch_meetings_impl().

    Contract:
    - Config errors raise in __init__
    - fetch_meetings() returns FetchResult with success=True/False
    - Callers can distinguish "0 meetings" from "adapter failed"
    """

    def __init__(
        self,
        city_slug: str,
        vendor: str,
        metrics: Optional[MetricsCollector] = None
    ):
        if not city_slug:
            raise ValueError(f"city_slug required for {vendor}")

        self.slug = city_slug
        self.vendor = vendor
        self.metrics = metrics or NullMetrics()
        # chunker cascade audits collected during a fetch, keyed by vendor_id;
        # fetch_meetings() stamps them onto the outgoing meeting dicts
        self._chunk_audits: Dict[str, List[Dict[str, Any]]] = {}

        logger.info("initialized async adapter", vendor=vendor, city_slug=city_slug)

    async def _get_session(self) -> aiohttp.ClientSession:
        return await AsyncSessionManager.get_session(self.vendor)

    # Transient aiohttp errors worth retrying. ClientConnectionError is the
    # umbrella for connection-level failures: ClientOSError (errno 104 etc.),
    # ClientConnectorError, ServerDisconnectedError, ServerTimeoutError, etc.
    # Excludes ClientPayloadError and TLS verification failures (permanent).
    _RETRYABLE_CLIENT_ERRORS = (aiohttp.ClientConnectionError,)
    _MAX_REQUEST_ATTEMPTS = 3

    async def _request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make async HTTP request with rate limiting, retries, and error handling.

        Every request gates through the shared per-vendor rate limiter, so
        adapter-internal concurrency (gather/_bounded_gather) naturally
        serializes at the configured cadence rather than firing in parallel.

        Retries up to _MAX_REQUEST_ATTEMPTS times on transient failures:
        - TCP resets / disconnects / connector errors (ClientOSError covers
          ConnectionResetError aka errno 104)
        - asyncio.TimeoutError
        - 5xx responses other than 503-with-Retry-After (which the rate
          limiter already defers)
        Permanent 4xx errors (auth, not-found, bad-request) raise immediately.
        """
        session = await self._get_session()

        if "timeout" not in kwargs:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=config.VENDOR_HTTP_TIMEOUT)

        # Legistar API: prefer JSON over XML
        if 'webapi.legistar.com' in url:
            headers = kwargs.get('headers', {})
            if 'Accept' not in headers:
                headers = headers.copy() if headers else {}
                headers['Accept'] = 'application/json, application/xml;q=0.9, */*;q=0.8'
                kwargs['headers'] = headers

        # Granicus has SSL cert issues on S3 redirects (confidence: 8/10)
        if self.vendor == "granicus" or "granicus.com" in url or "granicus_production_attachments" in url:
            kwargs["ssl"] = False

        last_error: Optional[BaseException] = None

        for attempt in range(self._MAX_REQUEST_ATTEMPTS):
            # Per-request politeness gate. Re-enter on every attempt so the
            # rate limiter spaces retries too.
            await get_rate_limiter().wait_if_needed(self.vendor)

            start_time = time.time()
            try:
                logger.debug("vendor request", vendor=self.vendor, slug=self.slug, method=method, url=url[:100], attempt=attempt + 1)
                response = await session.request(method, url, **kwargs)
                duration = time.time() - start_time

                logger.debug(
                    "vendor response",
                    vendor=self.vendor,
                    slug=self.slug,
                    status_code=response.status,
                    content_length=response.headers.get('content-length', 'unknown'),
                    content_type=response.headers.get('content-type', 'unknown'),
                    duration_seconds=round(duration, 2),
                )

                if response.status >= 400:
                    # Honor Retry-After when the server explicitly tells us how
                    # long to back off. Applies to 429 (rate-limited) and 503
                    # (service unavailable).
                    if response.status in (429, 503):
                        retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                        if retry_after is not None:
                            await get_rate_limiter().respect_retry_after(self.vendor, retry_after)

                    error_body = await response.text()
                    self.metrics.vendor_requests.labels(vendor=self.vendor, status=f"http_{response.status}").inc()
                    err = VendorHTTPError(
                        f"HTTP {response.status} error",
                        vendor=self.vendor,
                        status_code=response.status,
                        url=url,
                        city_slug=self.slug,
                    )
                    self.metrics.record_error(component="vendor", error=err)
                    logger.error(
                        "vendor http error",
                        vendor=self.vendor,
                        slug=self.slug,
                        status_code=response.status,
                        url=url[:100],
                        error_body=error_body[:500] if error_body else None,
                        duration_seconds=round(duration, 2),
                        attempt=attempt + 1,
                    )

                    # 5xx are usually transient (server bug, gateway hiccup,
                    # tenant-level throttle that didn't honor Retry-After).
                    # 4xx are application errors -- retrying won't help.
                    if 500 <= response.status < 600 and attempt < self._MAX_REQUEST_ATTEMPTS - 1:
                        last_error = err
                        await self._sleep_backoff(attempt)
                        continue
                    raise err

                self.metrics.vendor_requests.labels(vendor=self.vendor, status="success").inc()
                self.metrics.vendor_request_duration.labels(vendor=self.vendor).observe(duration)
                return response

            except asyncio.TimeoutError as e:
                duration = time.time() - start_time
                self.metrics.vendor_requests.labels(vendor=self.vendor, status="timeout").inc()
                self.metrics.record_error(component="vendor", error=e)
                logger.warning(
                    "vendor request timeout",
                    vendor=self.vendor,
                    slug=self.slug,
                    url=url[:100],
                    duration_seconds=round(duration, 2),
                    attempt=attempt + 1,
                )
                last_error = e
                if attempt < self._MAX_REQUEST_ATTEMPTS - 1:
                    await self._sleep_backoff(attempt)
                    continue
                raise VendorHTTPError(f"Request timeout after {duration:.1f}s", vendor=self.vendor, url=url, city_slug=self.slug) from e

            except self._RETRYABLE_CLIENT_ERRORS as e:
                duration = time.time() - start_time
                self.metrics.vendor_requests.labels(vendor=self.vendor, status="error").inc()
                self.metrics.record_error(component="vendor", error=e)
                logger.warning(
                    "vendor request transient failure",
                    vendor=self.vendor,
                    slug=self.slug,
                    url=url[:100],
                    error=str(e),
                    error_type=type(e).__name__,
                    duration_seconds=round(duration, 2),
                    attempt=attempt + 1,
                )
                last_error = e
                if attempt < self._MAX_REQUEST_ATTEMPTS - 1:
                    await self._sleep_backoff(attempt)
                    continue
                raise VendorHTTPError(f"Request failed: {e}", vendor=self.vendor, url=url, city_slug=self.slug) from e

            except aiohttp.ClientError as e:
                # Non-retryable aiohttp errors (TLS, payload, redirect).
                duration = time.time() - start_time
                self.metrics.vendor_requests.labels(vendor=self.vendor, status="error").inc()
                self.metrics.record_error(component="vendor", error=e)
                logger.error("vendor request failed", vendor=self.vendor, slug=self.slug, url=url[:100], error=str(e), error_type=type(e).__name__, duration_seconds=round(duration, 2))
                raise VendorHTTPError(f"Request failed: {e}", vendor=self.vendor, url=url, city_slug=self.slug) from e

        # Should be unreachable -- the final attempt either returned or raised.
        # Kept as a defensive fallback so the type checker sees a terminating path.
        raise VendorHTTPError(
            f"Request failed after {self._MAX_REQUEST_ATTEMPTS} attempts: {last_error}",
            vendor=self.vendor,
            url=url,
            city_slug=self.slug,
        )

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        """Exponential backoff with jitter: attempt 0 -> ~1s, 1 -> ~3s, 2 -> ~7s."""
        delay = (2 ** attempt) + random.uniform(0, 1)
        await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(value: Optional[str]) -> Optional[float]:
        """Parse a Retry-After header (seconds OR HTTP-date). None on bad input."""
        if not value:
            return None
        value = value.strip()
        try:
            return float(value)
        except ValueError:
            pass
        # HTTP-date form: defer to email.utils since aiohttp doesn't expose a parser
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(value)
            if dt is None:
                return None
            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
            delta = (dt - now).total_seconds()
            return max(0.0, delta)
        except (TypeError, ValueError):
            return None

    async def _get(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """GET request. Raises VendorHTTPError on failure."""
        return await self._request("GET", url, **kwargs)

    async def _post(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """POST request. Raises VendorHTTPError on failure."""
        return await self._request("POST", url, **kwargs)

    async def _get_json(self, url: str, **kwargs) -> Any:
        """GET request, parse JSON. Raises VendorHTTPError on failure."""
        response = await self._get(url, **kwargs)
        try:
            return await response.json()
        except aiohttp.ContentTypeError as e:
            # Some vendors serve JSON with wrong content-type (e.g. text/html)
            # Try parsing the body directly before giving up
            text = await response.text()
            try:
                return json.loads(text)
            except (ValueError, TypeError):
                logger.error("vendor json parse failed", vendor=self.vendor, slug=self.slug, url=url[:100], content_type=response.headers.get('content-type', 'unknown'), body_preview=text[:200] if text else None)
                raise VendorHTTPError(f"Expected JSON but got {response.headers.get('content-type', 'unknown')}", vendor=self.vendor, url=url, city_slug=self.slug) from e
        except ValueError as e:
            try:
                text = await response.text()
            except aiohttp.ClientError:
                text = "(unable to read body)"
            logger.error("vendor json parse failed", vendor=self.vendor, slug=self.slug, url=url[:100], error=str(e), body_preview=text[:200] if text else None)
            raise VendorHTTPError(f"JSON parse failed: {e}", vendor=self.vendor, url=url, city_slug=self.slug) from e

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse vendor date formats. Returns naive datetime or None."""
        if not date_str:
            return None

        date_str = date_str.strip()

        # ISO 8601 first
        if 'T' in date_str or date_str.count('-') >= 2:
            try:
                dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                return dt.replace(tzinfo=None)
            except ValueError:
                pass

        formats = [
            "%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p",
            "%b %d, %Y %H:%M", "%B %d, %Y %H:%M", "%m/%d/%Y %H:%M",
            "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y",
            "%b %d %Y", "%B %d %Y",  # comma-less (civicengage historical agendas)
            "%B %d, %Y at %I:%M %p", "%A, %B %d, %Y @ %I:%M %p",
        ]

        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except (ValueError, AttributeError):
                continue

        logger.warning("failed to parse date", date_str=date_str, vendor=self.vendor)
        return None

    @staticmethod
    def _load_vendor_config(config_file: str, required: bool = False) -> Dict[str, Any]:
        """Load a JSON vendor config file. Returns {} if optional and missing."""
        if not os.path.exists(config_file):
            if required:
                raise FileNotFoundError(f"Vendor config not found: {config_file}")
            return {}
        try:
            with open(config_file, "r") as f:
                return json.load(f)
        except Exception:
            if required:
                raise
            return {}

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags, decode entities, normalize whitespace."""
        if not text:
            return ""
        # Replace <br> variants with space
        text = re.sub(r'<br\s*/?>', ' ', text, flags=re.IGNORECASE)
        # Remove all other HTML tags
        text = re.sub(r'<[^>]+>', '', text)
        # Decode common HTML entities
        for entity, char in (
            ("&amp;", "&"), ("&#038;", "&"),
            ("&lt;", "<"), ("&gt;", ">"),
            ("&quot;", '"'), ("&#39;", "'"),
            ("&nbsp;", " "),
            ("&#8211;", "\u2013"), ("&#8212;", "\u2014"),
            ("&#8217;", "\u2019"),
        ):
            text = text.replace(entity, char)
        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _find_pdf_in_html(self, html: str, base_url: str) -> Optional[str]:
        """Find first PDF link in HTML, return absolute URL or None."""
        soup = BeautifulSoup(html, 'html.parser')
        for link in soup.find_all('a', href=True):
            href = link['href']  # type: ignore[index]
            if '.pdf' in href.lower():  # type: ignore[union-attr]
                return urljoin(base_url, href)  # type: ignore[arg-type]
        return None

    def _generate_fallback_vendor_id(self, title: str, date: Optional[datetime], meeting_type: Optional[str] = None) -> str:
        """Generate stable 12-char hash for vendors without native meeting IDs.

        Confidence: 8/10 - Includes full datetime for same-day meetings.
        Uses SHA256 with 12 hex chars (48 bits) for lower collision risk.
        """
        # Include full datetime (hour/minute) to distinguish same-day meetings
        date_str = date.strftime("%Y%m%dT%H%M") if date else "nodate"
        type_str = f"_{meeting_type}" if meeting_type else ""
        # Normalize title to avoid whitespace variations
        normalized_title = " ".join(title.split()).lower()
        id_string = f"{self.slug}_{date_str}_{normalized_title}{type_str}"
        # SHA256 with 12 chars for ~2^48 combinations (vs MD5's 2^32 with 8 chars)
        return hashlib.sha256(id_string.encode()).hexdigest()[:12]

    def _parse_meeting_status(self, title: str, date_str: Optional[str] = None) -> Optional[str]:
        """Detect cancelled/postponed/revised status from title or date string."""
        status_keywords = [
            ("CANCEL", "cancelled"), ("POSTPONE", "postponed"), ("DEFER", "deferred"),
            ("RESCHEDULE", "rescheduled"), ("REVISED", "revised"), ("AMENDMENT", "revised"), ("UPDATED", "revised"),
        ]
        status = None
        for text in [title, date_str]:
            if not text:
                continue
            text_upper = str(text).upper()
            for keyword, label in status_keywords:
                if keyword in text_upper:
                    status = label
        return status

    def _validate_meeting(self, meeting: Dict[str, Any]) -> bool:
        """Check meeting has vendor_id, title, start. Returns False if missing."""
        required = {"vendor_id", "title", "start"}
        missing = required - set(meeting.keys())
        if missing:
            logger.warning("meeting missing required fields", vendor=self.vendor, slug=self.slug, missing=list(missing), title=str(meeting.get("title", "unknown"))[:50])
            return False
        return True

    # ------------------------------------------------------------------
    # Domain discovery
    # ------------------------------------------------------------------

    def _get_candidate_base_urls(self) -> List[str]:
        """Return candidate base URLs to probe. Override to add vendor-specific domains."""
        slug = self.slug
        candidates = [
            f"https://www.{slug}.gov",
            f"https://www.{slug}.org",
            f"https://{slug}.gov",
            f"https://{slug}.org",
        ]
        if "." in slug:
            candidates.insert(0, f"https://www.{slug}.gov")
            candidates.insert(1, f"https://{slug}.gov")
        return candidates

    async def _discover_base_url(
        self,
        probe_path: str,
        validate=None,
    ) -> Optional[str]:
        """Discover working base URL by probing candidates.

        Args:
            probe_path: path to append to each candidate (e.g. "/wp-json/wp/v2/meetings?per_page=1")
            validate: async or sync callable(response) -> bool. Defaults to checking status 200.
        """
        for base_url in self._get_candidate_base_urls():
            test_url = f"{base_url}{probe_path}"
            try:
                response = await self._get(test_url)
                if validate:
                    if asyncio.iscoroutinefunction(validate):
                        ok = await validate(response)
                    else:
                        ok = validate(response)
                    if not ok:
                        continue
                logger.info("discovered site", vendor=self.vendor, slug=self.slug, base_url=base_url)
                return base_url
            except Exception:
                continue

        logger.warning("could not discover domain", vendor=self.vendor, slug=self.slug)
        return None

    # ------------------------------------------------------------------
    # Concurrency helper
    # ------------------------------------------------------------------

    async def _bounded_gather(
        self,
        coros,
        max_concurrent: int = 5,
        return_exceptions: bool = True,
    ):
        """Run coroutines concurrently with a semaphore bound.

        Returns list of results (or exceptions if return_exceptions=True).
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _limited(coro):
            async with semaphore:
                return await coro

        return await asyncio.gather(
            *[_limited(c) for c in coros],
            return_exceptions=return_exceptions,
        )

    # ------------------------------------------------------------------
    # PDF chunking: agenda (url) -> packet (toc) fallback chain
    # ------------------------------------------------------------------

    async def _chunk_agenda_then_packet(
        self,
        agenda_url: Optional[str] = None,
        packet_url: Optional[str] = None,
        vendor_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Two-step PDF chunking: try URL parsing on the agenda, then TOC on the packet.

        Agenda PDFs are short documents with hyperlinks to staff reports -- URL
        parsing extracts those links as attachments.  Packet PDFs are compiled
        documents with bookmark trees -- TOC parsing splits by page ranges and
        extracts embedded memo content.

        For URL-parsed items, runs a 2nd pass: downloads each attachment PDF
        and extracts embedded links (e.g. staff report cover sheets that link
        to the actual contracts/exhibits on Legistar S3).
        """
        text_fallback: List[Dict[str, Any]] = []
        if agenda_url:
            result = await self._chunk_packet_pdf(agenda_url, vendor_id, ladder="agenda")
            if result.items:
                # Only return attachment-bearing items directly — but keep
                # attachment-less ones (flat-text agendas carry their content
                # in body_text) as a last resort if the packet also fails.
                items = await self._resolve_sub_attachments(result.items, vendor_id)
                if any(it.get("attachments") for it in items):
                    return [it for it in items if it.get("attachments")]
                text_fallback = [it for it in items if it.get("body_text")]

        if packet_url:
            result = await self._chunk_packet_pdf(packet_url, vendor_id, ladder="packet")
            if result.items:
                return result.items

        return text_fallback

    async def _chunk_pdf_bytes(
        self,
        pdf_bytes: bytes,
        vendor_id: Optional[str] = None,
        ladder: str = "auto",
    ) -> ChunkResult:
        """Run the chunker cascade on raw PDF bytes. Returns full ChunkResult.

        Routing policy lives in router.LADDERS; every rung attempt and any
        terminal failure reason ends up in the result's audit trail.
        """
        if len(pdf_bytes) < MIN_PDF_BYTES:
            result = ChunkResult(failure_reason=TOO_SMALL, ladder=ladder)
            self._record_chunk_audit(vendor_id, result)
            return result

        hint = get_city_hint(self.vendor, self.slug, ladder)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(pdf_bytes)

            result = await asyncio.to_thread(chunk_pdf, tmp_path, ladder, hint)

        except Exception as e:
            result = ChunkResult(failure_reason=ENGINE_ERROR, ladder=ladder)
            result.attempts.append(
                Attempt(rung="cascade", failure_reason=ENGINE_ERROR,
                        error=f"{type(e).__name__}: {e}")
            )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        self._record_chunk_audit(vendor_id, result)

        if result.winning_rung:
            set_city_hint(self.vendor, self.slug, ladder, result.winning_rung)

        if result.items:
            logger.info(
                "chunker extracted items from pdf",
                vendor=self.vendor,
                slug=self.slug,
                vendor_id=vendor_id,
                item_count=len(result.items),
                parse_method=result.parse_method,
                winning_rung=result.winning_rung,
                ladder=ladder,
            )
        else:
            logger.warning(
                "chunker found no items",
                vendor=self.vendor,
                slug=self.slug,
                vendor_id=vendor_id,
                failure_reason=result.failure_reason,
                audit=result.audit(),
            )

        return result

    def _record_chunk_audit(
        self, vendor_id: Optional[str], result: ChunkResult
    ) -> None:
        """Accumulate cascade audits per meeting; fetch_meetings() stamps them
        onto outgoing meeting dicts so they reach queue.processing_metadata."""
        if vendor_id:
            self._chunk_audits.setdefault(str(vendor_id), []).append(result.audit())

    async def _parse_pdf_bytes(
        self,
        pdf_bytes: bytes,
        vendor_id: Optional[str] = None,
        force_method: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Legacy shim: maps force_method to a ladder, returns bare items."""
        result = await self._chunk_pdf_bytes(
            pdf_bytes, vendor_id, ladder_for_force_method(force_method)
        )
        return result.items

    async def _chunk_packet_pdf(
        self,
        pdf_url: str,
        vendor_id: Optional[str] = None,
        ladder: str = "auto",
    ) -> ChunkResult:
        """Download a PDF and run the chunker cascade. Returns full ChunkResult."""
        try:
            response = await self._get(pdf_url)
            pdf_bytes = await response.read()
        except Exception as e:
            logger.debug(
                "pdf download failed",
                vendor=self.vendor,
                slug=self.slug,
                vendor_id=vendor_id,
                error=str(e),
            )
            result = ChunkResult(failure_reason=DOWNLOAD_FAILED, ladder=ladder)
            self._record_chunk_audit(vendor_id, result)
            return result
        return await self._chunk_pdf_bytes(pdf_bytes, vendor_id, ladder)

    async def _parse_packet_pdf(
        self,
        pdf_url: str,
        vendor_id: Optional[str] = None,
        force_method: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Legacy shim: download + cascade, returns items or empty list."""
        result = await self._chunk_packet_pdf(
            pdf_url, vendor_id, ladder_for_force_method(force_method)
        )
        return result.items

    # ------------------------------------------------------------------
    # 2nd-pass: resolve sub-attachments from staff report cover PDFs
    # ------------------------------------------------------------------

    # URL patterns that indicate a real document link (not navigation/chrome)
    _ATTACHMENT_URL_PATTERNS = [
        "s3.amazonaws.com", ".pdf", "/uploads/attachment",
        "/attachments/", "cloudfront.net", "/ViewFile/",
        "/DocumentCenter/View/", "/LinkClick.aspx",
        "/showdocument?",
    ]

    async def _resolve_sub_attachments(
        self,
        items: List[Dict[str, Any]],
        vendor_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Download attachment PDFs and extract embedded document links.

        After URL-based chunking, each item may have a single attachment
        that is a staff report cover sheet (1-2 pages) containing hyperlinks
        to the actual documents (contracts, exhibits, etc. on Legistar S3 or
        similar). This method follows those links.

        The original attachment (staff report) is kept; extracted links are
        appended after it. Items without PDF attachments are returned as-is.
        """
        import fitz

        semaphore = asyncio.Semaphore(5)

        async def _resolve_item(item: Dict[str, Any]) -> Dict[str, Any]:
            pdf_atts = [
                a for a in item.get("attachments", [])
                if a.get("url") and a.get("type") in ("pdf", "unknown")
            ]
            if not pdf_atts:
                return item

            # Only inspect the first (primary) attachment per item
            primary_url = pdf_atts[0]["url"]
            tmp_path = None
            async with semaphore:
                try:
                    response = await self._get(primary_url)
                    pdf_bytes = await response.read()
                    if len(pdf_bytes) < 500:
                        return item

                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp_path = tmp.name
                        tmp.write(pdf_bytes)

                    def _extract_links():
                        doc = fitz.open(tmp_path)
                        links = []
                        seen = set()
                        for page in doc:
                            for link in page.get_links():
                                if link.get("kind") != 2:
                                    continue
                                uri = link.get("uri", "")
                                if not uri:
                                    continue
                                uri = _normalize_link_url(uri)
                                if uri in seen or uri == primary_url:
                                    continue
                                if not any(p in uri.lower() for p in self._ATTACHMENT_URL_PATTERNS):
                                    continue
                                seen.add(uri)
                                bbox = link.get("from", fitz.Rect())
                                name = _get_pdf_link_display_text(page, fitz.Rect(bbox))
                                links.append({
                                    "name": name or "Attachment",
                                    "url": uri,
                                    "type": "pdf" if ".pdf" in uri.lower() else "unknown",
                                })
                        doc.close()
                        return links

                    embedded = await asyncio.to_thread(_extract_links)
                    if embedded:
                        existing_urls = {a.get("url") for a in item.get("attachments", [])}
                        new_atts = [a for a in embedded if a.get("url") not in existing_urls]
                        if new_atts:
                            item["attachments"] = item["attachments"] + new_atts
                        logger.info(
                            "resolved sub-attachments from staff report",
                            vendor=self.vendor,
                            slug=self.slug,
                            vendor_id=vendor_id,
                            item=item.get("agenda_number") or item.get("vendor_item_id"),
                            sub_attachment_count=len(new_atts),
                        )

                except Exception as e:
                    logger.debug(
                        "sub-attachment resolution failed",
                        vendor=self.vendor,
                        slug=self.slug,
                        item=item.get("agenda_number") or item.get("vendor_item_id"),
                        error=str(e),
                    )
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
            return item

        return list(await asyncio.gather(*[_resolve_item(i) for i in items]))

    # SharePoint sharing URL patterns: /:b:/ (binary), /:w:/ (word), /:x:/ (excel), /:p:/ (ppt)
    _SHAREPOINT_SHARING_RE = re.compile(
        r'https?://[^/]+\.sharepoint\.com/:[bwxp]:/[gsr]/'
    )

    async def _resolve_sharepoint_urls(
        self,
        items: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Resolve SharePoint sharing URLs to direct download URLs.

        SharePoint sharing links (/:b:/g/...) serve HTML viewer pages, not PDFs.
        This fetches each link with a session, extracts the .downloadUrl from the
        embedded JSON, and replaces the attachment URL with the direct download URL.
        Items without SharePoint URLs are returned unchanged.
        """
        # Collect all unique SharePoint URLs across all items
        sp_urls = set()
        for item in items:
            for att in item.get("attachments", []):
                url = att.get("url", "")
                if self._SHAREPOINT_SHARING_RE.match(url):
                    sp_urls.add(url)

        if not sp_urls:
            return items

        # Resolve all unique SharePoint URLs concurrently
        resolved: Dict[str, Optional[str]] = {}
        semaphore = asyncio.Semaphore(3)

        # SharePoint's anonymous sharing flow requires proper cookie handling
        # through a redirect chain. aiohttp doesn't handle this correctly,
        # so we use requests.Session which natively follows the auth flow.
        import requests as sync_requests

        def _resolve_one_sync(sp_url: str) -> Optional[str]:
            try:
                session = sync_requests.Session()
                session.headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
                resp = session.get(sp_url, timeout=15)
                if resp.status_code != 200:
                    return None
                # PDF sharing pages embed .downloadUrl in ListData JSON.
                # Word/Excel pages use a different structure but still have
                # download.aspx URLs with UniqueId + tempauth tokens.
                m = re.search(r'"\.downloadUrl"\s*:\s*"([^"]+)"', resp.text)
                if not m:
                    m = re.search(
                        r'"(https?://[^"]+/_layouts/15/download\.aspx\?UniqueId=[^"]+)"',
                        resp.text,
                    )
                if m:
                    return m.group(1).replace("\\u002f", "/").replace("\\u0026", "&")
                return None
            except Exception:
                return None

        async def _resolve_one(sp_url: str):
            async with semaphore:
                dl_url = await asyncio.to_thread(_resolve_one_sync, sp_url)
                resolved[sp_url] = dl_url
                if dl_url:
                    logger.info(
                        "resolved sharepoint url",
                        vendor=self.vendor,
                        slug=self.slug,
                        original=sp_url[:80],
                    )

        await asyncio.gather(*[_resolve_one(url) for url in sp_urls])

        # Replace SharePoint URLs in attachments with resolved direct URLs
        for item in items:
            for att in item.get("attachments", []):
                url = att.get("url", "")
                if url in resolved and resolved[url]:
                    att["url"] = resolved[url]

        resolved_count = sum(1 for v in resolved.values() if v)
        if resolved_count:
            logger.info(
                "sharepoint urls resolved",
                vendor=self.vendor,
                slug=self.slug,
                resolved=resolved_count,
                total=len(sp_urls),
            )

        return items

    async def fetch_meetings(self, days_back: int = 14, days_forward: int = 14) -> FetchResult:
        """Fetch meetings, validate, return FetchResult.

        Returns FetchResult with success=True for valid results (even if empty).
        Returns FetchResult with success=False on adapter failure.
        Callers can distinguish "no meetings" from "adapter broken".
        """
        try:
            self._chunk_audits = {}
            meetings = await self._fetch_meetings_impl(days_back, days_forward)
            valid = [m for m in meetings if self._validate_meeting(m)]
            if len(valid) < len(meetings):
                logger.warning("filtered invalid meetings", vendor=self.vendor, slug=self.slug, total=len(meetings), valid=len(valid))

            # Attach chunker cascade audits to their meetings (by vendor_id)
            for m in valid:
                runs = self._chunk_audits.get(str(m.get("vendor_id")))
                if runs:
                    m["chunk_audit"] = summarize_runs(runs)

            # Resolve SharePoint sharing URLs to direct download URLs
            # across all items in all meetings before returning.
            all_items = [item for m in valid for item in m.get("items", [])]
            if any(self._SHAREPOINT_SHARING_RE.match(att.get("url", ""))
                   for item in all_items for att in item.get("attachments", [])):
                await self._resolve_sharepoint_urls(all_items)

            return FetchResult(meetings=valid, success=True)
        except NotImplementedError:
            raise
        except Exception as e:
            logger.error("fetch_meetings failed", vendor=self.vendor, slug=self.slug, error=str(e), error_type=type(e).__name__)
            return FetchResult(meetings=[], success=False, error=str(e), error_type=type(e).__name__)

    def _date_range(self, days_back: int, days_forward: int) -> Tuple[datetime, datetime]:
        """Compute inclusive date range for meeting filtering.

        Returns (start, end) as midnight datetimes so boundary-day meetings
        (stored as midnight) are never excluded by time-of-day comparison.
        """
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return today - timedelta(days=days_back), today + timedelta(days=days_forward)

    async def _fetch_meetings_impl(self, days_back: int, days_forward: int) -> List[Dict[str, Any]]:
        """Subclass must implement. Return raw meeting dicts."""
        raise NotImplementedError(f"{self.__class__.__name__} must implement _fetch_meetings_impl()")
