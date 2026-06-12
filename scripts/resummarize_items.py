#!/usr/bin/env python3
"""
Re-summarize items produced by an older prompts version.

The batch lane makes historical reprocessing affordable (50% token cost,
separate quota pool from streaming). This script unfreezes matching item
summaries — setting summary NULL lifts the freeze-on-summary invariant —
and re-enqueues their meetings. Past-dated meetings land in the batch lane
automatically; only the urgent window streams.

prompts_version semantics: NULL = summarized before provenance existed
(pre-2026-06), the oldest cohort. Versions compare lexicographically,
which is correct for the single-digit 'v2'/'v3' scheme.

Caveat: if a re-sync runs before the queue drains, the sync-time
attachments-unchanged copy can refill a NULLed row from a prior appearance
(carrying that appearance's old prompts_version). Such rows stay targeted
by future runs of this script and are replaced when the meeting job lands.

Usage:
    uv run python scripts/resummarize_items.py --below v3 --dry-run
    uv run python scripts/resummarize_items.py --below v3 --banana gainesvilleFL --yes
    uv run python scripts/resummarize_items.py --below v3 --limit 200 --yes
"""

import argparse
import asyncio

from config import get_logger
from database.db_postgres import Database

logger = get_logger(__name__)

# Re-enqueued backfill work must never outrank fresh meetings.
BACKFILL_PRIORITY = -10


async def find_targets(db: Database, below: str, banana: str | None, limit: int | None):
    """Meetings (newest first) holding summaries from before `below`."""
    conditions = [
        "i.summary IS NOT NULL",
        "(i.prompts_version IS NULL OR i.prompts_version < $1)",
    ]
    params: list = [below]
    if banana:
        params.append(banana)
        conditions.append(f"m.banana = ${len(params)}")

    sql = f"""
        SELECT m.id AS meeting_id, m.banana, m.date,
               array_agg(i.id) AS item_ids
        FROM items i
        JOIN meetings m ON m.id = i.meeting_id
        WHERE {" AND ".join(conditions)}
        GROUP BY m.id, m.banana, m.date
        ORDER BY m.date DESC NULLS LAST
    """
    if limit:
        params.append(limit)
        sql += f" LIMIT ${len(params)}"

    async with db.pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def apply(db: Database, targets: list) -> tuple[int, int]:
    unfrozen = 0
    for t in targets:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE items
                    SET summary = NULL, prompts_version = NULL
                    WHERE id = ANY($1::text[])
                    """,
                    t["item_ids"],
                )
                unfrozen += int(result.split()[-1])
        # processing_metadata=None keeps the stored chunk/html audit (COALESCE)
        await db.queue.enqueue_job(
            source_url=f"meeting://{t['meeting_id']}",
            job_type="meeting",
            payload={"meeting_id": t["meeting_id"]},
            meeting_id=t["meeting_id"],
            priority=BACKFILL_PRIORITY,
            banana=t["banana"],
        )
    return unfrozen, len(targets)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-summarize items produced by an older prompts version"
    )
    parser.add_argument("--below", required=True, help="target items with prompts_version < this (NULL always matches)")
    parser.add_argument("--banana", help="restrict to one city")
    parser.add_argument("--limit", type=int, help="max meetings to re-enqueue (newest first)")
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument("--yes", action="store_true", help="apply without prompting")
    args = parser.parse_args()

    db = await Database.create()
    try:
        targets = await find_targets(db, args.below, args.banana, args.limit)
        item_count = sum(len(t["item_ids"]) for t in targets)
        print(f"{item_count} items across {len(targets)} meetings below {args.below!r}"
              + (f" in {args.banana}" if args.banana else ""))
        for t in targets[:10]:
            print(f"  {t['banana']:<30} {str(t['date'])[:10]:<12} {len(t['item_ids'])} items")
        if len(targets) > 10:
            print(f"  ... and {len(targets) - 10} more meetings")

        if args.dry_run or not targets:
            return
        if not args.yes:
            print("\nre-run with --yes to unfreeze these summaries and re-enqueue")
            return

        unfrozen, enqueued = await apply(db, targets)
        print(f"\nunfroze {unfrozen} item summaries, re-enqueued {enqueued} meetings "
              f"at priority {BACKFILL_PRIORITY} (past dates take the batch lane)")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
