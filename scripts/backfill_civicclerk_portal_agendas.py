"""Purge and re-acquire CivicClerk agenda originals archived as the portal shell.

`{slug}.portal.civicclerk.com/event/N/files/agenda/{fileId}` is a viewer route,
not a document route. Fetched directly it returns 1,289 bytes of HTML whose only
text is "You need to enable JavaScript to run this app." PyMuPDF opens that as a
valid 1-page document rather than failing, so it was archived as the meeting's
original and nothing reported an error. 2,314 identities across 206
jurisdictions, over three portal builds (the shell's sha changes when CivicClerk
redeploys, which is why grouping by sha makes it look like a closed window).

pipeline.utils.canonical_fetch_url now rewrites that route to the API's
GetMeetingFileStream at the acquisition boundary, so fetches are correct going
forward. This script repairs what was already stored:

  1. purge    -- drop the document_source rows that map an agenda identity to a
                 shell blob. Those rows are the only thing that makes the shell
                 resolvable; without them the identity simply has no archive.
  2. backfill -- re-acquire each meeting's agenda through the analyzer, which
                 now fetches the API URL and archives under that identity.
  3. verify   -- confirm the API identity resolves to a PDF blob carrying text.

The shell blobs themselves are left in place. They are shared, content-addressed
rows referenced by nothing else once the mappings are gone, and deleting them
would orphan R2 objects for no gain -- consistent with the rule that a wrong
source is de-preferred by a read, never erased.

Usage:
    uv run python scripts/backfill_civicclerk_portal_agendas.py --dry-run
    uv run python scripts/backfill_civicclerk_portal_agendas.py --limit 25
    uv run python scripts/backfill_civicclerk_portal_agendas.py --concurrency 3
"""

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.analyzer_async import AsyncAnalyzer
from config import get_logger
from database.db_postgres import Database
from exceptions import DocumentDownloadError, ExtractionError
from parsing.memory_budget import MemoryAdmissionTimeout
from pipeline.utils import canonical_fetch_url
from scripts.ingest_minutes import (
    clear_failure,
    failure_attempt_limit,
    failure_error_text,
    record_failure,
)

logger = get_logger(__name__).bind(component="backfill_cc_portal_agendas")

PORTAL_AGENDA_LIKE = "%portal.civicclerk.com/event/%/files/agenda/%"

# One row per poisoned identity. A portal URL can front several meetings only
# if the same fileId is reused, so DISTINCT keeps the unit of work the fetch,
# not the meeting.
CANDIDATES_SQL = """
    SELECT DISTINCT ds.source_identity AS url, ds.banana
    FROM document_source ds
    JOIN document_blob b USING (content_sha256)
    WHERE ds.source_identity ILIKE $1
      AND b.content_type LIKE 'text/html%'
    ORDER BY ds.banana, ds.source_identity
"""

# Scoped to the identities this run will actually re-acquire, so a --limit run
# never strips an archive it is not going to replace.
PURGE_SQL = """
    DELETE FROM document_source ds
    USING document_blob b
    WHERE ds.content_sha256 = b.content_sha256
      AND ds.source_identity = ANY($1::text[])
      AND b.content_type LIKE 'text/html%'
"""

# Repaired means: the API identity this portal URL resolves to now points at a
# PDF blob with extracted text. Checking the blob (not just the fetch) is what
# distinguishes a real repair from a download that never persisted.
VERIFY_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM document_source ds
        JOIN document_blob b USING (content_sha256)
        WHERE ds.source_identity = $1
          AND b.content_type = 'application/pdf'
          AND b.text_key IS NOT NULL
    )
