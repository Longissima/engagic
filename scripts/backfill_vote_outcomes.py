"""Backfill vote outcomes for historical data.

Backfills missing API tallies only when one motion is uniquely identifiable.
Outcomes require an explicit source result; a majority is not stored as fact.

Usage:
    uv run python scripts/backfill_vote_outcomes.py [--dry-run] [--limit N]
"""

import argparse
import asyncio

from config import get_logger
from database.db_postgres import Database
from database.vote_utils import compute_vote_tally

logger = get_logger(__name__)


async def backfill_outcomes(dry_run: bool = False, limit: int | None = None) -> dict:
    """Backfill vote outcomes for matter_appearances.

    Returns:
        Stats dict with counts
    """
    db = await Database.create()

    try:
        async with db.pool.acquire() as conn:
            # Find matter_appearances that have votes but no outcome
            query = """
                SELECT DISTINCT ma.matter_id, ma.meeting_id, ma.item_id
                FROM matter_appearances ma
                WHERE ma.vote_tally IS NULL
                AND ma.vote_source IS DISTINCT FROM 'minutes'
                AND EXISTS (
                    SELECT 1 FROM votes v
                    WHERE v.matter_id = ma.matter_id AND v.meeting_id = ma.meeting_id
                )
            """
            if limit:
                appearances = await conn.fetch(query + " LIMIT $1", limit)
            else:
                appearances = await conn.fetch(query)

            logger.info(
                "found appearances needing backfill",
                count=len(appearances),
                dry_run=dry_run,
            )

            if not appearances:
                return {"found": 0, "updated": 0, "skipped": 0}

            updated = 0
            skipped = 0

            for row in appearances:
                matter_id = row["matter_id"]
                meeting_id = row["meeting_id"]
                item_id = row["item_id"]

                # Get votes for this matter+meeting
                votes = await conn.fetch(
                    """
                    SELECT vote, item_key, motion_index FROM votes v
                    WHERE matter_id = $1 AND meeting_id = $2 AND source='api'
                    AND (item_key=$3 OR (item_key='' AND 1=(
                        SELECT count(*) FROM items i WHERE i.matter_id=$1 AND i.meeting_id=$2
                    )))
                    """,
                    matter_id,
                    meeting_id,
                    item_id,
                )

                if not votes or len({(v['item_key'],v['motion_index']) for v in votes}) != 1:
                    skipped += 1
                    continue

                vote_list = [{"vote": v["vote"]} for v in votes]
                tally = compute_vote_tally(vote_list)

                if dry_run:
                    logger.info(
                        "would update",
                        matter_id=matter_id,
                        meeting_id=meeting_id,
                        tally=tally,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE matter_appearances
                        SET vote_tally = $1, vote_source = 'api'
                        WHERE matter_id = $2 AND meeting_id = $3 AND item_id = $4
                          AND vote_tally IS NULL AND vote_source IS DISTINCT FROM 'minutes'
                        """,
                        tally,
                        matter_id,
                        meeting_id,
                        item_id,
                    )

                updated += 1

                if updated % 100 == 0:
                    logger.info("progress", updated=updated)

            return {
                "found": len(appearances),
                "updated": updated,
                "skipped": skipped,
            }

    finally:
        await db.close()


async def main():
    parser = argparse.ArgumentParser(description="Backfill vote outcomes")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without making changes",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of records to process",
    )
    args = parser.parse_args()

    logger.info("starting backfill", dry_run=args.dry_run, limit=args.limit)

    stats = await backfill_outcomes(dry_run=args.dry_run, limit=args.limit)

    logger.info("backfill complete", **stats)

    if args.dry_run:
        print(f"\nDry run - would update {stats['updated']} appearances")
    else:
        print(f"\nBackfill complete: {stats['updated']} appearances updated")


if __name__ == "__main__":
    asyncio.run(main())
