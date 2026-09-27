"""Store archived meetings as ordinary meetings, items and attachments.

Replay manifests whose meeting IDs are absent from the database. This covers
older archive runs, --no-store-meetings runs, and archive runs where meeting
storage failed. The archive runner now stores meetings by default.

Each manifest holds the adapter's meeting dict for the ordinary sync_meeting
path. Replay reads the manifest from corpus storage without re-fetching vendor
documents. Only fields captured in the manifest can be restored; PDF text,
sponsors, votes and other omitted enrichment require later processing.

Shared no-op deciders suppress summary enqueueing by default. Pass
--enqueue-summaries to enable the ordinary processing deciders.

Usage:
    uv run python scripts/backfill_meetings_from_manifests.py --vendor legistar --dry-run
    uv run python scripts/backfill_meetings_from_manifests.py --vendor legistar --concurrency 4
"""

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_logger
from corpus.store import get_corpus
from database.db_postgres import Database
from pipeline.orchestrators.enqueue_decider import (
    SuppressedEnqueueDecider,
    SuppressedMatterEnqueueDecider,
)
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator

logger = get_logger(__name__).bind(component="backfill_meetings_from_manifests")

MANIFEST_PREFIX = "engagic://meeting-archive/"

# The meeting_id sits in the manifest identity because archive_meeting minted
# it with the same generate_meeting_id ordinary sync uses. Anti-joining on it
# skips meetings a normal sync already covered.
CANDIDATES_SQL = """
SELECT s.source_identity, s.banana, b.original_key
FROM document_source s
JOIN document_blob b ON b.content_sha256 = s.content_sha256
WHERE s.source_identity LIKE $1
  AND b.original_key IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM meetings m
      WHERE m.id = split_part(s.source_identity, '/', 6)
  )
-- Round-robin across jurisdictions. Ordering by banana would put every
-- concurrent worker in the same city, serializing them on that city's matter
-- aggregate locks; interleaving keeps them on disjoint rows.
ORDER BY row_number() OVER (PARTITION BY s.banana ORDER BY s.source_identity), s.banana
"""


async def load_manifest(corpus, original_key: str, identity: str, counts) -> Optional[Dict[str, Any]]:
    try:
        payload = await corpus.r2.get(original_key)
    except Exception as exc:
        counts["manifest_fetch_failed"] += 1
        logger.warning("manifest fetch failed", identity=identity[:120],
                       error=str(exc), error_type=type(exc).__name__)
        return None
    if payload is None:
        counts["manifest_missing"] += 1
        return None
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        counts["manifest_unparseable"] += 1
        logger.warning("manifest unparseable", identity=identity[:120], error=str(exc))
        return None


async def store_one(sync, corpus, jurisdictions, candidate, counts, lock) -> None:
    identity = candidate["source_identity"]
    manifest = await load_manifest(corpus, candidate["original_key"], identity, counts)
    if manifest is None:
        return

    meeting_dict = manifest.get("meeting")
    banana = manifest.get("banana") or candidate["banana"]
    if not meeting_dict or not meeting_dict.get("vendor_id"):
        counts["manifest_incomplete"] += 1
        return

    city = jurisdictions.get(banana)
    if city is None:
        counts["jurisdiction_missing"] += 1
        return

    started = time.monotonic()
    try:
        meeting, stats = await sync.sync_meeting(meeting_dict, city)
    except Exception as exc:
        counts["store_failed"] += 1
        logger.warning("meeting store failed", banana=banana, identity=identity[:120],
                       error=str(exc), error_type=type(exc).__name__)
        return

    async with lock:
        if meeting is None:
            counts["skipped"] += 1
            counts[f"skip_{stats.get('skip_reason') or 'unknown'}"] += 1
            return
        counts["meetings_stored"] += 1
        counts["items_stored"] += stats.get("items_stored", 0)
        counts["matters_tracked"] += stats.get("matters_tracked", 0)
        counts["seconds"] += round(time.monotonic() - started, 3)


async def run(sync, corpus, jurisdictions, candidates, args, counts) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    for candidate in candidates:
        queue.put_nowait(candidate)
    lock = asyncio.Lock()
    started = time.monotonic()

    async def worker() -> None:
        while True:
            try:
                candidate = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await store_one(sync, corpus, jurisdictions, candidate, counts, lock)
            finally:
                queue.task_done()
            done = counts["meetings_stored"] + counts["skipped"]
            if done and done % args.progress_every == 0:
                elapsed = max(time.monotonic() - started, 1e-6)
                logger.info("backfill progress", meetings=counts["meetings_stored"],
                            items=counts["items_stored"], skipped=counts["skipped"],
                            per_minute=round(done / elapsed * 60, 1))

    await asyncio.gather(*(worker() for _ in range(args.concurrency)))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vendor", default=None, help="restrict to one vendor; default all")
    parser.add_argument("--banana", default=None, help="comma-separated jurisdictions")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="stop after this many; zero means all")
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--enqueue-summaries", action="store_true",
                        help="let the ordinary deciders run; off by default so a "
                             "backfill never buys summaries")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if min(args.concurrency, args.progress_every) < 1:
        parser.error("concurrency and progress interval must be positive")

    db = await Database.create()
    try:
        corpus = get_corpus()
        if corpus is None:
            logger.error("backfill requires an enabled, configured corpus")
            print("backfill_meetings_from_manifests: corpus unavailable")
            return 2

        pattern = MANIFEST_PREFIX + (f"{args.vendor}/%" if args.vendor else "%")
        async with db.pool.acquire() as conn:
            candidates = await conn.fetch(CANDIDATES_SQL, pattern)
        if args.banana:
            wanted = {b.strip() for b in args.banana.split(",") if b.strip()}
            candidates = [c for c in candidates if c["banana"] in wanted]
        if args.limit:
            candidates = candidates[: args.limit]

        by_banana: Dict[str, int] = defaultdict(int)
        for candidate in candidates:
            by_banana[candidate["banana"]] += 1
        logger.info("backfill starting", manifests=len(candidates), bananas=len(by_banana),
                    vendor=args.vendor or "all", enqueue=args.enqueue_summaries,
                    dry_run=args.dry_run)
        print(f"meetings to store: {len(candidates)} across {len(by_banana)} jurisdictions"
              f" (summaries {'ENABLED' if args.enqueue_summaries else 'suppressed'})")
        if args.dry_run or not candidates:
            for banana, n in sorted(by_banana.items(), key=lambda kv: -kv[1])[:25]:
                print(f"  {banana:<28} {n}")
            return 0

        jurisdictions = {}
        for banana in by_banana:
            city = await db.jurisdictions.get_city(banana)
            if city is not None:
                jurisdictions[banana] = city
        missing = len(by_banana) - len(jurisdictions)
        if missing:
            logger.warning("jurisdictions not found", count=missing)

        sync = MeetingSyncOrchestrator(db)
        if not args.enqueue_summaries:
            sync.enqueue_decider = SuppressedEnqueueDecider()
            sync.matter_enqueue_decider = SuppressedMatterEnqueueDecider()

        counts: Dict[str, int] = defaultdict(int)
        await run(sync, corpus, jurisdictions, candidates, args, counts)
        logger.info("backfill complete", **counts)
        print(f"backfill_meetings_from_manifests: {dict(counts)}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
