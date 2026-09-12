#!/usr/bin/env python3
"""
Re-key items that carry a vendor matter_id but no matter_file onto the
durable identifier cited in their own text.

scripts/backfill_body_identifiers.py deliberately skips these: an item that is
already attached to a matter needs its matter moved, not just a column
filled. PrimeGov is the bulk of it (a fresh matter GUID per agenda, so 26k
items sit in one-appearance matters that never link across meetings).

For each affected item the target matter id is generate_matter_id(banana,
matter_file=derived); matter_file wins over matter_id there, which is exactly
what the sync funnel now does on every pass, so this only brings history in
line with what the next sync would do anyway.

Per target matter, in one transaction:
  1. create the city_matters row if absent, seeded from the newest source
     matter (title, canonical summary, topics, attachments, metadata) so no
     summary is lost and nothing is re-summarized
  2. move items, matter_appearances, deliberations
  3. copy sponsorships, votes, matter_topics with ON CONFLICT DO NOTHING
  4. delete source matters that no longer own any item (cascades clean the
     leftover child rows)

Usage:
    uv run scripts/relink_vendor_keyed_items.py            # dry run
    uv run scripts/relink_vendor_keyed_items.py --apply
    uv run scripts/relink_vendor_keyed_items.py --apply --banana louisvilleKY
"""

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from config import get_logger
from database.db_postgres import Database
from database.id_generation import generate_matter_id
from parsing.identifiers import extract_identifier

logger = get_logger(__name__).bind(component="relink_vendor_keyed")

CANDIDATES_SQL = """
    SELECT i.id, i.title, i.body_text, i.matter_id, i.matter_type, m.banana, m.id AS meeting_id
    FROM items i
    JOIN meetings m ON m.id = i.meeting_id
    WHERE i.matter_id IS NOT NULL
      AND i.matter_file IS NULL
      AND ($1::text IS NULL OR m.banana = $1)
"""

SEED_SQL = """
    SELECT id, banana, matter_id, matter_type, title, sponsors, canonical_summary,
           canonical_topics, attachments, metadata, first_seen, last_seen,
           appearance_count, status
    FROM city_matters
    WHERE id = ANY($1::text[])
    ORDER BY last_seen DESC NULLS LAST, canonical_summary IS NULL
"""

CREATE_SQL = """
    INSERT INTO city_matters (
        id, banana, matter_id, matter_file, matter_type, title, sponsors,
        canonical_summary, canonical_topics, attachments, metadata,
        first_seen, last_seen, appearance_count, status
    )
    SELECT $1, banana, matter_id, $3, COALESCE($4, matter_type), title, sponsors,
           canonical_summary, canonical_topics, attachments, metadata,
           first_seen, last_seen, appearance_count, status
    FROM city_matters WHERE id = $2
    ON CONFLICT (id) DO UPDATE SET
        canonical_summary = COALESCE(city_matters.canonical_summary, EXCLUDED.canonical_summary),
        canonical_topics = COALESCE(city_matters.canonical_topics, EXCLUDED.canonical_topics),
        first_seen = LEAST(city_matters.first_seen, EXCLUDED.first_seen),
        last_seen = GREATEST(city_matters.last_seen, EXCLUDED.last_seen),
        updated_at = CURRENT_TIMESTAMP
"""

MOVE_ITEMS_SQL = """
    UPDATE items SET matter_id = $1, matter_file = $2,
           matter_type = COALESCE(matter_type, $3)
    WHERE id = ANY($4::text[]) AND matter_file IS NULL
    RETURNING id
"""
MOVE_APPEARANCES_SQL = """
    UPDATE matter_appearances SET matter_id = $1
    WHERE item_id = ANY($2::text[]) AND matter_id <> $1
"""
MOVE_MOTIONS_SQL = "UPDATE item_motions SET matter_id = $1 WHERE item_id = ANY($2::text[])"
MOVE_DELIBERATIONS_SQL = "UPDATE deliberations SET matter_id = $1 WHERE matter_id = ANY($2::text[])"
COPY_SPONSORSHIPS_SQL = """
    INSERT INTO sponsorships (council_member_id, matter_id, is_primary, sponsor_order)
    SELECT council_member_id, $1::text, is_primary, sponsor_order
    FROM sponsorships WHERE matter_id = ANY($2::text[])
    ON CONFLICT (council_member_id, matter_id) DO NOTHING
"""
COPY_VOTES_SQL = """
    INSERT INTO votes (council_member_id, matter_id, meeting_id, vote, vote_date, sequence, metadata,
                       item_id, item_key, motion_index, motion_text, source, content_sha256, receipt, parse_run_id, observation_ordinal)
    SELECT council_member_id, $1::text, meeting_id, vote, vote_date, sequence, metadata,
           item_id, item_key, motion_index, motion_text, source, content_sha256, receipt, parse_run_id, observation_ordinal
    FROM votes WHERE matter_id = ANY($2::text[])
    ON CONFLICT (council_member_id, matter_id, meeting_id, item_key, motion_index, source) DO NOTHING
"""
COPY_TOPICS_SQL = """
    INSERT INTO matter_topics (matter_id, topic)
    SELECT $1::text, topic FROM matter_topics WHERE matter_id = ANY($2::text[])
    ON CONFLICT DO NOTHING
"""
# Every live FK onto city_matters is ON DELETE SET NULL (the schema file says
# CASCADE) and the child columns are NOT NULL, so an emptied parent cannot be
# deleted until its copied child rows are gone.
DELETE_EMPTY_CHILDREN_SQL = [
    f"""
    DELETE FROM {table} c
    WHERE c.matter_id = ANY($1::text[])
      AND NOT EXISTS (SELECT 1 FROM items i WHERE i.matter_id = c.matter_id)
    """
    for table in ("matter_topics", "sponsorships", "votes", "matter_appearances", "deliberations")
]
DELETE_EMPTY_SQL = """
    DELETE FROM city_matters cm
    WHERE cm.id = ANY($1::text[])
      AND NOT EXISTS (SELECT 1 FROM items i WHERE i.matter_id = cm.id)
    RETURNING cm.id
"""
RECOUNT_SQL = """
    UPDATE city_matters cm SET appearance_count = sub.n
    FROM (SELECT matter_id, count(DISTINCT meeting_id) n FROM items WHERE matter_id = $1 GROUP BY 1) sub
    WHERE cm.id = sub.matter_id
"""


