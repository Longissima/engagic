"""The single produce-ground-truth stage (stage 2 of docs/CORPUS_ARCHITECTURE.md).

One function turns document bytes into shape + text: archive the original
(stage 1 tee), run the chunker cascade in a resource-capped subprocess, and
persist provably-complete text to the corpus. Both callers -- the sync-side
base adapter and the processor's shape-manufacturing step -- delegate here,
so there is exactly ONE write path for ground truth. Extracted from
base_adapter_async._chunk_pdf_bytes when chunking stopped being a sync-only
concern: two producers coordinating through first-writer-wins was a treaty,
not an architecture.

Vendor/slug feed the sticky per-city rung hints; banana feeds corpus
provenance. Corpus failures never propagate -- the store swallows its own.
"""

import asyncio
import os
import tempfile
import time
from typing import Any, Dict, Optional
from urllib.parse import urldefrag

from config import config, get_logger
from corpus.store import get_corpus, sha256_hex
from parsing.subprocess_guard import GuardCrashed, GuardTimeout, run_guarded
from vendors.adapters.parsers.router import (
    ChunkResult,
    ENGINE_ERROR,
    MIN_PDF_BYTES,
    TIMEOUT,
    TOO_SMALL,
    Attempt,
    chunk_pdf,
    get_city_hint,
    recover_pdf_text,
    set_city_hint,
)

logger = get_logger(__name__).bind(component="ground_truth")

# Chunker subprocess budget. Chunking is text-layer work (OCR never runs in
# the chunker child), so 1GB catches runaway parses -- a broken CMap spewing
# gigabytes -- without touching legitimate big packets. The concurrency gate
# is global across callers and lives per event loop: sync fans out
# CITY_SYNC_CONCURRENCY wide per vendor across parallel vendors, and without
# this gate a busy sync could spawn dozens of children on a 3.8GB box.
_CHUNK_RLIMIT_BYTES = 1024 * 1024 * 1024
_chunk_guard_sems: Dict[Any, asyncio.Semaphore] = {}


def _chunk_guard_semaphore() -> asyncio.Semaphore:
    """The per-loop chunker-children gate (CLI runs and tests each get their
    own loop; an asyncio.Semaphore cannot cross loops)."""
    loop = asyncio.get_running_loop()
    sem = _chunk_guard_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(config.CHUNKER_SUBPROCESS_CONCURRENCY)
        _chunk_guard_sems[loop] = sem
    return sem


def _run_chunk_with_recovery(
    tmp_path: str,
    ladder: str,
    hint: Optional[str],
) -> tuple[ChunkResult, Optional[Dict[str, Any]]]:
    """Run the normal guard and its one reduced retry in the same worker.

    Keeping both blocking guard calls inside one ``to_thread`` job also
    avoids depending on executor rescheduling after an exception.
    """
    guard_started = time.monotonic()
    try:
        return run_guarded(
            chunk_pdf,
            (tmp_path, ladder, hint),
            timeout=config.CHUNKER_TIMEOUT_SECONDS,
            rlimit_bytes=_CHUNK_RLIMIT_BYTES,
        ), None
    except GuardCrashed as first_crash:
        remaining = config.CHUNKER_TIMEOUT_SECONDS - (
            time.monotonic() - guard_started
        )
        if remaining < 1.0:
            raise
        logger.warning(
            "chunker child crashed; trying guarded text-only recovery",
            exitcode=first_crash.exitcode,
            remaining_seconds=round(remaining, 1),
        )
        result = run_guarded(
            recover_pdf_text,
            (tmp_path, ladder),
            timeout=remaining,
            rlimit_bytes=_CHUNK_RLIMIT_BYTES,
        )
        return result, {
            "trigger": "crash",
            "exitcode": first_crash.exitcode,
            "path": "text_only",
        }


async def archive_bytes(
    pdf_bytes: bytes,
    source_url: Optional[str] = None,
    banana: Optional[str] = None,
) -> Optional[str]:
    """Stage-1 tee: content-hash and archive bytes to the corpus.

    Returns the content sha (None when the corpus is off). Safe under
    concurrency and cheap on re-encounter -- identical bytes dedup to a
    hash lookup.
    """
    corpus_store = get_corpus()
    if not corpus_store:
        return None
    content_sha256 = await asyncio.to_thread(sha256_hex, pdf_bytes)
    await corpus_store.archive_original(
        content_sha256,
        byte_count=len(pdf_bytes),
        data=pdf_bytes,
        source_url=source_url,
        banana=banana,
    )
    return content_sha256


