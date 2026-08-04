"""
Async Analyzer - LLM analysis orchestration with concurrent processing

Async version of pipeline/analyzer.py with:
- Async PDF downloads (aiohttp)
- Concurrent PDF extraction (asyncio.to_thread for PyMuPDF)
- Concurrent batch processing

Coordinates:
- PDF extraction (parsing/)
- LLM summarization (analysis/llm/)
- Participation parsing (parsing/)
- Topic extraction (analysis/topics/)

Rate limiting is handled by the summarizer via Gemini's retry instructions.
"""

import asyncio
import html as html_module
import os
import re
import tempfile
import time
from typing import AsyncIterator, List, Dict, Any, Optional, Tuple, cast
from urllib.parse import urljoin

import aiohttp

from corpus.store import get_corpus, sha256_hex
from exceptions import ExtractionError, LLMError
from parsing.pdf import PdfExtractor, extract_document_file
from parsing.subprocess_guard import GuardCrashed, GuardTaskError, GuardTimeout, run_guarded
from parsing.participation import parse_participation_info
from analysis.llm.summarizer import GeminiSummarizer
from analysis.llm.input_budget import (
    limit_item_title,
    limit_shared_context,
    prepare_item_text,
)
from pipeline.protocols import MetricsCollector, NullMetrics
from vendors.rate_limiter_async import get_rate_limiter, vendor_for_url

from config import config, get_logger

logger = get_logger(__name__).bind(component="pipeline")

# Patterns for extracting PDF links from HTML attachment pages.
# Ordered by specificity: direct .pdf links first, then vendor-specific patterns.
_PDF_HREF_RE = re.compile(
    r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']',
    re.IGNORECASE,
)
_VENDOR_DOC_HREF_RE = re.compile(
    r'href=["\']([^"\']*(?:'
    r'/ViewFile/|/DocumentCenter/View/|/LinkClick\.aspx'
    r'|/MetaViewer\.php|cloudfront\.net|s3\.amazonaws\.com'
    r')[^"\']*)["\']',
    re.IGNORECASE,
)


def _extract_best_pdf_link(html_bytes: bytes, base_url: str) -> Optional[str]:
    """Parse an HTML attachment page for the best PDF download link.

    Handles the generic case where a URL serves an HTML detail page
    instead of the actual PDF. Looks for direct .pdf hrefs first,
    then vendor-specific document viewer patterns.

    Returns absolute URL or None.
    """
    try:
        text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None

    # Direct .pdf links -- strongest signal
    pdf_matches = _PDF_HREF_RE.findall(text)
    if pdf_matches:
        return urljoin(base_url, html_module.unescape(pdf_matches[0]))

    # Vendor document viewer URLs (ViewFile, DocumentCenter, MetaViewer, etc.)
    vendor_matches = _VENDOR_DOC_HREF_RE.findall(text)
    if vendor_matches:
        return urljoin(base_url, html_module.unescape(vendor_matches[0]))

    return None


def _extract_pdf_in_subprocess(pdf_path, ocr_threshold, ocr_dpi,
                               detect_legislative_formatting, max_ocr_workers):
    """Run PDF extraction in an isolated, resource-capped subprocess.

    Thin translation over parsing.subprocess_guard.run_guarded -- the shared
    containment used by every heavy PDF path (this one and the sync chunker).
    The guard owns the forkserver, RLIMIT_AS, oom_score_adj, kill-on-timeout,
    and queue-drain-before-join mechanics; this wrapper owns only the
    extraction-flavored budget and error surface.

    1.5GB budget rationale (3.8GB RAM + 6GB swap box):
    - Parent no longer holds PDF bytes during extraction (tempfile handoff)
    - Up to 6 concurrent children (pdf_semaphore=6)
    - 6 * 1.5GB = 9GB child ceiling
    - Parent (~200-300MB) + postgres (~700MB) + system (~200MB) = ~1.2GB
    - Total: ~10.2GB vs ~9.7GB available -- safe because not all 6 hit ceiling
    - Normal PDFs use 200-350MB; only monster 1000+ page OCR jobs hit the cap

    The child imports parsing.pdf (the guard target's module), not this
    module -- spawns no longer pay for the analyzer's HTTP/LLM import stack.
    """
    try:
        return run_guarded(
            extract_document_file,
            (pdf_path, ocr_threshold, ocr_dpi, detect_legislative_formatting, max_ocr_workers),
            timeout=600,
            rlimit_bytes=int(1.5 * 1024 * 1024 * 1024),
        )
    except GuardTimeout:
        raise ExtractionError("PDF extraction subprocess timed out after 600s")
    except GuardCrashed as e:
        raise ExtractionError(
            f"PDF extraction subprocess crashed (exit code {e.exitcode}, likely segfault on malformed PDF)"
        )
    except GuardTaskError as e:
        raise ExtractionError(f"PDF extraction failed: {e} ({e.error_type})")


