#!/usr/bin/env python3
"""
Backfill matter_file on historical items from identifiers cited in their own text.

The sync item funnel derives these on every pass, so anything re-synced heals
itself. Meetings that have aged out of the sync window never get another pass,
which is what this covers.

Only title and body_text are read -- the exact sources the funnel uses. Keying off
summaries would find more, but a later sync could not reproduce the value and the
items upsert would erase it, leaving rows that flip between keyed and unkeyed.

Writes matter_file and matter_type only. Run scripts/backfill_matter_ids.py
afterwards to create the city_matters rows and link items to them.

Usage:
    uv run scripts/backfill_body_identifiers.py --dry-run       # Preview (default)
    uv run scripts/backfill_body_identifiers.py --apply
    uv run scripts/backfill_body_identifiers.py --apply --banana detroitMI
"""

import argparse
import asyncio
from collections import defaultdict

from config import get_logger
from database.db_postgres import Database
from parsing.identifiers import extract_identifier

logger = get_logger(__name__)

# An identifier seen once is still a real matter, but a value that recurs across
# meetings is also self-evidence that the pattern found a durable handle rather
# than a coincidence. Reported separately so the operator can see both.
BATCH_SIZE = 500


async def find_candidates(db: Database, banana: str | None) -> list[dict]:
    """Fully unkeyed items whose own text cites a durable identifier.

    Rows with an existing ``matter_id`` may already be attached to a different
    aggregate. Relinking those is a separate, reviewed migration; this safe
    backfill only fills the pair of identity fields for genuine orphans.
    """
    query = """
        SELECT i.id, i.title, i.body_text, m.banana, m.id AS meeting_id
        FROM items i
        JOIN meetings m ON m.id = i.meeting_id
        WHERE i.matter_file IS NULL
          AND i.matter_id IS NULL
          AND (i.body_text IS NOT NULL OR i.title IS NOT NULL)
    """
    params: list = []
    if banana:
        query += " AND m.banana = $1"
        params.append(banana)

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    candidates = []
    for row in rows:
        identifier = extract_identifier(row["title"], row["body_text"])
        if not identifier:
            continue
        matter_file, matter_type = identifier
        candidates.append(
            {
                "item_id": row["id"],
                "banana": row["banana"],
                "meeting_id": row["meeting_id"],
                "matter_file": matter_file,
                "matter_type": matter_type,
            }
        )
    return candidates


async def backfill(db: Database, apply: bool, banana: str | None) -> None:
    candidates = await find_candidates(db, banana)
    if not candidates:
        logger.info("no unkeyed items carry a durable identifier")
        return

    meetings_per_matter: dict[tuple[str, str], set[str]] = defaultdict(set)
    per_city: dict[str, int] = defaultdict(int)
    per_class: dict[str, int] = defaultdict(int)
    for candidate in candidates:
        key = (candidate["banana"], candidate["matter_file"])
        meetings_per_matter[key].add(candidate["meeting_id"])
        per_city[candidate["banana"]] += 1
        per_class[candidate["matter_file"].split(" ", 1)[0]] += 1

    recurring = {k: v for k, v in meetings_per_matter.items() if len(v) > 1}
    collapsed = sum(len(v) for v in recurring.values()) - len(recurring)

    logger.info(
        "backfill candidates",
        items=len(candidates),
        distinct_matters=len(meetings_per_matter),
        recurring_matters=len(recurring),
        appearances_collapsed=collapsed,
        cities=len(per_city),
        by_class=dict(per_class),
        apply=apply,
    )
    for city, count in sorted(per_city.items(), key=lambda pair: -pair[1])[:10]:
        logger.info("city candidates", city=city, count=count)
    for candidate in candidates[:5]:
        logger.info(
            "sample",
            item_id=candidate["item_id"],
            matter_file=candidate["matter_file"],
            matter_type=candidate["matter_type"],
        )

    if not apply:
        logger.info("DRY RUN - no changes made, pass --apply to write")
        return

    updated = 0
    async with db.pool.acquire() as conn:
        for start in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[start:start + BATCH_SIZE]
            async with conn.transaction():
                # Both identity fields are re-checked in the write so a
                # concurrent sync wins rather than leaving a derived
                # matter_file attached to a pre-existing, different matter_id.
                rows = await conn.fetch(
                    """
                    UPDATE items AS item
                    SET matter_file = candidate.matter_file,
                        matter_type = COALESCE(item.matter_type, candidate.matter_type)
                    FROM unnest($1::text[], $2::text[], $3::text[])
                        AS candidate(item_id, matter_file, matter_type)
                    WHERE item.id = candidate.item_id
                      AND item.matter_file IS NULL
                      AND item.matter_id IS NULL
                    RETURNING item.id
                    """,
                    [c["item_id"] for c in batch],
                    [c["matter_file"] for c in batch],
                    [c["matter_type"] for c in batch],
                )
            updated += len(rows)
            logger.info("batch written", written=updated, total=len(candidates))

    logger.info(
        "backfill complete",
        items_updated=updated,
        next_step="uv run scripts/backfill_matter_ids.py --dry-run",
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill matter_file from identifiers cited in item text"
    )
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry run)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    parser.add_argument("--banana", help="Limit to one jurisdiction")
    args = parser.parse_args()

    db = await Database.create()
    try:
        await backfill(db, apply=args.apply and not args.dry_run, banana=args.banana)
    finally:
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
