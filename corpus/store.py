"""Ground-truth corpus store: archive originals, persist text, serve reads.

The law (docs/CORPUS_ARCHITECTURE.md): extraction writes once, everything
downstream reads. This store is the write/read surface over the two R2
prefixes (originals/<sha256>, text/<sha256>.txt) and the document_blob
pointer index. Content addressing by sha256(source bytes) makes writes
idempotent: two workers teeing the same bytes converge on one object and
one row, so the interim double-tee (sync chunker + process extraction) is
safe by construction.

Failure policy: the corpus is a passenger, never the driver. Every public
method traps its own errors and degrades to "no corpus" (lookup misses,
archive/persist no-ops) with a warning -- an R2 outage must never fail a
sync or a summary.

Deliberately a module-level singleton (the AsyncSessionManager /
rate-limiter pattern): vendor adapters have no DB handle to thread a store
through, and there is exactly one corpus.
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, BinaryIO, Dict, Optional

from config import config, get_logger
from corpus.r2 import R2Client
from database.repositories_async.document_blobs import DocumentBlobRepository
from pipeline.utils import attachment_identity
from parsing.text_quality import is_garbled_text_layer

logger = get_logger(__name__).bind(component="corpus")

# Provenance tag stamped on every extraction this code writes. Bump when the
# extractor materially changes (Tesseract -> VLM OCR, Layout adoption):
# lookup_extraction treats rows from other versions as misses, so re-extraction
# happens lazily exactly where documents are touched again.
EXTRACT_VERSION = "2"
_COMPATIBLE_EXTRACT_VERSIONS = frozenset({"1", EXTRACT_VERSION})

_ORIGINAL_PREFIX = "originals/"
_TEXT_PREFIX = "text/"


def sha256_hex(data: bytes) -> str:
    """The corpus identity primitive: hash of the source bytes themselves.

    Everything else in the pipeline hashes metadata (URL+name); this is the
    only place identity comes from content. Dedup key and address key in one.
    """
    return hashlib.sha256(data).hexdigest()


def _sniff_content_type(head: bytes) -> str:
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"{\\rtf"):
        return "application/rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "application/msword"
    if head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if head.lstrip().lower().startswith((b"<!doctype", b"<html")):
        return "text/html; charset=utf-8"
    return "application/octet-stream"


@dataclass(frozen=True, slots=True)
class CorpusOriginal:
    """Archived source bytes with their content-addressed provenance."""

    data: bytes
    content_sha256: str
    content_type: Optional[str]
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_observed_at: Optional[datetime] = None
    last_validated_at: Optional[datetime] = None
    last_validation_attempt_at: Optional[datetime] = None

    def needs_revalidation(
        self,
        *,
        max_age_seconds: int,
        failure_retry_seconds: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """Return whether origin validation is due for this source revision."""
        reference = self.last_validated_at or self.last_validation_attempt_at
        current = now or datetime.now(reference.tzinfo if reference else None)
        if (
            self.last_validated_at is not None
            and current - self.last_validated_at
            < timedelta(seconds=max(0, max_age_seconds))
        ):
            return False

        # Only a post-validation attempt represents a failed refresh. A recent
        # successful validation is handled by the freshness check above.
        if (
            self.last_validation_attempt_at is not None
            and (
                self.last_validated_at is None
                or self.last_validation_attempt_at > self.last_validated_at
            )
            and current - self.last_validation_attempt_at
            < timedelta(seconds=max(0, failure_retry_seconds))
        ):
            return False
        return True


class CorpusStore:
    """Write/read surface over R2 blobs + the document_blob index."""

    def __init__(self, blobs: DocumentBlobRepository, r2: R2Client):
        self.blobs = blobs
        self.r2 = r2

    async def close(self) -> None:
        await self.r2.close()

    async def archive_original(
        self,
        content_sha256: str,
        *,
        byte_count: int,
        data: Optional[bytes] = None,
        file_obj: Optional[BinaryIO] = None,
        source_url: Optional[str] = None,
        banana: Optional[str] = None,
        content_type: Optional[str] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> bool:
        """Stage 1's archive step: ensure these bytes exist in the corpus.

        Pass the content as `data` (bytes) or `file_obj` (seekable binary
        handle -- preferred for large documents so the caller can release the
        bytes first). Skips the upload when the blob is already archived;
        always records the source sighting. Returns True when the blob is
        known archived after the call.
        """
        try:
            if data is None and file_obj is None:
                raise ValueError("archive_original needs data or file_obj")

            if byte_count > config.CORPUS_MAX_ORIGINAL_BYTES:
                # Still index the blob (hash, size, sources) so dedup works;
                # the REST endpoint can't carry the object itself.
                logger.warning(
                    "original exceeds corpus upload cap, indexing without archive",
                    sha=content_sha256[:16],
                    bytes=byte_count,
                )
                await self.blobs.upsert_blob(content_sha256, byte_count)
                await self._record_validation(
                    content_sha256,
                    source_url,
                    banana,
                    etag=etag,
                    last_modified=last_modified,
                )
                return False

            if file_obj is not None:
                head = file_obj.read(8)
                file_obj.seek(0)
            else:
                head = (data or b"")[:8]
            content_type = content_type or _sniff_content_type(head)

            await self.blobs.upsert_blob(content_sha256, byte_count, content_type)

            existing = await self.blobs.get_blob(content_sha256)
            archived = bool(existing and existing.get("original_key"))
            if not archived:
                key = _ORIGINAL_PREFIX + content_sha256
                start = time.monotonic()
                payload = file_obj if file_obj is not None else data
                if payload is None:  # narrowed above; defensive for type/runtime drift
                    raise ValueError("archive_original payload disappeared")
                # The content hash IS the payload hash -- SigV4 signs the
                # true body digest for free because content addressing
                # already computed it.
                await self.r2.put(
                    key,
                    payload,
                    content_type,
                    payload_sha256=content_sha256,
                    content_length=byte_count,
                )
                await self.blobs.mark_original_archived(content_sha256, key)
                logger.info(
                    "archived original to corpus",
                    sha=content_sha256[:16],
                    bytes=byte_count,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            await self._record_validation(
                content_sha256,
                source_url,
                banana,
                etag=etag,
                last_modified=last_modified,
            )
            return True
        except Exception as e:
            logger.warning(
                "corpus archive failed, pipeline continues",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

    async def persist_extraction(self, content_sha256: str, result: Dict[str, Any]) -> bool:
        """Stage 2's write-once: persist extracted text + provenance.

        `result` is the PdfExtractor result dict (success/text/method/
        page_count/ocr_pages). No-ops on unsuccessful or empty extractions.
        """
        try:
            text = result.get("text") or ""
            if not result.get("success") or not text:
                await self.blobs.record_extraction_failure(
                    content_sha256,
                    error_type=str(result.get("error_type") or "ExtractionFailed"),
                    error_message=str(
                        result.get("error")
                        or ("extraction returned empty text" if not text else "extraction failed")
                    ),
                )
                return False

            # Write-once means first-writer-wins per extractor version: sync
            # and process can both manufacture text for the same bytes, and a
            # re-persist would only replace equivalent content (or downgrade
            # OCR text with a text-layer pass). Skip when current-version
            # text already exists.
            existing = await self.blobs.get_blob(content_sha256)
            if (
                existing
                and existing.get("text_key")
                and existing.get("extract_version") == EXTRACT_VERSION
                and not str(existing.get("extract_method") or "").endswith("-partial")
            ):
                return True

            key = _TEXT_PREFIX + content_sha256 + ".txt"
            start = time.monotonic()
            await self.r2.put(key, text.encode("utf-8"), "text/plain; charset=utf-8")
            await self.blobs.set_extraction(
                content_sha256,
                text_key=key,
                extract_method=result.get("method"),
                extract_version=EXTRACT_VERSION,
                page_count=result.get("page_count"),
                ocr_page_count=result.get("ocr_pages"),
                text_chars=len(text),
                extraction_status=(
                    "partial"
                    if str(result.get("method") or "").endswith("-partial")
                    or int(result.get("ocr_pending") or 0) > 0
                    else "succeeded"
                ),
            )
            logger.info(
                "persisted extraction to corpus",
                sha=content_sha256[:16],
                chars=len(text),
                method=result.get("method"),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            return True
        except Exception as e:
            try:
                await self.blobs.record_extraction_failure(
                    content_sha256,
                    error_type=type(e).__name__,
                    error_message=str(e),
                )
            except Exception:
                pass
            logger.warning(
                "corpus text persist failed, pipeline continues",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )
            return False

    async def lookup_extraction(self, content_sha256: str) -> Optional[Dict[str, Any]]:
        """The dedup gate: return a ready extraction result for these bytes,
        or None if the corpus can't serve one (unknown hash, no text yet,
        stale extract_version, R2 hiccup). Shaped exactly like a fresh
        PdfExtractor result so callers can't tell the difference -- except
        for the from_corpus marker.
        """
        try:
            blob = await self.blobs.get_blob(content_sha256)
            if not blob or not blob.get("text_key"):
                return None
            extract_version = blob.get("extract_version")
            if extract_version not in _COMPATIBLE_EXTRACT_VERSIONS:
                return None  # older extractor produced this; re-extract fresh
            if str(blob.get("extract_method") or "").endswith("-partial"):
                logger.info(
                    "partial corpus extraction requires OCR retry",
                    sha=content_sha256[:16],
                    extract_method=blob.get("extract_method"),
                )
                return None

            text_bytes = await self.r2.get(blob["text_key"])
            if text_bytes is None:
                logger.warning(
                    "corpus index points at missing text object",
                    sha=content_sha256[:16],
                    key=blob["text_key"],
                )
                return None

            text = text_bytes.decode("utf-8", errors="replace")
            # Version 2 adds broken-font/CMap repair. Keep serving every clean
            # v1 extraction so this targeted upgrade does not force a corpus-
            # wide re-extraction; only demonstrably garbled legacy text misses.
            if extract_version != EXTRACT_VERSION and is_garbled_text_layer(text):
                logger.info(
                    "legacy corpus extraction requires garbled-text repair",
                    sha=content_sha256[:16],
                    extract_version=extract_version,
                )
                return None

            return {
                "success": True,
                "text": text,
                "method": blob.get("extract_method"),
                "page_count": blob.get("page_count") or 0,
                "ocr_pages": blob.get("ocr_page_count") or 0,
                "extraction_time": 0.0,
                "from_corpus": True,
            }
        except Exception as e:
            logger.warning(
                "corpus lookup failed, falling back to extraction",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    async def get_original_artifact_by_identity(
        self, source_url: str
    ) -> Optional[CorpusOriginal]:
        """Fetch archived original bytes for a URL identity -- the well.

        Resolves to the NEWEST blob seen at the identity (a URL that served
        revised bytes gets its latest revision).  The typed return keeps the
        database's content identity/media metadata attached to the bytes so
        acquisition does not need to hash a corpus hit again.
        """
        try:
            blob = await self.blobs.get_blob_for_identity(attachment_identity(source_url))
            if not blob or not blob.get("original_key"):
                return None
            data = await self.r2.get(blob["original_key"])
            if data is None:
                return None
            return CorpusOriginal(
                data=data,
                content_sha256=blob["content_sha256"],
                content_type=blob.get("content_type"),
                etag=blob.get("etag"),
                last_modified=blob.get("last_modified"),
                last_observed_at=blob.get("last_observed_at"),
                last_validated_at=blob.get("last_validated_at"),
                last_validation_attempt_at=blob.get("last_validation_attempt_at"),
            )
        except Exception as e:
            logger.warning(
                "corpus original fetch failed, caller falls back to download",
                url=source_url[:120],
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    async def get_original_by_identity(self, source_url: str) -> Optional[bytes]:
        """Compatibility read returning bytes only.

        New acquisition code should use :meth:`get_original_artifact_by_identity`
        to preserve content identity and media metadata across the boundary.
        """
        original = await self.get_original_artifact_by_identity(source_url)
        return original.data if original else None

    async def record_sighting(
        self, content_sha256: str, source_url: Optional[str], banana: Optional[str] = None
    ) -> None:
        """Record a cache observation without advancing origin freshness."""
        try:
            if source_url:
                await self.blobs.record_source_observation(
                    content_sha256, attachment_identity(source_url), banana
                )
        except Exception as e:
            logger.warning(
                "corpus source record failed",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )

    async def record_validation(
        self,
        content_sha256: str,
        source_url: Optional[str],
        banana: Optional[str] = None,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """Record a successful conditional or full origin validation."""
        try:
            await self._record_validation(
                content_sha256,
                source_url,
                banana,
                etag=etag,
                last_modified=last_modified,
            )
        except Exception as e:
            logger.warning(
                "corpus validation record failed",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )

    async def record_validation_failure(
        self,
        content_sha256: str,
        source_url: Optional[str],
        banana: Optional[str] = None,
    ) -> None:
        """Record a failed origin check so fail-open reads back off."""
        if not source_url:
            return
        try:
            await self.blobs.record_source_validation_failure(
                content_sha256, attachment_identity(source_url), banana
            )
        except Exception as e:
            logger.warning(
                "corpus validation failure record failed",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )

    async def _record_validation(
        self,
        content_sha256: str,
        source_url: Optional[str],
        banana: Optional[str],
        *,
        etag: Optional[str],
        last_modified: Optional[str],
    ) -> None:
        if source_url:
            await self.blobs.record_source_validation(
                content_sha256,
                attachment_identity(source_url),
                banana,
                etag=etag,
                last_modified=last_modified,
            )


# ---------------------------------------------------------------------------
# Singleton wiring. Database.__init__ calls init_corpus with its repository;
# consumers (analyzer, vendor adapters) call get_corpus() and treat None as
# "corpus off" -- which is exactly what tests and CORPUS_ENABLED=false get.
# ---------------------------------------------------------------------------

_store: Optional[CorpusStore] = None


def init_corpus(blobs: DocumentBlobRepository) -> Optional[CorpusStore]:
    global _store
    if not config.CORPUS_ENABLED:
        return None
    if not (
        config.CLOUDFLARE_ACCOUNT_ID
        and config.R2_ACCESS_KEY_ID
        and config.R2_SECRET_ACCESS_KEY
    ):
        logger.warning("corpus enabled but R2 data-plane credentials missing, corpus off")
        return None
    _store = CorpusStore(
        blobs,
        R2Client(
            account_id=config.CLOUDFLARE_ACCOUNT_ID,
            access_key_id=config.R2_ACCESS_KEY_ID,
            secret_access_key=config.R2_SECRET_ACCESS_KEY,
            bucket=config.CORPUS_BUCKET,
        ),
    )
    logger.info("corpus store initialized", bucket=config.CORPUS_BUCKET)
    return _store


def get_corpus() -> Optional[CorpusStore]:
    return _store


async def close_corpus() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None
