"""Pull minutes documents into the ground-truth corpus (R2 originals + text).

Rides the analyzer's existing extraction path -- download, sha256, archive
original, extract text, persist -- so minutes bytes enter the same
content-addressed corpus as agendas and packets, deduped by both content hash
and URL identity. Zero LLM calls: the AsyncAnalyzer's summarizer is constructed
as a side effect of its ctor but never invoked.

Already-seen URLs are skipped via document_source.source_identity, which
archive_original records even when extraction later fails -- a viewer-page
URL that isn't a document (some vendors expose only those) is downloaded once,
archived, counted as failed_extract, and never fetched again.

This is the substrate step for the roll-call track (spygov docs/MODEL_DOCTRINE.md):
the parser reads corpus text, never live vendor URLs.

Usage:
    uv run python scripts/ingest_minutes.py --dry-run
    uv run python scripts/ingest_minutes.py --days-back 120 --limit 500
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.analyzer_async import AsyncAnalyzer
from config import get_logger
from database.db_postgres import Database
from exceptions import ExtractionError
from pipeline.utils import attachment_identity

logger = get_logger(__name__).bind(component="ingest_minutes")


CANDIDATES_SQL = """
    SELECT id, banana, minutes_url
    FROM meetings
    WHERE minutes_url IS NOT NULL
      AND date >= CURRENT_TIMESTAMP - make_interval(days => $1)
      AND ($2::text IS NULL OR banana = $2)
    ORDER BY date DESC
"""

SEEN_SQL = """
    SELECT DISTINCT source_identity
    FROM document_source
    WHERE source_identity = ANY($1::text[])
"""


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=120,
                        help="meeting-date window to consider (default 120)")
    parser.add_argument("--limit", type=int, default=500,
                        help="max NEW documents to ingest this run (default 500)")
    parser.add_argument("--banana", default=None, help="restrict to one city")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = await Database.create()
    analyzer = AsyncAnalyzer()
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(CANDIDATES_SQL, args.days_back, args.banana)

        identities = {r["id"]: attachment_identity(r["minutes_url"]) for r in rows}
        async with db.pool.acquire() as conn:
            seen_rows = await conn.fetch(SEEN_SQL, list(set(identities.values())))
        seen = {r["source_identity"] for r in seen_rows}

        new_rows = [r for r in rows if identities[r["id"]] not in seen]
        todo = new_rows[: args.limit]
        logger.info("ingest starting", candidates=len(rows),
                    already_in_corpus=len(rows) - len(new_rows),
                    new=len(new_rows), this_run=len(todo), dry_run=args.dry_run)

        if args.dry_run:
            for r in todo[:20]:
                print(f"would ingest  {r['banana']:<20} {r['minutes_url'][:110]}")
            print(f"ingest_minutes (dry-run): {len(new_rows)} new in window, "
                  f"{len(todo)} this run at --limit {args.limit}")
            return 0

        sem = asyncio.Semaphore(args.concurrency)
        counts = {"ingested": 0, "failed_extract": 0, "failed_download": 0}

        async def ingest_one(row):
            async with sem:
                try:
                    result = await analyzer.extract_pdf_async(
                        row["minutes_url"], banana=row["banana"]
                    )
                    counts["ingested"] += 1
                    logger.info("minutes ingested", banana=row["banana"],
                                meeting_id=row["id"], chars=len(result.get("text") or ""))
                except ExtractionError as e:
                    # Bytes are archived pre-extraction and the URL identity is
                    # recorded, so this document will not be re-fetched.
                    counts["failed_extract"] += 1
                    logger.warning("minutes extract failed (archived, won't retry)",
                                   banana=row["banana"], url=row["minutes_url"][:110],
                                   error=str(e)[:200])
                except Exception as e:
                    counts["failed_download"] += 1
                    logger.warning("minutes download failed (will retry next run)",
                                   banana=row["banana"], url=row["minutes_url"][:110],
                                   error=str(e)[:200])

        await asyncio.gather(*(ingest_one(r) for r in todo))

        logger.info("ingest complete", **counts)
        print(f"ingest_minutes: {counts}")
        return 0
    finally:
        await analyzer.close()
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