@dataclass
class Target:
    matter_file: str
    matter_type: Optional[str] = None
    items: List[str] = field(default_factory=list)
    sources: Set[str] = field(default_factory=set)
    meetings: Set[str] = field(default_factory=set)


async def plan(db, banana) -> Tuple[int, Dict[Tuple[str, str], Target]]:
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(CANDIDATES_SQL, banana)
    groups: Dict[Tuple[str, str], Target] = {}
    for row in rows:
        derived = extract_identifier(row["title"], row["body_text"])
        if not derived:
            continue
        matter_file, matter_type = derived
        target = generate_matter_id(banana=row["banana"], matter_file=matter_file)
        if not target or target == row["matter_id"]:
            continue
        group = groups.setdefault((row["banana"], target), Target(matter_file=matter_file))
        group.matter_type = group.matter_type or matter_type or row["matter_type"]
        group.items.append(row["id"])
        group.sources.add(row["matter_id"])
        group.meetings.add(row["meeting_id"])
    return len(rows), groups


async def apply_group(conn, target: str, group: Target) -> int:
    sources = sorted(group.sources)
    async with conn.transaction():
        seeds = await conn.fetch(SEED_SQL, sources)
        if not seeds:
            return 0
        await conn.execute(CREATE_SQL, target, seeds[0]["id"], group.matter_file, group.matter_type)
        moved = await conn.fetch(MOVE_ITEMS_SQL, target, group.matter_file, group.matter_type, group.items)
        await conn.execute(MOVE_MOTIONS_SQL, target, group.items)
        await conn.execute(MOVE_APPEARANCES_SQL, target, group.items)
        await conn.execute(MOVE_DELIBERATIONS_SQL, target, sources)
        await conn.execute(COPY_SPONSORSHIPS_SQL, target, sources)
        await conn.execute(COPY_VOTES_SQL, target, sources)
        await conn.execute(COPY_TOPICS_SQL, target, sources)
        for statement in DELETE_EMPTY_CHILDREN_SQL:
            await conn.execute(statement, sources)
        await conn.fetch(DELETE_EMPTY_SQL, sources)
        await conn.execute(RECOUNT_SQL, target)
    return len(moved)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Re-key vendor-keyed items onto cited identifiers")
    ap.add_argument("--apply", action="store_true", help="write changes (default is dry run)")
    ap.add_argument("--banana")
    args = ap.parse_args()

    db = await Database.create()
    try:
        scanned, groups = await plan(db, args.banana)
        merges = sum(1 for g in groups.values() if len(g.sources) > 1)
        cross_meeting = sum(1 for g in groups.values() if len(g.meetings) > 1)
        per_city: Dict[str, int] = defaultdict(int)
        for (banana, _), g in groups.items():
            per_city[banana] += len(g.items)
        logger.info(
            "relink plan",
            scanned=scanned,
            items=sum(len(g.items) for g in groups.values()),
            target_matters=len(groups),
            source_matters=len({s for g in groups.values() for s in g.sources}),
            merges=merges,
            cross_meeting_targets=cross_meeting,
            apply=args.apply,
        )
        for city, n in sorted(per_city.items(), key=lambda kv: -kv[1])[:10]:
            logger.info("city", banana=city, items=n)
        for (_, target), g in list(groups.items())[:5]:
            logger.info("sample", target=target, matter_file=g.matter_file, items=len(g.items), sources=len(g.sources))
        if not args.apply:
            logger.info("DRY RUN - no changes made, pass --apply to write")
            return

        moved_total = 0
        async with db.pool.acquire() as conn:
            for n, ((_, target), g) in enumerate(groups.items(), 1):
                moved_total += await apply_group(conn, target, g)
                if n % 500 == 0:
                    logger.info("progress", groups_done=n, items_moved=moved_total)
        logger.info("relink complete", items_moved=moved_total, target_matters=len(groups))
    finally:
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