async def produce_ground_truth(
    pdf_bytes: bytes,
    *,
    vendor: str,
    slug: str,
    ladder: str = "auto",
    source_url: Optional[str] = None,
    banana: Optional[str] = None,
    archived_content_sha256: Optional[str] = None,
) -> ChunkResult:
    """Bytes in; shape + persisted text out. The one write path.

    Archives the original, runs the chunker cascade in the guarded child
    (timeout -> failure_reason "timeout", crash -> "engine_error"), and
    persists the child's ground-truth text when it is provably complete
    (ocr_pending == 0 -- no page would have OCR'd in the process extractor
    either). Routing policy lives in router.LADDERS; every rung attempt and
    any terminal failure reason ends up in the result's audit trail.
    """
    if len(pdf_bytes) < MIN_PDF_BYTES:
        return ChunkResult(failure_reason=TOO_SMALL, ladder=ladder)

    content_sha256 = archived_content_sha256
    if content_sha256 is None:
        content_sha256 = await archive_bytes(pdf_bytes, source_url, banana)

    hint = get_city_hint(vendor, slug, ladder)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
            tmp.write(pdf_bytes)
        # The guarded child reads the tempfile. Drop the parent-side byte
        # buffer before waiting for a potentially long-running parser.
        pdf_bytes = b""

        # The guarded dispatch (parsing/subprocess_guard.py): chunk_pdf --
        # and its ground-truth text pass -- runs in a resource-capped
        # child. A pathological page wedges one child for at most the
        # timeout, never the pipeline (the 2026-06-29 freeze class).
        async with _chunk_guard_semaphore():
            result, guard_recovery = await asyncio.to_thread(
                _run_chunk_with_recovery,
                tmp_path,
                ladder,
                hint,
            )
            if guard_recovery:
                result.quality["guard_recovery"] = guard_recovery

    except GuardTimeout as e:
        # Distinct from ENGINE_ERROR: timeouts are the freeze telemetry.
        result = ChunkResult(failure_reason=TIMEOUT, ladder=ladder)
        result.attempts.append(
            Attempt(rung="cascade", failure_reason=TIMEOUT, error=str(e))
        )
    except Exception as e:
        # GuardCrashed/GuardTaskError and anything else: the cascade
        # failed as a unit. Same classification as a chunker raise.
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

    if result.winning_rung:
        set_city_hint(vendor, slug, ladder, result.winning_rung)

    if source_url:
        _attach_incomplete_source_links(result, source_url)

    # Stage-2 write-once: persist the child's ground-truth text when it is
    # provably complete. Process extraction then serves these bytes from the
    # corpus instead of re-extracting.
    corpus_store = get_corpus()
    if (
        corpus_store
        and content_sha256
        and result.extraction
        and result.extraction.get("ocr_pending", 0) == 0
    ):
        await corpus_store.persist_extraction(content_sha256, result.extraction)

    return result


def _attach_incomplete_source_links(result: ChunkResult, source_url: str) -> None:
    """Expose OCR-incomplete structural pages without reprocessing the URL."""
    base_url, _fragment = urldefrag(source_url)
    for item in result.items:
        metadata = item.get("metadata") or {}
        if not metadata.get("extraction_incomplete"):
            continue
        pages = metadata.get("ocr_pending_pages") or []
        if not pages:
            try:
                pages = [int(metadata["page_start"])]
            except (KeyError, TypeError, ValueError):
                pages = []
        first_page = pages[0] if pages else None
        if not pages:
            page_label = "document"
        elif len(pages) == 1:
            page_label = str(first_page)
        elif pages == list(range(pages[0], pages[-1] + 1)):
            page_label = f"{pages[0]}-{pages[-1]}"
        else:
            page_label = f"{first_page} and {len(pages) - 1} other page(s)"
        url = f"{base_url}#page={first_page}" if first_page else base_url
        attachments = item.setdefault("attachments", [])
        if any(att.get("url") == url for att in attachments):
            continue
        attachments.append({
            "name": f"Source page {page_label} (OCR incomplete)",
            "url": url,
            "type": "source_link",
        })
