"""Backfill minutes_url for meetings the daily sync window has already left behind.

Minutes publish after the meeting -- often approved at the NEXT regular session,
2-4 weeks later -- while the daemon's resync only looks back ~14 days. This sweep
re-fetches vendor listings with a wider back-window for cities that have recent
meetings missing minutes_url, and fills ONLY that column. It deliberately does not
run the full sync_meeting path: re-storing old meetings and re-tracking matters is
the daemon's job inside its own window; the sweep is surgical by design and can
never overwrite anything (UPDATE ... WHERE minutes_url IS NULL).

Zero LLM calls. Listing fetches only -- one adapter fetch per city, rate-limited
by the adapters' own limiters.

Usage:
    uv run python scripts/sweep_minutes.py --dry-run
    uv run python scripts/sweep_minutes.py --days-back 60 --concurrency 4
    uv run python scripts/sweep_minutes.py --banana appletonWI
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config, get_logger
from database.db_postgres import Database
from database.id_generation import generate_meeting_id
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from vendors.factory import get_async_adapter

logger = get_logger(__name__).bind(component="sweep_minutes")


CITIES_SQL = """
    SELECT j.banana, j.vendor, j.slug,
           count(*) FILTER (WHERE m.minutes_url IS NULL) AS missing
    FROM jurisdictions j
    JOIN meetings m ON m.banana = j.banana
    WHERE j.status = 'active'
      AND m.date >= CURRENT_TIMESTAMP - make_interval(days => $1)
      AND m.date < CURRENT_TIMESTAMP
      AND ($2::text IS NULL OR j.banana = $2)
    GROUP BY 1, 2, 3
    HAVING count(*) FILTER (WHERE m.minutes_url IS NULL) > 0
    ORDER BY missing DESC
"""

FILL_SQL = """
    UPDATE meetings
    SET minutes_url = $2, updated_at = CURRENT_TIMESTAMP
    WHERE id = $1 AND minutes_url IS NULL
"""


async def sweep_city(db, parse_date, city_row, days_back: int, dry_run: bool) -> dict:
    banana, vendor, slug = city_row["banana"], city_row["vendor"], city_row["slug"]
    counts = {"fetched": 0, "with_minutes": 0, "filled": 0, "already_set": 0,
              "id_miss": 0, "undated_skipped": 0, "fetch_failed": 0}

    kwargs = {}
    if vendor == "legistar" and slug == "nyc":
        kwargs["api_token"] = config.NYC_LEGISTAR_TOKEN

    try:
        adapter = get_async_adapter(vendor, slug, **kwargs)
        adapter.banana = banana
        fetch_result = await adapter.fetch_meetings(days_back=days_back, days_forward=0)
    except Exception as e:
        logger.warning("sweep fetch failed", banana=banana, vendor=vendor, error=str(e))
        counts["fetch_failed"] = 1
        return counts

    if not fetch_result.success:
        logger.warning("adapter fetch unsuccessful", banana=banana, error=fetch_result.error)
        counts["fetch_failed"] = 1
        return counts

    counts["fetched"] = len(fetch_result.meetings)
    for md in fetch_result.meetings:
        minutes_url = md.get("minutes_url")
        if not minutes_url:
            continue
        counts["with_minutes"] += 1

        vendor_id = md.get("vendor_id")
        title = md.get("title") or "Meeting"
        meeting_date = parse_date(md)
        if not vendor_id:
            continue
        if meeting_date is None:
            # sync_meeting ids undated meetings with datetime.now() at store
            # time -- underivable here, so the sweep cannot re-match them.
            counts["undated_skipped"] += 1
            continue

        meeting_id = generate_meeting_id(
            banana=banana, vendor_id=str(vendor_id), date=meeting_date, title=title
        )

        if dry_run:
            counts["filled"] += 1
            logger.info("would fill", banana=banana, meeting_id=meeting_id, url=minutes_url[:120])
            continue

        async with db.pool.acquire() as conn:
            status = await conn.execute(FILL_SQL, meeting_id, minutes_url)
            if status == "UPDATE 1":
                counts["filled"] += 1
            else:
                already = await conn.fetchval(
                    "SELECT minutes_url IS NOT NULL FROM meetings WHERE id = $1", meeting_id
                )
                if already is None:
                    counts["id_miss"] += 1
                elif already:
                    counts["already_set"] += 1

    return counts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=60,
                        help="listing back-window per city (default 60)")
    parser.add_argument("--banana", default=None, help="restrict to one city")
    parser.add_argument("--limit-cities", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = await Database.create()
    try:
        # Reuse the orchestrator's date parser: meeting-id equality with the
        # sync path depends on parsing dates the exact same way.
        parse_date = MeetingSyncOrchestrator(db)._parse_meeting_date

        async with db.pool.acquire() as conn:
            cities = await conn.fetch(CITIES_SQL, args.days_back, args.banana)
        if args.limit_cities:
            cities = cities[: args.limit_cities]

        logger.info("sweep starting", cities=len(cities), days_back=args.days_back,
                    dry_run=args.dry_run)

        sem = asyncio.Semaphore(args.concurrency)

        async def run_one(row):
            async with sem:
                return await sweep_city(db, parse_date, row, args.days_back, args.dry_run)

        results = await asyncio.gather(*(run_one(r) for r in cities))

        totals: dict = {}
        for c in results:
            for k, v in c.items():
                totals[k] = totals.get(k, 0) + v
        logger.info("sweep complete", **totals)
        print(f"sweep_minutes: {totals}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
