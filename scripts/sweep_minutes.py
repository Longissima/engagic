"""Backfill minutes_url for meetings the daily sync window has already left behind.

Minutes publish after the meeting -- often approved at the NEXT regular session,
2-4 weeks later -- while the daemon's resync only looks back ~14 days. This sweep
re-fetches vendor listings with a wider back-window for cities that have recent
meetings missing minutes_url, and fills ONLY that column. It still never
overwrites an existing value (UPDATE ... WHERE minutes_url IS NULL, under a row
lock) and still does not run the full sync_meeting path: re-storing old meetings
and re-tracking matters is the daemon's job inside its own window. But
minutes_url is an mv1 work-version input, so each fill publishes atomically --
lock, fill, and enqueue the meeting at its new work_version in one transaction --
rather than leaving version skew for the daemon to rediscover. Item summaries
stay frozen, so the enqueued job is cheap version bookkeeping, not an LLM re-run.

Zero LLM calls and no document downloads/parsing/corpus writes. Most adapters
discover minutes on listing/API responses; ProudCity and CivicPlus may fetch
lightweight meeting pages, and WP Events queries its media API per event.
Primary and extra-vendor streams are both rate-limited by their adapters.

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
from pipeline.utils import meeting_work_version
from vendors.factory import get_async_adapter

logger = get_logger(__name__).bind(component="sweep_minutes")

# Swept meetings are past-dated backfill: never outrank fresh meetings
# (past dates take the batch lane), mirroring resummarize_items.
BACKFILL_PRIORITY = -10


CITIES_SQL = """
    SELECT j.banana, j.vendor, j.slug, j.extra_vendors,
           count(*) FILTER (WHERE m.minutes_url IS NULL) AS missing
    FROM jurisdictions j
    JOIN meetings m ON m.banana = j.banana
    WHERE j.status = 'active'
      AND m.date >= CURRENT_TIMESTAMP - make_interval(days => $1)
      AND m.date < CURRENT_TIMESTAMP
      AND ($2::text IS NULL OR j.banana = $2)
    GROUP BY 1, 2, 3, 4
    HAVING count(*) FILTER (WHERE m.minutes_url IS NULL) > 0
    ORDER BY missing DESC
"""

FILL_SQL = """
    UPDATE meetings
    SET minutes_url = $2, updated_at = CURRENT_TIMESTAMP
    WHERE id = $1 AND minutes_url IS NULL
"""

MEETING_STATE_SQL = """
    SELECT minutes_url
    FROM meetings
    WHERE id = $1
"""


def vendor_streams(city_row) -> list[tuple[str, str]]:
    """Primary plus valid, deduplicated extra-vendor streams."""
    streams = [(city_row["vendor"], city_row["slug"])]
    for extra in city_row.get("extra_vendors") or []:
        if isinstance(extra, dict) and extra.get("vendor") and extra.get("slug"):
            streams.append((extra["vendor"], extra["slug"]))
    return list(dict.fromkeys(streams))


async def sweep_city(db, parse_date, city_row, days_back: int, dry_run: bool) -> dict:
    banana = city_row["banana"]
    counts = {"fetched": 0, "with_minutes": 0, "would_fill": 0, "filled": 0,
              "enqueued": 0, "already_set": 0,
              "id_miss": 0, "fetch_failed": 0,
              "unsupported": 0}

    for vendor, slug in vendor_streams(city_row):
        kwargs = {}
        if vendor == "legistar" and slug == "nyc":
            kwargs["api_token"] = config.NYC_LEGISTAR_TOKEN

        try:
            adapter = get_async_adapter(vendor, slug, **kwargs)
            adapter.banana = banana
            if not adapter.MINUTES_DISCOVERY_SUPPORTED:
                counts["unsupported"] += 1
                logger.info(
                    "minutes discovery unsupported",
                    banana=banana,
                    vendor=vendor,
                    slug=slug,
                )
                continue
            fetch_result = await adapter.fetch_minutes(
                days_back=days_back, days_forward=0
            )
        except Exception as e:
            logger.warning(
                "sweep fetch failed", banana=banana, vendor=vendor, slug=slug, error=str(e)
            )
            counts["fetch_failed"] += 1
            continue

        if not fetch_result.success:
            logger.warning(
                "adapter fetch unsuccessful",
                banana=banana,
                vendor=vendor,
                slug=slug,
                error=fetch_result.error,
            )
            counts["fetch_failed"] += 1
            continue

        counts["fetched"] += len(fetch_result.meetings)
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

            meeting_id = generate_meeting_id(
                banana=banana, vendor_id=str(vendor_id), date=meeting_date, title=title
            )

            if dry_run:
                # ID parity is the sweep's critical invariant. Discovery must
                # reproduce the exact vendor_id/title/start tuple used by the
                # original sync, so dry-run resolves every generated ID rather
                # than optimistically counting candidates as fills.
                async with db.pool.acquire() as conn:
                    existing = await conn.fetchrow(MEETING_STATE_SQL, meeting_id)
                if existing is None:
                    counts["id_miss"] += 1
                    logger.warning(
                        "dry-run id parity miss",
                        banana=banana,
                        vendor=vendor,
                        slug=slug,
                        meeting_id=meeting_id,
                        vendor_id=str(vendor_id),
                        title=title,
                        start=str(md.get("start")),
                    )
                elif existing["minutes_url"] is not None:
                    counts["already_set"] += 1
                else:
                    counts["would_fill"] += 1
                    logger.info(
                        "would fill",
                        banana=banana,
                        meeting_id=meeting_id,
                        url=minutes_url[:120],
                    )
                continue

            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    # Lock-first, resummarize-style: the locked read classifies
                    # id_miss/already_set authoritatively, and guarantees the
                    # fill and its queue publication commit together or not at
                    # all. Filling a row we never saw locked could commit an
                    # mv1 change with no publication.
                    meeting = await db.meetings.get_meeting(
                        meeting_id, conn=conn, lock_for_update=True
                    )
                    if meeting is None:
                        counts["id_miss"] += 1
                        continue
                    if meeting.minutes_url is not None:
                        counts["already_set"] += 1
                        continue
                    status = await conn.execute(FILL_SQL, meeting_id, minutes_url)
                    if status != "UPDATE 1":  # unreachable under the row lock
                        continue
                    counts["filled"] += 1
                    # minutes_url is an mv1 input: hash the post-fill value
                    # (the locked read predates the UPDATE) and publish the new
                    # version in this same transaction. Item summaries stay
                    # frozen, so the job is bookkeeping, not an LLM re-run.
                    items = await db.items.get_agenda_items(meeting_id, conn=conn)
                    meeting.minutes_url = minutes_url
                    work_version = meeting_work_version(meeting, items)
                    # NULL -> value changes the mv1 hash, so plain enqueue at
                    # the new version already lands runnable; exact-version
                    # reactivation is only for UNCHANGED versions (see
                    # resummarize_items).
                    if await db.queue.enqueue_job(
                        source_url=f"meeting://{meeting_id}",
                        job_type="meeting",
                        payload={"meeting_id": meeting_id},
                        meeting_id=meeting_id,
                        priority=BACKFILL_PRIORITY,
                        banana=meeting.banana,
                        work_version=work_version,
                        conn=conn,
                    ):
                        counts["enqueued"] += 1
                    else:
                        logger.warning(
                            "fill committed but queue already held this version",
                            banana=banana,
                            meeting_id=meeting_id,
                            work_version=work_version,
                        )

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
