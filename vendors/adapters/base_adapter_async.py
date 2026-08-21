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
from typing import Optional, List, Dict, Any, Tuple, cast
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import aiohttp
from pydantic import ValidationError

from config import config, get_logger
from corpus.store import get_corpus
from pipeline.document_acquisition import DocumentResponse, DocumentSourceAcquirer
from pipeline.document_artifacts import DocumentFormat
from pipeline.ground_truth import archive_bytes, produce_ground_truth
from pipeline.protocols import MetricsCollector, NullMetrics
from vendors.adapters.parsers.agenda_chunker import _normalize_link_url
from vendors.adapters.parsers.morphology import is_bare_document
from vendors.adapters.parsers.router import (
    ChunkResult,
    DEFERRED,
    DOWNLOAD_FAILED,
    ladder_for_force_method,
    summarize_runs,
)
from vendors.rate_limiter_async import get_rate_limiter
from vendors.schemas import validate_meeting_output
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

    # Adapters opt in only after their implementation has a metadata-only
    # branch. This keeps the minutes sweep from accidentally running detail,
    # attachment, or corpus-enrichment work on an unsupported vendor.
    MINUTES_DISCOVERY_SUPPORTED = False

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
        # Adapters are constructed from (vendor, slug) and legitimately don't
        # know their jurisdiction; the fetcher stamps this after construction
        # so corpus provenance (document_source.banana) knows which government
        # produced the bytes. None is fine -- provenance degrades, tee still works.
        self.banana: Optional[str] = None
        self.metrics = cast(MetricsCollector, metrics or NullMetrics())
        self._document_acquirer = DocumentSourceAcquirer(
            self._load_document_response,
            fetch_errors=(VendorHTTPError,),
            corpus_getter=lambda: get_corpus(),
            metrics=self.metrics,
            metric_component="sync",
        )
        # chunker cascade audits collected during a fetch, keyed by vendor_id;
        # fetch_meetings() stamps them onto the outgoing meeting dicts
        self._chunk_audits: Dict[str, List[Dict[str, Any]]] = {}
        # html parse audits (which dialect pattern matched), same lifecycle
        self._html_audits: Dict[str, Dict[str, Any]] = {}
        self._minutes_discovery_only = False

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

        # Discovery matrices already get retried at the city-sync boundary.
        # Let those callers opt out of multiplying one dead host into three
        # attempts for every candidate path while keeping the normal request
        # policy as the default everywhere else.
        max_attempts = kwargs.pop("_max_attempts", self._MAX_REQUEST_ATTEMPTS)
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("_max_attempts must be a positive integer")

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

        for attempt in range(max_attempts):
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
                    if 500 <= response.status < 600 and attempt < max_attempts - 1:
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
                if attempt < max_attempts - 1:
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
                if attempt < max_attempts - 1:
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
            f"Request failed after {max_attempts} attempts: {last_error}",
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

    async def _load_document_response(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> DocumentResponse:
        """Load document bytes through the adapter's auth/rate/retry policy."""
        conditional_headers = {
            key: value
            for key, value in (
                ("If-None-Match", etag),
                ("If-Modified-Since", last_modified),
            )
            if value
        }
        try:
            response = (
                await self._get(url, headers=conditional_headers)
                if conditional_headers
                else await self._get(url)
            )
        except VendorHTTPError as exc:
            if exc.status_code != 412 or not conditional_headers:
                raise
            logger.info(
                "vendor document validator rejected, retrying unconditionally",
                vendor=self.vendor,
                slug=self.slug,
                url=url[:120],
            )
            response = await self._get(url)

        response_url = str(getattr(response, "url", url))
        response_etag = response.headers.get("ETag")
        response_last_modified = response.headers.get("Last-Modified")
        if response.status == 304 and conditional_headers:
            response.release()
            return DocumentResponse(
                data=None,
                content_type=response.headers.get("Content-Type", ""),
                response_url=response_url,
                etag=response_etag or etag,
                last_modified=response_last_modified or last_modified,
            )

        return DocumentResponse(
            data=await response.read(),
            content_type=response.headers.get("Content-Type", ""),
            response_url=response_url,
            etag=response_etag,
            last_modified=response_last_modified,
        )

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
        """Validate the complete typed adapter contract.

        Kept as a boolean compatibility shim for tests and custom adapter code.
        ``fetch_meetings`` additionally uses the model output so safe
        normalizations cross the canonical boundary.
        """
        try:
            validate_meeting_output(meeting)
        except (ValidationError, TypeError) as e:
            title = (
                meeting.get("title", "unknown")
                if isinstance(meeting, dict)
                else "unknown"
            )
            logger.warning(
                "meeting failed schema validation",
                vendor=self.vendor,
                slug=self.slug,
                title=str(title)[:50],
                error=str(e),
            )
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
                # A bare document keeps its whole listing: the titles are the
                # only record of what the body met about, and body_text there
                # is an accident of which item happened to absorb the roll-call
                # block. Filtering on it deleted the trailing item of every
                # bare agenda (nothing follows it to slice a body from) and
                # sometimes all but one. Nothing summarizes these -- the
                # processor's bare-agenda gate stops before the LLM. Every
                # other shape keeps the body_text filter, so richer agendas
                # still fall through to packet chunking and manufacture.
                if is_bare_document(result.profile):
                    text_fallback = items
                else:
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
        source_url: Optional[str] = None,
        archived_content_sha256: Optional[str] = None,
    ) -> ChunkResult:
        """Run the chunker cascade on raw PDF bytes. Returns full ChunkResult.

        Routing policy lives in router.LADDERS; every rung attempt and any
        terminal failure reason ends up in the result's audit trail.
        """
        if self._minutes_discovery_only:
            # Defense in depth for sweep dry-runs: even if an adapter's
            # metadata-only branch regresses, discovery can never tee bytes to
            # the corpus or invoke a parser.
            return ChunkResult(failure_reason=DEFERRED, ladder=ladder)

        # Shape deferral: with SYNC_CHUNKING off, sync does stage-1 only --
        # archive the bytes to the corpus and store the meeting's URLs; the
        # processor manufactures items at claim time from the archived bytes
        # (same producer, later call). Adapters' probe logic reads this as
        # "no items" and falls through its URL ladder, archiving each
        # candidate on the way -- which is exactly stage 1's job.
        if not config.SYNC_CHUNKING:
            if archived_content_sha256 is None:
                await archive_bytes(pdf_bytes, source_url, self.banana)
            result = ChunkResult(failure_reason=DEFERRED, ladder=ladder)
            self._record_chunk_audit(vendor_id, result)
            return result

        # The single producer (pipeline/ground_truth.py): archive tee +
        # guarded chunk + provably-complete text persist, shared with the
        # processor's shape-manufacturing step.
        producer = produce_ground_truth(
            pdf_bytes,
            vendor=self.vendor,
            slug=self.slug,
            ladder=ladder,
            source_url=source_url,
            banana=self.banana,
            archived_content_sha256=archived_content_sha256,
        )
        # Transfer the sole local byte reference into the producer coroutine so
        # this adapter frame does not retain it while the guarded child runs.
        pdf_bytes = b""
        result = await producer

        self._record_chunk_audit(vendor_id, result)

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
            self._chunk_audits.setdefault(str(vendor_id), []).append({
                **result.audit(),
                "observed_at": datetime.now().isoformat(),
            })

    def _record_html_audit(
        self,
        vendor_id: Optional[str],
        pattern: Optional[str],
        items: List[Dict[str, Any]],
    ) -> None:
        """Record which HTML dialect pattern parsed this meeting's items.
        Stamped onto the meeting dict by fetch_meetings(), persisted in
        queue.processing_metadata — dialect drift becomes queryable."""
        if vendor_id:
            self._html_audits[str(vendor_id)] = {
                "audit_version": "ha1",
                "observed_at": datetime.now().isoformat(),
                "pattern": pattern,
                "item_count": len(items),
                "attachment_count": sum(
                    len(it.get("attachments") or []) for it in items
                ),
            }

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
        if self._minutes_discovery_only:
            return ChunkResult(failure_reason=DEFERRED, ladder=ladder)

        try:
            artifact = await self._document_acquirer.acquire(
                pdf_url,
                banana=self.banana,
            )
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
        if artifact.document_format is not DocumentFormat.PDF:
            logger.debug(
                "packet source was not a pdf",
                vendor=self.vendor,
                slug=self.slug,
                vendor_id=vendor_id,
                document_format=artifact.document_format.value,
            )
            result = ChunkResult(failure_reason=DOWNLOAD_FAILED, ladder=ladder)
            self._record_chunk_audit(vendor_id, result)
            return result

        chunk = self._chunk_pdf_bytes(
            artifact.data,
            vendor_id,
            ladder,
            source_url=pdf_url,
            archived_content_sha256=(
                artifact.content_sha256 if artifact.corpus_persisted else None
            ),
        )
        del artifact
        return await chunk

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
                    artifact = await self._document_acquirer.acquire(
                        primary_url,
                        banana=self.banana,
                    )
                    if artifact.document_format is not DocumentFormat.PDF:
                        return item
                    pdf_bytes = artifact.data
                    if len(pdf_bytes) < 500:
                        return item

                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        tmp_path = tmp.name
                        tmp.write(pdf_bytes)
                    # Link extraction reopens the tempfile; release the parent
                    # artifact and bytes before entering the worker thread.
                    del artifact
                    pdf_bytes = b""

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

    async def fetch_meetings(self, days_back: int = 14, days_forward: int = 28) -> FetchResult:
        """Fetch meetings, validate, return FetchResult.

        Returns FetchResult with success=True for valid results (even if empty).
        Returns FetchResult with success=False on adapter failure.
        Callers can distinguish "no meetings" from "adapter broken".
        """
        try:
            self._chunk_audits = {}
            self._html_audits = {}
            meetings = await self._fetch_meetings_impl(days_back, days_forward)
            valid: List[Dict[str, Any]] = []
            for index, meeting in enumerate(meetings):
                try:
                    validated = validate_meeting_output(meeting)
                except (ValidationError, TypeError) as e:
                    logger.warning(
                        "meeting failed schema validation",
                        vendor=self.vendor,
                        slug=self.slug,
                        meeting_index=index,
                        title=(
                            str(meeting.get("title", "unknown"))[:50]
                            if isinstance(meeting, dict)
                            else "unknown"
                        ),
                        error=str(e),
                    )
                    continue
                # Preserve adapter-specific extras while applying the schema's
                # compatibility normalizations. Omit None-valued optional fields
                # to retain the prior sparse-dict contract.
                valid.append(validated.model_dump(exclude_none=True))
            if len(valid) < len(meetings):
                logger.warning("filtered invalid meetings", vendor=self.vendor, slug=self.slug, total=len(meetings), valid=len(valid))

            # A genuinely empty vendor result is a successful no-op. Raw work
            # that was fetched but entirely rejected at the schema boundary is
            # different: reporting success would mark the city synced while
            # silently discarding every candidate.
            if meetings and not valid:
                error = (
                    f"All {len(meetings)} fetched meeting candidate(s) failed "
                    "schema validation"
                )
                logger.error(
                    "all fetched meetings failed schema validation",
                    vendor=self.vendor,
                    slug=self.slug,
                    total=len(meetings),
                )
                return FetchResult(
                    meetings=[],
                    success=False,
                    error=error,
                    error_type="SchemaValidationError",
                )

            # Attach extraction audits to their meetings (by vendor_id)
            for m in valid:
                vid = str(m.get("vendor_id"))
                runs = self._chunk_audits.get(vid)
                if runs:
                    m["chunk_audit"] = summarize_runs(runs)
                html_audit = self._html_audits.get(vid)
                if html_audit and "html_audit" not in m:
                    m["html_audit"] = html_audit

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

    async def fetch_minutes(self, days_back: int = 60, days_forward: int = 0) -> FetchResult:
        """Discover minutes URLs without agenda/item/document enrichment.

        Subclasses must explicitly opt in and honor
        ``self._minutes_discovery_only`` before this method will call them.
        Unsupported adapters safely report no discoveries.
        """
        if not self.MINUTES_DISCOVERY_SUPPORTED:
            logger.info(
                "minutes discovery unsupported",
                vendor=self.vendor,
                slug=self.slug,
            )
            return FetchResult(meetings=[], success=True)

        self._minutes_discovery_only = True
        try:
            result = await self.fetch_meetings(days_back, days_forward)
        finally:
            self._minutes_discovery_only = False

        if result.success:
            result.meetings = [m for m in result.meetings if m.get("minutes_url")]
        return result

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
