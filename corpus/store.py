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
from typing import Any, BinaryIO, Dict, Optional

from config import config, get_logger
from corpus.r2 import R2Client
from database.repositories_async.document_blobs import DocumentBlobRepository
from pipeline.utils import attachment_identity

logger = get_logger(__name__).bind(component="corpus")

# Provenance tag stamped on every extraction this code writes. Bump when the
# extractor materially changes (Tesseract -> VLM OCR, Layout adoption):
# lookup_extraction treats rows from other versions as misses, so re-extraction
# happens lazily exactly where documents are touched again.
EXTRACT_VERSION = "1"

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
    return "application/octet-stream"


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
                await self._record_source(content_sha256, source_url, banana)
                return False

            if file_obj is not None:
                head = file_obj.read(8)
                file_obj.seek(0)
            else:
                head = (data or b"")[:8]
            content_type = _sniff_content_type(head)

            await self.blobs.upsert_blob(content_sha256, byte_count, content_type)

            existing = await self.blobs.get_blob(content_sha256)
            archived = bool(existing and existing.get("original_key"))
            if not archived:
                key = _ORIGINAL_PREFIX + content_sha256
                start = time.monotonic()
                # The content hash IS the payload hash -- SigV4 signs the
                # true body digest for free because content addressing
                # already computed it.
                await self.r2.put(
                    key,
                    file_obj if file_obj is not None else data,
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

            await self._record_source(content_sha256, source_url, banana)
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
            if blob.get("extract_version") != EXTRACT_VERSION:
                return None  # older extractor produced this; re-extract fresh

            text_bytes = await self.r2.get(blob["text_key"])
            if text_bytes is None:
                logger.warning(
                    "corpus index points at missing text object",
                    sha=content_sha256[:16],
                    key=blob["text_key"],
                )
                return None

            return {
                "success": True,
                "text": text_bytes.decode("utf-8", errors="replace"),
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

    async def record_sighting(
        self, content_sha256: str, source_url: Optional[str], banana: Optional[str] = None
    ) -> None:
        """Remember a URL identity for already-known bytes (corpus-hit path)."""
        try:
            await self._record_source(content_sha256, source_url, banana)
        except Exception as e:
            logger.warning(
                "corpus source record failed",
                sha=content_sha256[:16],
                error=str(e),
                error_type=type(e).__name__,
            )

    async def _record_source(
        self, content_sha256: str, source_url: Optional[str], banana: Optional[str]
    ) -> None:
        if source_url:
            await self.blobs.record_source(
                content_sha256, attachment_identity(source_url), banana
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