"""

MAX_REQUEUES = 5
MAX_DOWNLOAD_REQUEUES = 1


class Candidate:
    __slots__ = ("url", "api_url", "banana", "requeues", "download_requeues")

    def __init__(self, url: str, banana: str):
        self.url = url
        self.api_url = canonical_fetch_url(url)
        self.banana = banana
        self.requeues = 0
        self.download_requeues = 0


def is_memory_pressure(error: Exception) -> bool:
    """Admission timeouts mean this host was busy, not that the document is bad."""
    return isinstance(error.__cause__, MemoryAdmissionTimeout) or (
        "shared memory capacity" in str(error)
    )


async def ingest_one(db, analyzer: AsyncAnalyzer, candidate: Candidate, args, counts) -> bool:
    """Returns False when the candidate should go back on the queue."""
    started = time.monotonic()
    try:
        # Deliberately passes the PORTAL url: the rewrite under test lives at
        # the acquisition boundary, so this exercises the production path
        # rather than a shortcut around it.
        result = await analyzer.extract_document_async(candidate.url, banana=candidate.banana)
        async with db.pool.acquire() as conn:
            repaired = await conn.fetchval(VERIFY_SQL, candidate.api_url)
        if not repaired:
            counts["failed_persist"] += 1
            logger.warning(
                "agenda re-acquired but no pdf text durably persisted",
                banana=candidate.banana, api_url=candidate.api_url[:110],
                document_format=result.get("document_format"),
            )
            return True
        await clear_failure(db, candidate.url)
        counts["repaired"] += 1
        logger.info(
            "agenda original repaired",
            banana=candidate.banana,
            chars=len(result.get("text") or ""),
            pages=result.get("page_count"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except DocumentDownloadError as e:
        if candidate.download_requeues < MAX_DOWNLOAD_REQUEUES:
            candidate.download_requeues += 1
            counts["requeued_download"] += 1
            return False
        counts["failed_download"] += 1
        await record_failure(
            db, identity=candidate.url, source_url=candidate.api_url, banana=candidate.banana,
            stage="download", error=e, max_failures=failure_attempt_limit(e, args.max_failures),
            retry_days=args.failure_retry_days,
        )
        logger.warning(
            "agenda download failed", banana=candidate.banana,
            api_url=candidate.api_url[:110], error=failure_error_text(e, candidate.url)[:200],
        )
    except ExtractionError as e:
        if is_memory_pressure(e) and candidate.requeues < MAX_REQUEUES:
            candidate.requeues += 1
            counts["requeued_memory"] += 1
            return False
        counts["failed_extract"] += 1
        await record_failure(
            db, identity=candidate.url, source_url=candidate.api_url, banana=candidate.banana,
            stage="extract", error=e, max_failures=args.max_failures,
            retry_days=args.failure_retry_days,
        )
        logger.warning(
            "agenda extraction failed", banana=candidate.banana,
            api_url=candidate.api_url[:110], error=failure_error_text(e, candidate.url)[:200],
        )
    except Exception as e:
        counts["failed_other"] += 1
        logger.warning(
            "agenda repair failed (will retry)", banana=candidate.banana,
            api_url=candidate.api_url[:110], error=failure_error_text(e, candidate.url)[:200],
        )
    return True


async def run_workers(db, analyzer, todo: List[Candidate], args, counts) -> None:
    """Fixed worker pool over a shared cursor, mirroring the attachment backfill."""
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
                print(f"{done}/{len(todo)} {round(done / elapsed * 60, 1)}/min {dict(counts)}", flush=True)

    await asyncio.gather(*(worker() for _ in range(args.concurrency)))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=1_000_000, help="max identities this run")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--max-failures", type=int, default=3)
    parser.add_argument("--failure-retry-days", type=int, default=7)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--banana", default=None, help="comma-separated jurisdictions; default all")
    parser.add_argument("--skip-purge", action="store_true",
                        help="re-acquire only; leave the shell mappings in place")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = await Database.create()
    counts: Dict[str, int] = defaultdict(int)
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(CANDIDATES_SQL, PORTAL_AGENDA_LIKE)

        wanted = {b.strip() for b in args.banana.split(",")} if args.banana else None
        todo = [
            Candidate(r["url"], r["banana"])
            for r in rows
            if wanted is None or r["banana"] in wanted
        ][: args.limit]
        jurisdictions = len({c.banana for c in todo})
        print(f"{len(todo)} poisoned agenda identities across {jurisdictions} jurisdictions")

        if args.dry_run:
            for candidate in todo[:10]:
                print(f"  {candidate.banana:22} {candidate.url}")
                print(f"  {'':22} -> {candidate.api_url}")
            print(f"  ... would purge {len(todo)} document_source rows, then re-acquire each")
            return 0

        if not args.skip_purge:
            async with db.pool.acquire() as conn:
                status = await conn.execute(PURGE_SQL, [c.url for c in todo])
            print(f"purged shell mappings: {status}")

        analyzer = AsyncAnalyzer(enable_llm=False)
        try:
            await run_workers(db, analyzer, todo, args, counts)
        finally:
            await analyzer.close()

        async with db.pool.acquire() as conn:
            left = await conn.fetchval(
                "SELECT count(*) FROM document_source ds JOIN document_blob b USING (content_sha256)"
                " WHERE ds.source_identity ILIKE $1 AND b.content_type LIKE 'text/html%'",
                PORTAL_AGENDA_LIKE,
            )
        print(f"\ndone: {dict(counts)}")
        print(f"shell mappings remaining: {left}")
        return 0 if not counts["failed_persist"] else 1
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
