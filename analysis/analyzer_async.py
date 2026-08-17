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
import os
import tempfile
import time
from typing import AsyncIterator, List, Dict, Any, Optional, Tuple, cast

import aiohttp

from corpus.store import get_corpus
from exceptions import DocumentDownloadError, ExtractionError, LLMError
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
from pipeline.document_acquisition import DocumentResponse, DocumentSourceAcquirer
from pipeline.document_artifacts import (
    DocumentArtifact,
    DocumentFormat,
    extract_document_links,
    sanitize_html_text,
    verify_tls_for_url,
)
from pipeline.utils import attachment_identity
from vendors.rate_limiter_async import get_rate_limiter, vendor_for_url

from config import config, get_logger

logger = get_logger(__name__).bind(component="pipeline")


def _extract_best_pdf_link(html_bytes: bytes, base_url: str) -> Optional[str]:
    """Compatibility wrapper for the former PDF-only HTML resolver."""
    links = extract_document_links(html_bytes, base_url)
    return links[0] if links else None


def _extract_pdf_in_subprocess(document_path, ocr_threshold, ocr_dpi,
                               detect_legislative_formatting, max_ocr_workers):
    """Run document extraction in an isolated, resource-capped subprocess.

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
    args = (
        document_path,
        ocr_threshold,
        ocr_dpi,
        detect_legislative_formatting,
        max_ocr_workers,
    )
    try:
        return run_guarded(
            extract_document_file,
            args,
            timeout=600,
            rlimit_bytes=int(1.5 * 1024 * 1024 * 1024),
        )
    except GuardTimeout:
        raise ExtractionError("Document extraction subprocess timed out after 600s")
    except GuardCrashed as e:
        # Drawing inspection is the highest-risk native MuPDF operation in
        # this path. Some otherwise readable, graphics-heavy PDFs crash there
        # while plain text extraction succeeds. Retry once in a fresh guarded
        # child without redline geometry; never retry a crash in-process.
        if detect_legislative_formatting:
            logger.warning(
                "guarded PDF extraction crashed; retrying without legislative geometry",
                exit_code=e.exitcode,
            )
            try:
                return run_guarded(
                    extract_document_file,
                    (
                        document_path,
                        ocr_threshold,
                        ocr_dpi,
                        False,
                        max_ocr_workers,
                    ),
                    timeout=600,
                    rlimit_bytes=int(1.5 * 1024 * 1024 * 1024),
                )
            except GuardTimeout:
                raise ExtractionError(
                    "Document extraction fallback timed out after 600s"
                )
            except GuardCrashed as fallback_error:
                raise ExtractionError(
                    "Document extraction subprocess crashed twice "
                    f"(exit codes {e.exitcode}, {fallback_error.exitcode})"
                )
            except GuardTaskError as fallback_error:
                raise ExtractionError(
                    "Document extraction fallback failed: "
                    f"{fallback_error} ({fallback_error.error_type})"
                )
        raise ExtractionError(
            f"Document extraction subprocess crashed (exit code {e.exitcode})"
        )
    except GuardTaskError as e:
        raise ExtractionError(f"Document extraction failed: {e} ({e.error_type})")


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
        self._recycle_lock = asyncio.Lock()  # Serialize recycle checks
        self._session_in_flight: Dict[aiohttp.ClientSession, int] = {}
        self._retired_sessions: set[aiohttp.ClientSession] = set()
        self._source_acquirer = DocumentSourceAcquirer(
            self._download_url_bytes,
            fetch_errors=(DocumentDownloadError,),
            corpus_getter=lambda: get_corpus(),
            metrics=self.metrics,
            metric_component="processor",
        )
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
        async with self._recycle_lock:
            sessions = set(self._retired_sessions)
            sessions.update(self._session_in_flight)
            if self.http_session is not None:
                sessions.add(self.http_session)
            self.http_session = None
            self._retired_sessions.clear()
            self._session_in_flight.clear()
        for session in sessions:
            if not session.closed:
                await session.close()
        if sessions:
            logger.debug("http sessions closed", count=len(sessions))

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensures cleanup even on exception"""
        await self.close()
        return False  # Don't suppress exceptions

    async def _session_for_download(self) -> aiohttp.ClientSession:
        """Lease a session; retired sessions close after their final request."""
        close_after_unlock: Optional[aiohttp.ClientSession] = None
        async with self._recycle_lock:
            self._request_count += 1
            if self._request_count >= self._recycle_after:
                old_session = self.http_session
                self.http_session = None
                self._request_count = 0
                logger.info("http session rotating", after_requests=self._recycle_after)
                if old_session and not old_session.closed:
                    if self._session_in_flight.get(old_session, 0):
                        self._retired_sessions.add(old_session)
                    else:
                        close_after_unlock = old_session
            session = await self._get_session()
            self._session_in_flight[session] = (
                self._session_in_flight.get(session, 0) + 1
            )
        if close_after_unlock is not None:
            await close_after_unlock.close()
        return session

    async def _release_download_session(
        self, session: aiohttp.ClientSession
    ) -> None:
        close_after_unlock = False
        async with self._recycle_lock:
            remaining = self._session_in_flight.get(session, 0) - 1
            if remaining > 0:
                self._session_in_flight[session] = remaining
            else:
                self._session_in_flight.pop(session, None)
                if session in self._retired_sessions:
                    self._retired_sessions.remove(session)
                    close_after_unlock = True
        if close_after_unlock and not session.closed:
            await session.close()
            logger.debug("retired http session closed after final request")

    async def _sleep_download_retry(self, attempt: int, retry_after: Optional[float]) -> None:
        delay = min(30.0, retry_after if retry_after is not None else 2.0 ** attempt)
        await asyncio.sleep(max(0.0, delay))

    @staticmethod
    def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return max(0.0, min(float(value), 120.0))
        except (TypeError, ValueError):
            return None

    async def _download_url_bytes(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> DocumentResponse:
        """Fetch or conditionally validate one URL with bounded retries."""
        safe_url = attachment_identity(url)
        session = await self._session_for_download()
        vendor = vendor_for_url(url)
        attempts = 3
        conditional_headers = {
            key: value
            for key, value in (
                ("If-None-Match", etag),
                ("If-Modified-Since", last_modified),
            )
            if value
        }
        try:
            for attempt in range(attempts):
                await get_rate_limiter().wait_if_needed(vendor)
                try:
                    request_kwargs: Dict[str, Any] = {
                        "ssl": verify_tls_for_url(url)
                    }
                    if conditional_headers:
                        request_kwargs["headers"] = conditional_headers
                    async with session.get(url, **request_kwargs) as resp:
                        response_url = str(getattr(resp, "url", url))
                        response_etag = resp.headers.get("ETag")
                        response_last_modified = resp.headers.get("Last-Modified")
                        if resp.status == 304 and conditional_headers:
                            return DocumentResponse(
                                data=None,
                                content_type=resp.headers.get("Content-Type", ""),
                                response_url=response_url,
                                etag=response_etag or etag,
                                last_modified=response_last_modified or last_modified,
                            )
                        if resp.status == 412 and conditional_headers:
                            # A validator can become invalid independently of
                            # the content. Retry once as a normal GET rather
                            # than treating the cached revision as authoritative.
                            conditional_headers = {}
                            logger.info(
                                "document validator rejected, retrying unconditionally",
                                url=safe_url[:120],
                            )
                            continue
                        if resp.status != 200:
                            error = DocumentDownloadError(
                                f"HTTP {resp.status} downloading document from {safe_url}",
                                document_url=safe_url,
                                status_code=resp.status,
                            )
                            if error.is_retryable and attempt < attempts - 1:
                                retry_after = self._retry_after_seconds(
                                    resp.headers.get("Retry-After")
                                )
                                logger.warning(
                                    "transient document response, retrying",
                                    url=safe_url[:120],
                                    status=resp.status,
                                    attempt=attempt + 1,
                                )
                                await self._sleep_download_retry(attempt, retry_after)
                                continue
                            raise error
                        raw_bytes = await resp.read()
                        content_type = resp.headers.get("Content-Type", "")
                        return DocumentResponse(
                            data=raw_bytes,
                            content_type=content_type,
                            response_url=response_url,
                            etag=response_etag,
                            last_modified=response_last_modified,
                        )
                except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError) as e:
                    raise DocumentDownloadError(
                        f"TLS verification failed downloading document from {safe_url}",
                        document_url=safe_url,
                        retryable=False,
                        original_error=e,
                    ) from e
                except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, asyncio.TimeoutError) as e:
                    if attempt < attempts - 1:
                        logger.warning(
                            "transient document download failure, retrying",
                            url=safe_url[:120],
                            error_type=type(e).__name__,
                            attempt=attempt + 1,
                        )
                        await self._sleep_download_retry(attempt, None)
                        continue
                    raise DocumentDownloadError(
                        f"Failed to download document from {safe_url}: {type(e).__name__}",
                        document_url=safe_url,
                        original_error=e,
                    ) from e
                except aiohttp.ClientError as e:
                    raise DocumentDownloadError(
                        f"Failed to download document from {safe_url}: {type(e).__name__}",
                        document_url=safe_url,
                        retryable=False,
                        original_error=e,
                    ) from e
        finally:
            await self._release_download_session(session)
        raise DocumentDownloadError(
            f"Failed to download document from {safe_url}",
            document_url=safe_url,
        )

    async def _acquire_document_async(
        self,
        requested_url: str,
        source_url: str,
        banana: Optional[str],
        depth: int,
    ) -> DocumentArtifact:
        artifact = await self._source_acquirer.acquire(
            source_url,
            requested_url=requested_url,
            banana=banana,
        )

        if artifact.document_format is not DocumentFormat.HTML or depth >= 2:
            return artifact

        candidates = extract_document_links(artifact.data, artifact.source_url)
        onbase_alt = None
        if "/Documents/ViewDocument/" in source_url:
            onbase_alt = source_url.replace(
                "/Documents/ViewDocument/", "/Documents/DownloadFileBytes/"
            )
        elif "/Documents/DownloadFileBytes/" in source_url:
            onbase_alt = source_url.replace(
                "/Documents/DownloadFileBytes/", "/Documents/ViewDocument/"
            )
        if onbase_alt and onbase_alt not in candidates:
            candidates.append(onbase_alt)

        transient_error: Optional[DocumentDownloadError] = None
        for candidate in candidates[:3]:
            try:
                resolved = await self._acquire_document_async(
                    requested_url, candidate, banana, depth + 1
                )
                if resolved.document_format is not DocumentFormat.HTML:
                    logger.info(
                        "html attachment page resolved to document",
                        original_url=attachment_identity(source_url)[:120],
                        resolved_url=resolved.source_identity[:120],
                        document_format=resolved.document_format.value,
                    )
                    return resolved
            except DocumentDownloadError as e:
                if e.is_retryable:
                    transient_error = e
                else:
                    logger.debug(
                        "document link was unavailable",
                        url=attachment_identity(candidate)[:120],
                        status=e.status_code,
                    )
        if transient_error is not None:
            raise transient_error
        return artifact

    async def acquire_document_async(
        self, url: str, banana: Optional[str] = None
    ) -> DocumentArtifact:
        """Acquire a typed artifact through the shared source boundary."""
        return await self._acquire_document_async(url, url, banana, 0)

    async def download_pdf_async(self, url: str, _depth: int = 0) -> bytes:
        """Compatibility byte download for document-only consumers."""
        artifact = (
            await self.acquire_document_async(url)
            if _depth == 0
            else await self._acquire_document_async(url, url, None, _depth)
        )
        if artifact.document_format is DocumentFormat.HTML:
            safe_url = attachment_identity(url)
            raise DocumentDownloadError(
                f"Attachment page contained no downloadable document: {safe_url[:120]}",
                document_url=safe_url,
                retryable=False,
            )
        return artifact.data

    async def extract_document_async(
        self, url: str, banana: Optional[str] = None
    ) -> Dict[str, Any]:
        """Acquire and extract a supported document or useful HTML fallback."""
        safe_url = attachment_identity(url)
        artifact = await self.acquire_document_async(url, banana=banana)
        corpus_store = get_corpus()

        if corpus_store:
            cached = await corpus_store.lookup_extraction(artifact.content_sha256)
            if cached:
                await corpus_store.record_sighting(
                    artifact.content_sha256, artifact.source_url, banana
                )
                logger.info(
                    "extraction served from corpus",
                    url=safe_url[:100],
                    sha=artifact.content_sha256[:16],
                    chars=len(cached.get("text") or ""),
                )
                cached.update(
                    content_sha256=artifact.content_sha256,
                    corpus_persisted=True,
                    document_format=artifact.document_format.value,
                    source_url=artifact.source_identity,
                    from_corpus=True,
                )
                return cached

        content_sha256 = artifact.content_sha256
        document_format = artifact.document_format
        source_identity = artifact.source_identity
        temporary_suffix = artifact.suffix

        if document_format is DocumentFormat.HTML:
            html_bytes = artifact.data
            del artifact
            text = await asyncio.to_thread(sanitize_html_text, html_bytes)
            del html_bytes
            if not text:
                raise ExtractionError(
                    f"HTML attachment page contained no usable text: {safe_url[:100]}",
                    document_url=safe_url,
                    document_type="html",
                )
            result: Dict[str, Any] = {
                "success": True,
                "text": text,
                "method": "html_sanitized",
                "page_count": 0,
                "ocr_pages": 0,
                "extraction_time": 0.0,
            }
        else:
            fd, document_path = tempfile.mkstemp(suffix=temporary_suffix)
            try:
                with os.fdopen(fd, "wb") as temporary:
                    temporary.write(artifact.data)
                # The guarded child reopens the tempfile. Retaining the immutable
                # artifact here would keep a second full document copy resident
                # in the parent for the child's entire (up to 620s) lifetime.
                del artifact
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        _extract_pdf_in_subprocess,
                        document_path,
                        self.pdf_extractor.ocr_threshold,
                        self.pdf_extractor.ocr_dpi,
                        self.pdf_extractor.detect_legislative_formatting,
                        self.pdf_extractor.max_ocr_workers,
                    ),
                    timeout=620,
                )
            except asyncio.TimeoutError:
                logger.error("document extraction timed out", url=safe_url[:100])
                raise ExtractionError(
                    f"Document extraction timed out: {safe_url[:100]}",
                    document_url=safe_url,
                    document_type=document_format.value,
                )
            finally:
                try:
                    os.unlink(document_path)
                except OSError:
                    pass

        if not result.get("success"):
            raise ExtractionError(
                f"Document extraction failed: {result.get('error', 'Unknown error')}"
            )

        corpus_persisted = False
        if corpus_store:
            corpus_persisted = await corpus_store.persist_extraction(
                content_sha256, result
            )
        result.update(
            content_sha256=content_sha256,
            corpus_persisted=corpus_persisted,
            document_format=document_format.value,
            source_url=source_identity,
        )
        logger.debug(
            "document extracted",
            url=safe_url,
            document_format=document_format.value,
            pages=result.get("page_count", 0),
        )
        return result

    async def extract_pdf_async(self, url: str, banana: Optional[str] = None) -> Dict[str, Any]:
        """Compatibility adapter for callers migrating to typed artifacts."""
        return await self.extract_document_async(url, banana=banana)

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
            result = await self.extract_document_async(url, banana=banana)

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
