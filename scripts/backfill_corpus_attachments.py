"""Backfill the ground-truth corpus for item attachments that predate it.

The corpus tee shipped 2026-07-02. Every item summarized before that had its
attachment text extracted and thrown away, and item summaries are frozen per
appearance, so nothing re-downloads them. Downstream readers (motioncount)
key on document_source.source_identity and see those items as "awaiting
text" forever. This script closes the gap: for every item attachment whose
URL identity has no current-version corpus text, ride the analyzer's
extraction path (download, sha256, archive original, extract, persist) with
the LLM disabled. No item summary is touched.

State lives in the corpus itself: an identity is done when document_source
points at a blob with text, and failures go through the shared
document_ingest_failure ledger with bounded retries. Restart at will.

Filtered items (procedural, ceremonial) are included by default because
readers do not distinguish them; --skip-filtered narrows to summarizable
items only.

Usage:
    uv run python scripts/backfill_corpus_attachments.py --banana paloaltoCA,sunnyvaleCA --since 2025-09-01 --dry-run
    uv run python scripts/backfill_corpus_attachments.py --banana paloaltoCA,sunnyvaleCA --since 2025-09-01 --concurrency 4
    uv run python scripts/backfill_corpus_attachments.py --state CA --since 2025-09-01 --concurrency 3
"""

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.analyzer_async import AsyncAnalyzer
from analysis.llm.input_budget import DOCUMENT_ATTACHMENT_TYPES
from config import get_logger
from corpus.store import EXTRACT_VERSION, get_corpus
from database.db_postgres import Database
from database.models import AttachmentInfo
from exceptions import DocumentDownloadError, ExtractionError
from parsing.memory_budget import MemoryAdmissionTimeout
from pipeline.url_refresh import refresh_attachment_urls
from pipeline.utils import attachment_identity
from scripts.ingest_minutes import (
    clear_failure,
    failure_attempt_limit,
    failure_error_text,
    record_failure,
)

logger = get_logger(__name__).bind(component="backfill_corpus_attachments")


# Raw-URL anti-join is a cheap pre-filter only. Signed URLs differ from their
# identity, so the authoritative check re-runs on identities in Python.
CANDIDATES_SQL = """
    SELECT i.id AS item_id, m.banana, j.vendor, j.slug, m.date::date AS meeting_date,
           i.filter_reason, a AS attachment
    FROM items i
    JOIN meetings m ON m.id = i.meeting_id
    JOIN jurisdictions j ON j.banana = m.banana
    CROSS JOIN LATERAL jsonb_array_elements(i.attachments) a
    WHERE jsonb_typeof(i.attachments) = 'array'
      AND m.date >= $1::date
      AND ($2::date IS NULL OR m.date < $2::date + 1)
      AND ($3::text[] IS NULL OR m.banana = ANY($3::text[]))
      AND ($4::boolean IS FALSE OR i.filter_reason IS NULL)
      AND ($6::text IS NULL OR j.state = $6)
      AND NOT EXISTS (
          SELECT 1 FROM (
              SELECT b.text_key, b.extract_version
              FROM document_source s JOIN document_blob b USING (content_sha256)
              WHERE s.source_identity = a->>'url'
              ORDER BY s.last_validated_at DESC NULLS LAST, s.first_seen DESC
              LIMIT 1
          ) b
          WHERE b.text_key IS NOT NULL
            AND b.extract_version = ANY($5::text[])
      )
    ORDER BY m.date DESC, i.id
"""

# Same readiness rule motioncount applies: current text at this identity.
READY_IDENTITIES_SQL = """
    WITH current_revision AS (
        SELECT DISTINCT ON (s.source_identity) s.source_identity, b.text_key, b.extract_version
        FROM document_source s JOIN document_blob b USING (content_sha256)
        WHERE s.source_identity = ANY($1::text[])
        ORDER BY s.source_identity, s.last_validated_at DESC NULLS LAST, s.first_seen DESC
    )
    SELECT source_identity FROM current_revision
    WHERE text_key IS NOT NULL AND extract_version = ANY($2::text[])
"""

