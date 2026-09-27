"""Corpus-aware acquisition shared by sync and processing document paths.

Callers own transport policy (authentication, rate limiting, retries, TLS) and
provide a small loader returning :class:`DocumentResponse`.  This module owns
the invariant that every source identity follows the same corpus freshness,
conditional-validation, fail-open, archival, and single-flight behavior.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
import time
import hashlib
import tempfile
import zipfile
from urllib.parse import urlparse
from typing import Optional

from config import config, get_logger
from corpus.store import CorpusStore, sha256_hex
from pipeline.document_artifacts import DocumentArtifact, make_artifact
from pipeline.protocols import MetricsCollector, NullMetrics
from pipeline.utils import attachment_identity, canonical_fetch_url

logger = get_logger(__name__).bind(component="document_acquisition")


@dataclass(frozen=True, slots=True)
class DocumentResponse:
    """Transport-neutral document response, including HTTP validators."""

    data: Optional[bytes]
    content_type: str
    response_url: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None

    @property
    def not_modified(self) -> bool:
        return self.data is None


DocumentLoader = Callable[
    [str, Optional[str], Optional[str]], Awaitable[DocumentResponse]
]
CorpusGetter = Callable[[], Optional[CorpusStore]]


def _original_media_type(handle, head: bytes, declared: str) -> str:
    """Classify streamed originals without extracting their contents.

    ZIP directory names distinguish Office formats without loading the full
    document. Unknown binary formats are still worth preserving.
    """
    if b"%PDF-" in head[:1024]:
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            handle.seek(0)
            with zipfile.ZipFile(handle) as archive:
                names = set(archive.namelist())
            for entry, media in (
                ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            ):
                if entry in names:
                    return media
        except zipfile.BadZipFile:
            pass
        return "application/zip"
    if head.startswith(b"{\\rtf"):
        return "application/rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE containers may be DOC, XLS, or PPT; don't invent a subtype.
        return "application/x-ole-storage"
    for magic, media in ((b"\x89PNG\r\n\x1a\n", "image/png"),
                         (b"\xff\xd8\xff", "image/jpeg"),
                         (b"GIF8", "image/gif"),
                         (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff")):
        if head.startswith(magic):
            return media
    return declared if declared and "application/pdf" not in declared.lower() else "application/octet-stream"


class ArchiveDocumentUnavailable(RuntimeError):
    """A concrete document URL returned a terminal access/not-found status."""

    def __init__(self, status_code, *, cached=False):
        self.status_code = status_code
        suffix = " (cached for this source run)" if cached else ""
        super().__init__(f"Document returned HTTP {status_code}{suffix}")


class DocumentSourceAcquirer:
    """Acquire one source through shared freshness and single-flight policy.

    The instance is intentionally scoped to one transport owner (an analyzer or
    vendor adapter).  It collapses duplicate work within that owner while the
    corpus's content addressing converges work across owners and processes.
    """

    def __init__(
        self,
        loader: DocumentLoader,
        *,
        fetch_errors: tuple[type[Exception], ...],
        corpus_getter: CorpusGetter,
        metrics: Optional[MetricsCollector] = None,
        metric_component: str,
        stream_loader=None,
    ) -> None:
        self._loader = loader
        self._stream_loader = stream_loader
        self._fetch_errors = fetch_errors
        self._corpus_getter = corpus_getter
        self._metrics = metrics or NullMetrics()
        self._metric_component = metric_component
        self._tasks: dict[str, asyncio.Task[DocumentArtifact]] = {}
        self._archive_missing: OrderedDict[str, int] = OrderedDict()

    async def acquire(
        self,
        source_url: str,
        *,
        requested_url: Optional[str] = None,
        banana: Optional[str] = None,
    ) -> DocumentArtifact:
        """Return a typed artifact, joining concurrent work for this identity."""
        # Resolve viewer-only routes to the URL that serves bytes before
        # anything keys off it. Done here rather than at each call site so
        # every caller -- sync, processing, backfills, replays -- gets the
        # document instead of a portal's JavaScript shell, and so the corpus
        # keys the bytes under the URL they actually came from. Pin the
        # caller's URL as requested_url first: it stays the durable
        # human-facing link on the artifact even though we fetch elsewhere.
        requested_url = requested_url or source_url
        source_url = canonical_fetch_url(source_url)
        identity = attachment_identity(source_url)
        task = self._tasks.get(identity)
        joined_existing = task is not None
        if task is None:
            task = asyncio.create_task(
                self._acquire_once(
                    requested_url=requested_url,
                    source_url=source_url,
                    banana=banana,
                )
            )
            self._tasks[identity] = task
            task.add_done_callback(
                lambda completed, key=identity: self._release(key, completed)
            )

        artifact = await asyncio.shield(task)
        # Do not wait for the scheduled done callback to release a completed
        # task: Task.result() retains the full artifact byte buffer.
        self._release(identity, task)
        if joined_existing:
            corpus_store = self._corpus_getter()
            if corpus_store:
                await corpus_store.record_sighting(
                    artifact.content_sha256, source_url, banana
                )
            self._record_metric("singleflight_join", artifact, 0.0)

        # Preserve the caller's durable identity when a known portal route is
        # fetched through its API. Existing attachment readers use this alias.
        if canonical_fetch_url(requested_url) != requested_url and artifact.corpus_persisted:
            corpus = self._corpus_getter()
            if corpus is not None:
                await corpus.record_alias(artifact.content_sha256, source_url, requested_url, banana)

        if artifact.requested_url != requested_url:
            return replace(artifact, requested_url=requested_url)
        return artifact

    async def archive(self, source_url: str, *, banana=None, max_bytes=2 * 1024**3,
                      document_semaphore=None):
        """Archive an original without extraction or loading its bytes into RAM.

        Historical acquisition reuses the latest archived revision, recording
        this source sighting. Normal acquire() retains freshness revalidation.
        Returns the durable corpus receipt; an indexed-only blob is not success.
        """
        corpus = self._corpus_getter()
        if corpus is None:
            raise RuntimeError("Original archival requires an enabled corpus")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        requested_url = source_url
        source_url = canonical_fetch_url(source_url)
        if urlparse(source_url).scheme not in {"http", "https"}:
            raise ValueError("Document source must be HTTP(S)")
        blob = await corpus.blobs.get_blob_for_identity(attachment_identity(source_url))
        if blob and blob.get("original_key") and (blob.get("bytes") or 0) > 0:
            await corpus.record_sighting(blob["content_sha256"], source_url, banana)
            if requested_url != source_url:
                await corpus.record_alias(blob["content_sha256"], source_url, requested_url, banana)
            return {"content_sha256": blob["content_sha256"], "bytes": blob["bytes"],
                    "original_key": blob["original_key"], "reused": True,
                    "content_type": blob.get("content_type")}
        if self._stream_loader is None:
            raise RuntimeError("Transport does not provide streaming acquisition")
        # Scope negative results to this adapter/run and the exact fetch URL:
        # a refreshed signed URL must still get its own attempt. Never cache
        # transient failures or exception tracebacks.
        if source_url in self._archive_missing:
            status = self._archive_missing[source_url]
            self._archive_missing.move_to_end(source_url)
            raise ArchiveDocumentUnavailable(status, cached=True)
        async with document_semaphore if document_semaphore is not None else nullcontext():
            return await self._archive_download(
                corpus, source_url, requested_url, banana, max_bytes)

    def _remember_missing(self, source_url, status):
        if status in (403, 404, 410):
            self._archive_missing[source_url] = status
            self._archive_missing.move_to_end(source_url)
            if len(self._archive_missing) > 4096:
                self._archive_missing.popitem(last=False)

    async def _archive_download(self, corpus, source_url, requested_url, banana, max_bytes):
        # One attempt per scheduled visit. The checkpoint owns deferred retries;
        # do not hold a transfer slot through sleeps or repeat body downloads.
        try:
            response = await self._stream_loader(source_url)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            self._remember_missing(source_url, status)
            if status in (403, 404, 410):
                raise ArchiveDocumentUnavailable(status) from exc
            raise
        try:
            if response.status != 200:
                self._remember_missing(source_url, response.status)
                if response.status in (403, 404, 410):
                    raise ArchiveDocumentUnavailable(response.status)
                raise RuntimeError(f"Document returned HTTP {response.status}")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError(f"Document exceeds archive allowance ({length} bytes)")
            digest = hashlib.sha256()
            size = 0
            head = b""
            with tempfile.TemporaryFile() as handle:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError(f"Document exceeds archive allowance ({max_bytes} bytes)")
                    if len(head) < 4096:
                        head += chunk[:4096 - len(head)]
                    digest.update(chunk)
                    await asyncio.to_thread(handle.write, chunk)
                if not size:
                    raise ValueError("Empty document response")
                content_type = response.headers.get("Content-Type", "")
                looks_html = head.lstrip().lower().startswith((b"<!doctype html", b"<html"))
                pdf_expected = ("application/pdf" in content_type.lower()
                                or urlparse(source_url).path.lower().endswith(".pdf"))
                if pdf_expected and looks_html:
                    raise ValueError("Document download returned HTML instead of a file")
                if looks_html and any(marker in head.lower() for marker in (
                    b"access denied", b"just a moment...", b"friendly error page",
                    b"<title>sign in", b"<title>login", b"the request could not be satisfied",
                )):
                    raise ValueError("Document response is an access/error page")
                content_type = await asyncio.to_thread(_original_media_type, handle, head, content_type)
                sha = digest.hexdigest()
                handle.seek(0)
                archived = await corpus.archive_original(
                    sha, byte_count=size, file_obj=handle, source_url=source_url,
                    banana=banana, content_type=content_type,
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                    max_original_bytes=max_bytes,
                )
                if not archived:
                    raise RuntimeError("Corpus upload failed; original is not archived")
            final_url = str(response.url)
            for alias in {requested_url, final_url} - {source_url}:
                await corpus.record_alias(sha, source_url, alias, banana)
            blob = await corpus.blobs.get_blob(sha)
            if not blob or not blob.get("original_key"):
                raise RuntimeError("Corpus did not confirm an original object")
            return {"content_sha256": sha, "bytes": size,
                    "original_key": blob["original_key"], "reused": False,
                    "content_type": blob.get("content_type")}
        finally:
            response.release()

    def _release(
        self, identity: str, task: asyncio.Task[DocumentArtifact]
    ) -> None:
        if self._tasks.get(identity) is task:
            self._tasks.pop(identity, None)

    async def _acquire_once(
        self,
        *,
        requested_url: str,
        source_url: str,
        banana: Optional[str],
    ) -> DocumentArtifact:
        started = time.monotonic()
        corpus_store = self._corpus_getter()
        original = (
            await corpus_store.get_original_artifact_by_identity(source_url)
            if corpus_store
            else None
        )
        if original:
            if corpus_store is None:  # narrowed by the lookup expression above
                raise AssertionError("corpus original loaded without a corpus store")
            artifact = make_artifact(
                requested_url=requested_url,
                source_url=source_url,
                data=original.data,
                content_sha256=original.content_sha256,
                content_type=original.content_type,
                from_corpus=True,
                corpus_persisted=True,
            )
            if not original.needs_revalidation(
                max_age_seconds=config.CORPUS_REVALIDATE_SECONDS,
                failure_retry_seconds=config.CORPUS_REVALIDATE_FAILURE_SECONDS,
            ):
                await corpus_store.record_sighting(
                    artifact.content_sha256, source_url, banana
                )
                self._record_metric("corpus_fresh", artifact, started)
                return artifact

            try:
                response = await self._loader(
                    source_url, original.etag, original.last_modified
                )
            except self._fetch_errors as exc:
                await corpus_store.record_validation_failure(
                    artifact.content_sha256, source_url, banana
                )
                logger.warning(
                    "document revalidation failed, serving corpus revision",
                    url=attachment_identity(source_url)[:120],
                    sha=artifact.content_sha256[:16],
                    error=str(exc),
                )
                self._record_metric("fail_open", artifact, started)
                return artifact

            if response.not_modified:
                await self._record_validation_aliases(
                    corpus_store,
                    artifact.content_sha256,
                    source_url,
                    response,
                    banana,
                )
                self._record_metric("not_modified", artifact, started)
                return artifact

            artifact = await self._artifact_from_response(
                requested_url=requested_url,
                source_url=source_url,
                response=response,
                banana=banana,
                corpus_store=corpus_store,
            )
            outcome = (
                "origin_unchanged"
                if artifact.content_sha256 == original.content_sha256
                else "origin_changed"
            )
            self._record_metric(outcome, artifact, started)
            return artifact

        response = await self._loader(source_url, None, None)
        if response.not_modified:
            raise RuntimeError(
                "unconditional document request returned not-modified without a corpus revision"
            )
        artifact = await self._artifact_from_response(
            requested_url=requested_url,
            source_url=source_url,
            response=response,
            banana=banana,
            corpus_store=corpus_store,
        )
        self._record_metric("origin_miss", artifact, started)
        return artifact

    async def _artifact_from_response(
        self,
        *,
        requested_url: str,
        source_url: str,
        response: DocumentResponse,
        banana: Optional[str],
        corpus_store: Optional[CorpusStore],
    ) -> DocumentArtifact:
        data = response.data
        if data is None:  # narrowed by callers
            raise RuntimeError("document response had no body")
        content_sha256 = await asyncio.to_thread(sha256_hex, data)
        artifact = make_artifact(
            requested_url=requested_url,
            source_url=response.response_url,
            data=data,
            content_sha256=content_sha256,
            content_type=response.content_type,
        )
        if not corpus_store:
            return artifact

        corpus_persisted = await corpus_store.archive_original(
            content_sha256,
            byte_count=len(data),
            data=data,
            source_url=source_url,
            banana=banana,
            content_type=artifact.media_type,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        if (
            attachment_identity(response.response_url)
            != attachment_identity(source_url)
        ):
            await corpus_store.record_validation(
                content_sha256,
                response.response_url,
                banana,
                etag=response.etag,
                last_modified=response.last_modified,
            )
        return replace(artifact, corpus_persisted=corpus_persisted)

    @staticmethod
    async def _record_validation_aliases(
        corpus_store: CorpusStore,
        content_sha256: str,
        source_url: str,
        response: DocumentResponse,
        banana: Optional[str],
    ) -> None:
        await corpus_store.record_validation(
            content_sha256,
            source_url,
            banana,
            etag=response.etag,
            last_modified=response.last_modified,
        )
        if (
            attachment_identity(response.response_url)
            != attachment_identity(source_url)
        ):
            await corpus_store.record_validation(
                content_sha256,
                response.response_url,
                banana,
                etag=response.etag,
                last_modified=response.last_modified,
            )

    def _record_metric(
        self,
        outcome: str,
        artifact: DocumentArtifact,
        started: float,
    ) -> None:
        labels = {
            "component": self._metric_component,
            "outcome": outcome,
            "document_type": artifact.document_format.value,
        }
        self._metrics.document_acquisitions.labels(**labels).inc()
        if started:
            self._metrics.document_acquisition_duration.labels(**labels).observe(
                time.monotonic() - started
            )