class AnalysisError(Exception):
    """Base exception for analysis errors"""
    pass


class AsyncAnalyzer:
    """
    Async LLM analysis orchestrator.

    Key Features:
    - Async PDF downloads (aiohttp, concurrent)
    - CPU-bound extraction in thread pool (non-blocking)
    - Concurrent batch processing

    Rate limiting is handled reactively by the summarizer via Gemini's retry instructions.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        metrics: Optional[MetricsCollector] = None,
        *,
        enable_llm: bool = True,
    ):
        """Initialize the async analyzer

        Args:
            api_key: Gemini API key (or uses environment variables)
            metrics: Metrics collector for LLM call tracking (uses NullMetrics if not provided)
            enable_llm: Construct the Gemini client. Extraction-only jobs set
                this false so they do not require an API key.
        """
        self.metrics = cast(MetricsCollector, metrics or NullMetrics())
        self.pdf_extractor = PdfExtractor(ocr_dpi=150)  # 150 DPI sufficient for meeting agendas
        self.summarizer = (
            GeminiSummarizer(api_key=api_key, metrics=self.metrics)
            if enable_llm
            else None
        )
        self.http_session: Optional[aiohttp.ClientSession] = None
        self._request_count = 0
        self._recycle_after = 100  # Recycle session after N requests to prevent memory accumulation
        self._in_flight = 0  # Track concurrent downloads to prevent recycling mid-download
        self._recycle_lock = asyncio.Lock()  # Serialize recycle checks

        logger.info(
            "async analyzer initialized",
            pdf_extractor="pymupdf",
            summarizer="gemini" if enable_llm else "disabled"
        )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session (lazy initialization)"""
        if self.http_session is None or self.http_session.closed:
            timeout = aiohttp.ClientTimeout(total=300, connect=30)  # 5min total, 30s connect
            self.http_session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/pdf,application/octet-stream,*/*"
                }
            )
        return self.http_session

    async def close(self):
        """Close HTTP session (cleanup)"""
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.debug("http session closed")

    async def _drain_and_close_session(self, session: aiohttp.ClientSession) -> None:
        """Close a rotated session after a grace period for in-flight requests.

        When rotating sessions under load, we drop our reference but callers that
        already grabbed a pointer to the old session keep using it. A grace period
        lets their requests complete naturally before we force-close the connector.
        """
        try:
            await asyncio.sleep(60)
            if not session.closed:
                await session.close()
                logger.debug("rotated http session closed after grace period")
        except (aiohttp.ClientError, asyncio.CancelledError, OSError) as e:
            logger.warning("failed to close rotated session", error=str(e))

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensures cleanup even on exception"""
        await self.close()
        return False  # Don't suppress exceptions

    async def download_pdf_async(self, url: str, _depth: int = 0) -> bytes:
        """
        Download PDF asynchronously (non-blocking).

        If the URL serves HTML instead of a PDF, parses the page for PDF links
        and follows through to the actual document (generic 2nd-pass resolution).
        This handles intermediate "attachment detail" pages across vendors.

        Args:
            url: PDF or attachment-page URL
            _depth: recursion guard (internal)

        Returns:
            PDF bytes

        Raises:
            ExtractionError: If download fails or no PDF can be resolved
        """
        # Acquire session with rotation check (serialized, but quick).
        # Rotation drops the old session reference and creates a new one -- in-flight
        # requests on the old session keep working because they already hold a local
        # reference, and the old session is scheduled for close after a grace period.
        # This avoids the previous deadlock where _in_flight never hit 0 under load.
        async with self._recycle_lock:
            self._request_count += 1
            if self._request_count >= self._recycle_after:
                old_session = self.http_session
                self.http_session = None
                self._request_count = 0
                logger.info("http session rotating", after_requests=self._recycle_after)
                if old_session and not old_session.closed:
                    asyncio.create_task(self._drain_and_close_session(old_session))
            session = await self._get_session()
            self._in_flight += 1

        # Per-vendor politeness gate: shared with the sync side so processing
        # downloads pace through the same multi-slot rate limiter rather than
        # bypassing it via the analyzer's separate aiohttp session.
        vendor = vendor_for_url(url)
        await get_rate_limiter().wait_if_needed(vendor)

        # Actual download happens outside lock (concurrent)
        try:
            async with session.get(url, ssl=False) as resp:  # Disable SSL for Granicus S3
                if resp.status != 200:
                    raise ExtractionError(f"HTTP {resp.status} downloading PDF from {url}")

                content_type = resp.headers.get("Content-Type", "")
                raw_bytes = await resp.read()

                # Happy path: response is a PDF
                if raw_bytes[:5] == b"%PDF-" or "application/pdf" in content_type:
                    logger.debug("pdf downloaded", url=url, size_mb=round(len(raw_bytes) / 1024 / 1024, 2))
                    return raw_bytes

                # HTML response -- intermediate attachment page. Parse for PDF links.
                if _depth >= 1:
                    # Already followed one redirect; don't chase further.
                    logger.debug("html attachment page returned non-pdf after resolve, giving up", url=url[:120])
                    raise ExtractionError(f"Resolved URL still not a PDF: {url[:120]}")

                if "text/html" in content_type or raw_bytes[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
                    pdf_url = _extract_best_pdf_link(raw_bytes, url)
                    if pdf_url:
                        logger.info(
                            "html attachment page resolved to pdf",
                            original_url=url[:120],
                            resolved_url=pdf_url[:120],
                        )
                        return await self.download_pdf_async(pdf_url, _depth=_depth + 1)

                    # OnBase dual-endpoint: different Hyland deployments serve
                    # the PDF via different paths. Whittier CA / Santa Barbara
                    # respond to /Documents/ViewDocument/ and 404 on
                    # /DownloadFileBytes/; Concord CA / Tampa respond to
                    # /DownloadFileBytes/ and 404 on /ViewDocument/. The
                    # adapter picks one; if it's the wrong one for a given
                    # deployment, swap and retry once.
                    onbase_alt = None
                    if "/Documents/ViewDocument/" in url:
                        onbase_alt = url.replace("/Documents/ViewDocument/", "/Documents/DownloadFileBytes/")
                    elif "/Documents/DownloadFileBytes/" in url:
                        onbase_alt = url.replace("/Documents/DownloadFileBytes/", "/Documents/ViewDocument/")
                    if onbase_alt and onbase_alt != url:
                        logger.info(
                            "onbase endpoint returned html, retrying alternate",
                            original_url=url[:120],
                            alt_url=onbase_alt[:120],
                        )
                        return await self.download_pdf_async(onbase_alt, _depth=_depth + 1)

                    logger.debug("html attachment page had no pdf links", url=url[:120])
                    raise ExtractionError(f"Attachment page contained no PDF links: {url[:120]}")

                # Unknown content type -- try using it as-is (could be octet-stream)
                logger.debug("pdf downloaded", url=url, size_mb=round(len(raw_bytes) / 1024 / 1024, 2))
                return raw_bytes

        except aiohttp.ClientError as e:
            logger.error("pdf download failed", url=url, error=str(e))
            raise ExtractionError(f"Failed to download PDF: {e}") from e
        finally:
            self._in_flight -= 1

    async def extract_pdf_async(self, url: str, banana: Optional[str] = None) -> Dict[str, Any]:
        """
        Extract text from PDF asynchronously.

        Downloads PDF with async HTTP, writes to temp file, runs extraction
        in isolated subprocess. Temp file avoids doubling memory: parent
        releases PDF bytes before the child starts extracting.

        Args:
            url: PDF URL
            banana: Jurisdiction for corpus provenance (document_source.banana);
                    extraction itself doesn't need it

        Returns:
            Dict with keys: success, text, page_count, etc.

        Raises:
            ExtractionError: If extraction fails, times out, or subprocess crashes
        """
        pdf_bytes = await self.download_pdf_async(url)

        # Corpus dedup gate (docs/CORPUS_ARCHITECTURE.md): identity comes from
        # the bytes themselves. If the corpus already holds text for this exact
        # content, serve it and skip extraction entirely -- re-summarization,
        # matter aggregation, and packet-vs-attachment overlaps all stop
        # re-paying extraction. Hash in a thread: big packets would stall the
        # loop for hundreds of ms.
        corpus_store = get_corpus()
        content_sha256 = None
        byte_count = len(pdf_bytes)
        if corpus_store:
            content_sha256 = await asyncio.to_thread(sha256_hex, pdf_bytes)
            cached = await corpus_store.lookup_extraction(content_sha256)
            if cached:
                await corpus_store.record_sighting(content_sha256, url, banana)
                logger.info(
                    "extraction served from corpus",
                    url=url[:100],
                    sha=content_sha256[:16],
                    chars=len(cached.get("text") or ""),
                )
                cached["content_sha256"] = content_sha256
                cached["corpus_persisted"] = True
                return cached

        # Write to temp file and release bytes before subprocess starts.
        # Without this, parent holds bytes for the full extraction duration
        # (asyncio.to_thread keeps a reference to all args).
        fd, pdf_path = tempfile.mkstemp(suffix=".pdf")
        os.write(fd, pdf_bytes)
        os.close(fd)
        del pdf_bytes

        # Run in subprocess via thread (proc.join is blocking)
        # Subprocess isolates against PyMuPDF segfaults on malformed PDFs
        try:
            # Archive the original first, streaming from the temp file so the
            # parent never re-holds the bytes. Before extraction on purpose:
            # a pathological document that times out below still enters the
            # archive, ready for a better extractor later. Failures inside the
            # store are logged and swallowed -- the corpus never fails a job.
            if corpus_store and content_sha256:
                with open(pdf_path, "rb") as original:
                    await corpus_store.archive_original(
                        content_sha256,
                        byte_count=byte_count,
                        file_obj=original,
                        source_url=url,
                        banana=banana,
                    )
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    _extract_pdf_in_subprocess,
                    pdf_path,
                    self.pdf_extractor.ocr_threshold,
                    self.pdf_extractor.ocr_dpi,
                    self.pdf_extractor.detect_legislative_formatting,
                    self.pdf_extractor.max_ocr_workers,
                ),
                timeout=620
            )
        except asyncio.TimeoutError:
            logger.error("PDF extraction timed out", url=url[:100])
            raise ExtractionError(f"PDF extraction timed out: {url[:100]}")
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass

        if not result.get("success"):
            raise ExtractionError(f"PDF extraction failed: {result.get('error', 'Unknown error')}")

        # Write-once: the freshly manufactured ground truth enters the corpus.
        # Every future encounter with these bytes is a lookup, not an extraction.
        corpus_persisted = False
        if corpus_store and content_sha256:
            corpus_persisted = await corpus_store.persist_extraction(content_sha256, result)

        # Operational ingestion callers need to distinguish "text extracted"
        # from "text durably entered the corpus". Normal analysis callers can
        # continue to treat corpus persistence as best-effort.
        result["content_sha256"] = content_sha256
        result["corpus_persisted"] = corpus_persisted

        logger.debug("pdf extracted", url=url, pages=result.get("page_count", 0))
        return result

    async def process_agenda_with_cache_async(self, meeting_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main entry point - process agenda with caching (async version).

        Args:
            meeting_data: Dictionary with packet_url, city_banana, etc.

        Returns:
            Dictionary with summary, processing_time, cached flag, etc.
        """
        packet_url = meeting_data.get("packet_url")
        if not packet_url:
            return {"success": False, "error": "No packet_url provided"}

        city_banana = meeting_data.get("city_banana", "unknown")

        # Process with Gemini
        logger.info("processing meeting", city=city_banana)
        start_time = time.time()

        try:
            # Process the agenda (returns summary, method, participation)
            summary, method, participation = await self.process_agenda_async(
                packet_url, banana=meeting_data.get("city_banana")
            )

            processing_time = time.time() - start_time
            meeting_id = meeting_data.get("meeting_id")

            logger.info("processing success", city=city_banana, duration_seconds=round(processing_time, 1))

            return {
                "success": True,
                "summary": summary,
                "processing_time": processing_time,
                "processing_method": method,
                "participation": participation,
                "cached": False,
                "meeting_id": meeting_id,
            }

        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(
                "processing failed",
                city=city_banana,
                error=str(e),
                error_type=type(e).__name__,
                duration_seconds=round(processing_time, 1)
            )
            return {
                "success": False,
                "error": str(e),
                "processing_time": processing_time,
                "cached": False,
            }

    async def process_agenda_async(self, url: str, banana: Optional[str] = None) -> Tuple[str, str, Optional[Dict[str, Any]]]:
        """
        Process agenda using PyMuPDF + Gemini (async, fail fast approach).

        Args:
            url: PDF URL
            banana: Jurisdiction for corpus provenance

        Returns:
            Tuple of (summary, method_used, participation_info)

        Raises:
            AnalysisError: If processing fails
        """
        summarizer = self.summarizer
        if summarizer is None:
            raise AnalysisError("LLM processing is disabled for this analyzer")

        try:
            # Extract PDF text (async download + thread pool extraction)
            result = await self.extract_pdf_async(url, banana=banana)

            if result.get("success") and result.get("text"):
                extracted_text = result["text"]

                # Parse participation info BEFORE AI summarization
                participation = parse_participation_info(extracted_text)
                if participation:
                    logger.debug("extracted participation info", fields=list(participation.model_dump(exclude_none=True).keys()))

                # Summarize meeting (Gemini SDK is sync, run in thread pool)
                # Rate limiting handled reactively by summarizer via Gemini's retry instructions
                # 5 min timeout - summarizer has internal 3 min retry budget
                try:
                    summary = await asyncio.wait_for(
                        asyncio.to_thread(
                            summarizer.summarize_meeting,
                            extracted_text
                        ),
                        timeout=300
                    )
                except asyncio.TimeoutError:
                    logger.error("LLM summarization timed out after 5 minutes", url=url[:100])
                    raise LLMError("LLM summarization timed out after 5 minutes", model="gemini", prompt_type="meeting")

                logger.info("agenda processing success", url=url)

                participation_dict = participation.model_dump() if participation else None
                return summary, "pymupdf_gemini", participation_dict
            else:
                logger.warning("no text extracted or poor quality", url=url)

        except (ExtractionError, LLMError, OSError, IOError) as e:
            logger.warning("processing failed", url=url, error=str(e), error_type=type(e).__name__)

        # Fail fast
        logger.error("analysis rejected", url=url)
        raise AnalysisError(
            "Document analysis failed. "
            "This PDF may be scanned or have complex formatting."
        )

    async def process_batch_items_async(
        self,
        item_requests: List[Dict[str, Any]],
        shared_context: Optional[str] = None,
        meeting_id: Optional[str] = None
    ) -> AsyncIterator[List[Dict[str, Any]]]:
        """
        Process multiple agenda items concurrently, yielding each result as it
        completes (streaming).

        Items run through an LLM_CONCURRENCY-bounded semaphore; each finished
        item is yielded immediately rather than waiting for the slowest sibling.
        Callers see steady DB writes instead of a final bulk dump, and a slow
        Gemini call only delays its own item -- siblings keep flowing.

        Args:
            item_requests: List of dicts with structure:
                [{
                    'item_id': str,
                    'title': str,
                    'text': str,
                    'sequence': int,
                    'page_count': int or None
                }, ...]
            shared_context: Optional meeting-level shared document context
            meeting_id: Optional meeting ID (for cache naming)

        Yields:
            Single-item chunks [{...}] as each item finishes, in completion
            order (NOT input order). Each item dict has:
                'item_id': str
                'success': bool
                'summary': str (if success)
                'topics': List[str] (if success)
                'error': str (if not success)
        """
        if not item_requests:
            return

        summarizer = self.summarizer
        if summarizer is None:
            raise AnalysisError("LLM processing is disabled for this analyzer")

        shared_context = limit_shared_context(shared_context)

        logger.info(
            "processing batch items async",
            count=len(item_requests),
            concurrent=True,
        )

        async def process_item(item: Dict[str, Any]) -> Dict[str, Any]:
            """Process single item with timeout."""
            try:
                text = item.get("text", "")
                title = limit_item_title(item.get("title", ""))
                page_count = item.get("page_count")

                # Mirror the batch lane's shared-context inlining
                # (_submit_one_chunk); without it, items whose only documents
                # are shared reach the model as "[Item: title]" with no
                # document text.
                text = prepare_item_text(
                    title,
                    text,
                    shared_context,
                    inline_shared=True,
                )

                # Summarize item (Gemini SDK is sync, run in thread pool).
                # SDK now has its own 300s http timeout matching this wait_for,
                # so a real stall cancels the socket and frees the thread
                # instead of leaking it past the async-layer cancellation.
                summary, topics = await asyncio.wait_for(
                    asyncio.to_thread(
                        summarizer.summarize_item,
                        title,
                        text,
                        page_count,
                    ),
                    timeout=300,
                )

                return {
                    "item_id": item["item_id"],
                    "success": True,
                    "summary": summary,
                    "topics": topics,
                }

            except asyncio.TimeoutError:
                logger.error(
                    "item summarization timed out after 5 minutes",
                    item_id=item.get("item_id"),
                    title=item.get("title", "")[:50],
                )
                return {
                    "item_id": item["item_id"],
                    "success": False,
                    "error": "LLM summarization timed out after 5 minutes",
                }

            except Exception as e:
                logger.error(
                    "item processing failed",
                    item_id=item.get("item_id"),
                    error=str(e),
                    error_type=type(e).__name__,
                )
                return {
                    "item_id": item["item_id"],
                    "success": False,
                    "error": str(e),
                }

        concurrency = config.LLM_CONCURRENCY
        semaphore = asyncio.Semaphore(concurrency)

        async def process_with_limit(item: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await process_item(item)

        # Fan out all tasks up-front; semaphore caps in-flight at LLM_CONCURRENCY.
        # asyncio.as_completed yields each task's result the moment it lands,
        # giving the caller a steady stream of completions to write to the DB.
        tasks = [asyncio.create_task(process_with_limit(item)) for item in item_requests]
        success_count = 0
        try:
            for finished in asyncio.as_completed(tasks):
                try:
                    result = await finished
                except Exception as e:
                    # process_item already swallows expected exceptions; this
                    # only catches truly unexpected ones (e.g., CancelledError
                    # bubbling up). Yield a synthetic failure rather than
                    # losing the item silently.
                    logger.error("unexpected task exception", error=str(e), error_type=type(e).__name__)
                    yield [{"item_id": "unknown", "success": False, "error": str(e)}]
                    continue
                if result["success"]:
                    success_count += 1
                yield [result]
        finally:
            # If the consumer breaks out (e.g., shutdown), make sure no tasks
            # keep running in the background holding semaphore slots or LLM
            # connections.
            for t in tasks:
                if not t.done():
                    t.cancel()

        logger.info(
            "batch processing complete",
            success=success_count,
            total=len(item_requests),
            concurrency=concurrency,
        )