FAILURE_STATE_SQL = """
    SELECT source_identity, attempt_count, permanent,
           retry_after <= CURRENT_TIMESTAMP AS retry_due
    FROM document_ingest_failure
    WHERE source_identity = ANY($1::text[])
      AND extract_version = $2
"""

# Compatible, not current: a byte-identical blob already holding version 1
# text is served to readers as-is, and record_sighting links the identity.
CORPUS_READY_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM document_blob
        WHERE content_sha256 = $1 AND text_key IS NOT NULL AND extract_version = ANY($2::text[])
    )
"""

# Extract version 1 text is still served to readers; re-extracting it would
# only churn R2. Mirrors corpus.store.COMPATIBLE_EXTRACT_VERSIONS.
COMPATIBLE_EXTRACT_VERSIONS = ["1", EXTRACT_VERSION]


MAX_REQUEUES = 5
# Legistar answers bursts with spurious 404s (55 of 56 Sunnyvale "404"s were
# 200 minutes later, 2026-09-11). One requeue separates a real dead link from
# a bad moment before the ledger sees it.
MAX_DOWNLOAD_REQUEUES = 1


class Candidate:
    __slots__ = ("identity", "attachment", "banana", "vendor", "slug", "item_ids", "meeting_date", "requeues", "download_requeues")

    def __init__(self, identity: str, attachment: AttachmentInfo, row: Any):
        self.identity = identity
        self.attachment = attachment
        self.banana = row["banana"]
        self.vendor = row["vendor"]
        self.slug = row["slug"]
        self.meeting_date = row["meeting_date"]
        self.item_ids = [row["item_id"]]
        self.requeues = 0
        self.download_requeues = 0


def parse_attachment(raw: Any) -> Optional[AttachmentInfo]:
    """Typed view of one attachments[] element, or None for non-documents."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict) or not raw.get("url"):
        return None
    try:
        attachment = AttachmentInfo.model_validate({k: v for k, v in raw.items() if k in AttachmentInfo.model_fields})
    except Exception:
        return None
    if attachment.type not in DOCUMENT_ATTACHMENT_TYPES:
        return None
    return attachment


async def fetch_chunked(conn, sql: str, values: List[str], *args, chunk: int = 5000):
    rows = []
    for start in range(0, len(values), chunk):
        rows.extend(await conn.fetch(sql, values[start:start + chunk], *args))
    return rows


async def select_candidates(db, args) -> tuple[List[Candidate], Dict[str, int]]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            CANDIDATES_SQL, args.since, args.until, args.bananas, args.skip_filtered,
            COMPATIBLE_EXTRACT_VERSIONS, args.state,
        )

    by_identity: Dict[str, Candidate] = {}
    counts = defaultdict(int)
    counts["attachment_rows"] = len(rows)
    for row in rows:
        attachment = parse_attachment(row["attachment"])
        if attachment is None:
            counts["non_document"] += 1
            continue
        identity = attachment_identity(attachment.url)
        existing = by_identity.get(identity)
        if existing:
            existing.item_ids.append(row["item_id"])
        else:
            by_identity[identity] = Candidate(identity, attachment, row)

    identities = list(by_identity)
    async with db.pool.acquire() as conn:
        ready = {r["source_identity"] for r in await fetch_chunked(
            conn, READY_IDENTITIES_SQL, identities, COMPATIBLE_EXTRACT_VERSIONS)}
        failures = {r["source_identity"]: dict(r) for r in await fetch_chunked(
            conn, FAILURE_STATE_SQL, identities, EXTRACT_VERSION)}

    todo: List[Candidate] = []
    for identity, candidate in by_identity.items():
        if identity in ready:
            counts["already_current"] += 1
            continue
        failure = failures.get(identity)
        if failure and failure["permanent"]:
            counts["permanent_failure"] += 1
            continue
        if failure and not failure["retry_due"] and not args.retry_failures:
            counts["failure_backoff"] += 1
            continue
        todo.append(candidate)
    counts["due"] = len(todo)
    return todo[: args.limit], dict(counts)


async def refresh_ephemeral_urls(todo: List[Candidate]) -> None:
    """Re-sign expiring vendor URLs (CivicClerk today) before download.

    Identity is query-stripped for signed URLs, so a refreshed URL still lands
    on the same document_source identity readers look up.
    """
    by_site: Dict[tuple, List[Candidate]] = defaultdict(list)
    for candidate in todo:
        by_site[(candidate.vendor, candidate.slug)].append(candidate)
    for (vendor, slug), group in by_site.items():
        if vendor != "civicclerk" or not slug:
            continue
        try:
            refreshed = await refresh_attachment_urls(vendor, slug, [c.attachment for c in group])
            logger.info("refreshed ephemeral urls", vendor=vendor, slug=slug, refreshed=refreshed, of=len(group))
        except (OSError, RuntimeError, asyncio.TimeoutError) as e:
            logger.warning("url refresh failed, using stored urls", vendor=vendor, slug=slug, error=str(e)[:200])


def is_memory_pressure(error: Exception) -> bool:
    """Admission timeouts mean this host was busy, not that the document is bad."""
    cause = error.__cause__
    return isinstance(cause, MemoryAdmissionTimeout) or "shared memory capacity" in str(error)


async def ingest_one(db, analyzer: AsyncAnalyzer, candidate: Candidate, args, counts: Dict[str, int]) -> bool:
    """Returns False when the candidate should go back on the queue."""
    url = candidate.attachment.url
    identity = candidate.identity
    started = time.monotonic()
    try:
        result = await analyzer.extract_document_async(url, banana=candidate.banana)
        content_sha256 = result.get("content_sha256")
        ready = False
        if content_sha256 and result.get("corpus_persisted"):
            async with db.pool.acquire() as conn:
                ready = await conn.fetchval(CORPUS_READY_SQL, content_sha256, COMPATIBLE_EXTRACT_VERSIONS)
        if not ready:
            counts["failed_persist"] += 1
            logger.warning(
                "attachment extraction not durably persisted",
                banana=candidate.banana, source_identity=identity[:110],
                content_sha256=(content_sha256 or "")[:16],
            )
            return True
        await clear_failure(db, identity)
        counts["ingested"] += 1
        logger.info(
            "attachment ingested",
            banana=candidate.banana, items=len(candidate.item_ids),
            from_corpus=bool(result.get("from_corpus")),
            chars=len(result.get("text") or ""),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except DocumentDownloadError as e:
        if candidate.download_requeues < MAX_DOWNLOAD_REQUEUES:
            candidate.download_requeues += 1
            counts["requeued_download"] += 1
            logger.info("download failed once, requeued", banana=candidate.banana, url=identity[:110], error=failure_error_text(e, url)[:120])
            return False
        counts["failed_download"] += 1
        failure = await record_failure(
            db, identity=identity, source_url=url, banana=candidate.banana, stage="download",
            error=e, max_failures=failure_attempt_limit(e, args.max_failures),
            retry_days=args.failure_retry_days,
        )
        logger.warning(
            "attachment download failed",
            banana=candidate.banana, url=identity[:110],
            attempt=(failure or {}).get("attempt_count"),
            suppressed=(failure or {}).get("permanent", False),
            error=failure_error_text(e, url)[:200],
        )
    except ExtractionError as e:
        if is_memory_pressure(e) and candidate.requeues < MAX_REQUEUES:
            candidate.requeues += 1
            counts["requeued_memory"] += 1
            logger.info("memory pressure, requeued", banana=candidate.banana, url=identity[:110], requeues=candidate.requeues)
            return False
        # Partial OCR results are persisted as partial text before this
        # raises; readers accept them, and the ledger keeps a retry pending.
        counts["failed_extract"] += 1
        failure = await record_failure(
            db, identity=identity, source_url=url, banana=candidate.banana, stage="extract",
            error=e, max_failures=args.max_failures, retry_days=args.failure_retry_days,
        )
        logger.warning(
            "attachment extraction failed",
            banana=candidate.banana, url=identity[:110],
            attempt=(failure or {}).get("attempt_count"),
            suppressed=(failure or {}).get("permanent", False),
            error=failure_error_text(e, url)[:200],
        )
    except Exception as e:
        counts["failed_other"] += 1
        logger.warning(
            "attachment ingest failed (will retry)",
            banana=candidate.banana, url=identity[:110],
            error=failure_error_text(e, url)[:200],
        )
    return True


async def run_workers(db, analyzer, todo: List[Candidate], args, counts: Dict[str, int]) -> None:
    """Fixed worker pool over a shared cursor: no task per candidate, so a
    500k-URL run holds one coroutine per worker, not one per URL."""
    cursor = 0
    done = 0
    started = time.monotonic()

    async def worker():
        nonlocal cursor, done
        while cursor < len(todo):
            index = cursor
            cursor += 1
            if not await ingest_one(db, analyzer, todo[index], args, counts):
                todo.append(todo[index])
                await asyncio.sleep(30)
                continue
            done += 1
            if done % args.progress_every == 0 or done == len(todo):
                elapsed = time.monotonic() - started
                logger.info(
                    "backfill progress", done=done, of=len(todo),
                    per_minute=round(done / elapsed * 60, 1), **counts,
                )
                print(f"{done}/{len(todo)} {round(done / elapsed * 60, 1)}/min {dict(counts)}", flush=True)

    await asyncio.gather(*(worker() for _ in range(args.concurrency)))


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", type=parse_date, required=True, help="earliest meeting date (YYYY-MM-DD)")
    parser.add_argument("--until", type=parse_date, default=None, help="latest meeting date, inclusive")
    parser.add_argument("--banana", default=None, help="comma-separated jurisdictions; default all")
    parser.add_argument("--state", default=None, help="two-letter state code; default all")
    parser.add_argument("--limit", type=int, default=1_000_000, help="max identities this run")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--max-failures", type=int, default=3)
    parser.add_argument("--failure-retry-days", type=int, default=7)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--skip-filtered", action="store_true", help="skip items with a filter_reason")
    parser.add_argument("--retry-failures", action="store_true", help="retry non-permanent ledger failures before their backoff expires")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.bananas = [b.strip() for b in args.banana.split(",") if b.strip()] if args.banana else None
    if min(args.limit, args.concurrency, args.max_failures, args.failure_retry_days, args.progress_every) < 1:
        parser.error("limit, concurrency, max failures, retry days, and progress interval must be positive")

    db = await Database.create()
    analyzer = None
    try:
        todo, candidate_counts = await select_candidates(db, args)
        by_banana = defaultdict(int)
        for candidate in todo:
            by_banana[candidate.banana] += 1
        logger.info("backfill starting", this_run=len(todo), bananas=len(by_banana), dry_run=args.dry_run, **candidate_counts)
        print(f"candidates: {candidate_counts}; this run: {len(todo)} identities across {len(by_banana)} jurisdictions")
        if args.dry_run:
            for banana, n in sorted(by_banana.items(), key=lambda kv: -kv[1])[:30]:
                print(f"  {banana:<28} {n}")
            for candidate in todo[:10]:
                print(f"  would ingest {candidate.banana:<20} {candidate.meeting_date} {candidate.identity[:90]}")
            return 0
        if not todo:
            return 0
        if get_corpus() is None:
            logger.error("corpus backfill requires an enabled, configured corpus")
            print("backfill_corpus_attachments: corpus unavailable; nothing processed")
            return 2

        await refresh_ephemeral_urls(todo)
        analyzer = AsyncAnalyzer(enable_llm=False)
        counts: Dict[str, int] = defaultdict(int)
        await run_workers(db, analyzer, todo, args, counts)
        logger.info("backfill complete", **counts)
        print(f"backfill_corpus_attachments: {dict(counts)}")
        return 0
    finally:
        if analyzer is not None:
            await analyzer.close()
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
