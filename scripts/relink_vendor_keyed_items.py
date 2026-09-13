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

Per target, move only the items still matching the plan and their keyed evidence.
Whole-source relationships move only when the plan assigns every source item to
one target. Ambiguous or conflicting evidence stays on its original matter.
Sponsorships for retained items are reconciled through the repository.

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
from database.repositories_async.council_members import CouncilMemberRepository
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
    WHERE id = ANY($4::text[]) AND matter_file IS NULL AND matter_id = ANY($5::text[])
    RETURNING id
"""
MOVE_APPEARANCES_SQL = """
    UPDATE matter_appearances a SET matter_id = $1
    WHERE a.item_id = ANY($2::text[]) AND a.matter_id <> $1
      AND NOT EXISTS (SELECT 1 FROM matter_appearances existing
          WHERE existing.matter_id=$1 AND existing.meeting_id=a.meeting_id AND existing.item_id=a.item_id)
      AND NOT EXISTS (SELECT 1 FROM matter_appearances other
          WHERE other.item_id=a.item_id AND other.meeting_id=a.meeting_id
            AND other.matter_id<>a.matter_id)
"""
MOVE_MOTIONS_SQL = "UPDATE item_motions SET matter_id = $1 WHERE item_id = ANY($2::text[])"
MOVE_DELIBERATIONS_SQL = "UPDATE deliberations SET matter_id = $1 WHERE matter_id = ANY($2::text[])"
COPY_SPONSORSHIPS_SQL = """
    INSERT INTO sponsorships (council_member_id, matter_id, is_primary, sponsor_order)
    SELECT council_member_id, $1::text, bool_or(is_primary), min(sponsor_order)
    FROM sponsorships WHERE matter_id = ANY($2::text[]) GROUP BY council_member_id
    ON CONFLICT (council_member_id, matter_id) DO UPDATE SET
        is_primary = sponsorships.is_primary OR EXCLUDED.is_primary,
        sponsor_order = LEAST(sponsorships.sponsor_order, EXCLUDED.sponsor_order)
"""
MOVE_VOTES_SQL = """
    UPDATE votes v SET matter_id = $1
    WHERE v.matter_id = ANY($2::text[])
      AND (v.item_key = ANY($3::text[])
           OR (v.item_key = '' AND v.matter_id = ANY($4::text[])))
      AND NOT EXISTS (
          SELECT 1 FROM votes existing WHERE existing.matter_id = $1
            AND existing.council_member_id = v.council_member_id
            AND existing.meeting_id = v.meeting_id AND existing.item_key = v.item_key
            AND existing.motion_index = v.motion_index AND existing.source = v.source
      )
      AND NOT EXISTS (
          SELECT 1 FROM votes other WHERE other.id <> v.id AND other.matter_id = ANY($2::text[])
            AND (other.item_key = ANY($3::text[])
                 OR (other.item_key = '' AND other.matter_id = ANY($4::text[])))
            AND other.council_member_id=v.council_member_id AND other.meeting_id=v.meeting_id
            AND other.item_key=v.item_key AND other.motion_index=v.motion_index AND other.source=v.source
      )
"""
CREATE_FROM_ITEM_SQL = """
    INSERT INTO city_matters (id, banana, matter_file, matter_type, title,
        canonical_summary, canonical_topics, attachments, first_seen, last_seen)
    SELECT $1, m.banana, $2, COALESCE($3, i.matter_type), i.title,
           i.summary, i.topics, i.attachments, m.date, m.date
    FROM items i JOIN meetings m ON m.id = i.meeting_id WHERE i.id = $4
    ON CONFLICT (id) DO NOTHING
"""
COPY_TOPICS_SQL = """
    INSERT INTO matter_topics (matter_id, topic)
    SELECT $1::text, topic FROM matter_topics WHERE matter_id = ANY($2::text[])
    ON CONFLICT DO NOTHING
"""
# Do not cascade away ambiguous unkeyed history or unresolved collisions.
DELETE_EMPTY_SQL = """
    DELETE FROM city_matters cm WHERE cm.id = ANY($1::text[])
      AND NOT EXISTS (SELECT 1 FROM items i WHERE i.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM votes v WHERE v.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM item_motions im WHERE im.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM matter_appearances a WHERE a.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM sponsorships s WHERE s.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM deliberations d WHERE d.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM matter_topics t WHERE t.matter_id = cm.id)
    RETURNING cm.id
"""
RECOUNT_SQL = """
    UPDATE city_matters cm SET appearance_count = (
        SELECT count(DISTINCT meeting_id) FROM items WHERE matter_id = cm.id
    ), first_seen = COALESCE((SELECT min(m.date) FROM items i JOIN meetings m ON m.id=i.meeting_id
                              WHERE i.matter_id=cm.id), first_seen),
       last_seen = COALESCE((SELECT max(m.date) FROM items i JOIN meetings m ON m.id=i.meeting_id
                             WHERE i.matter_id=cm.id), last_seen)
    WHERE cm.id = ANY($1::text[])
"""


@dataclass
class Target:
    matter_file: str
    matter_type: Optional[str] = None
    items: List[str] = field(default_factory=list)
    sources: Set[str] = field(default_factory=set)
    meetings: Set[str] = field(default_factory=set)
    whole_sources: Set[str] = field(default_factory=set)


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
    sources = sorted({source for group in groups.values() for source in group.sources})
    async with db.pool.acquire() as conn:
        ownership = await conn.fetch("SELECT matter_id, array_agg(id) AS ids FROM items "
                                     "WHERE matter_id = ANY($1::text[]) GROUP BY matter_id", sources)
    source_items = {r['matter_id']: set(r['ids']) for r in ownership}
    for group in groups.values():
        group.whole_sources = {source for source in group.sources
                               if source_items.get(source) and source_items[source] <= set(group.items)}
    return len(rows), groups


async def apply_group(conn, target: str, group: Target, members: CouncilMemberRepository) -> int:
    sources = sorted(group.sources)
    async with conn.transaction():
        # Use the same meeting locks as sync and minutes publication.
        await conn.fetch("SELECT id FROM meetings WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE",
                         sorted(group.meetings))
        seeds = await conn.fetch(SEED_SQL, sources)
        if not seeds:
            return 0
        await conn.fetch("SELECT id FROM city_matters WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE",
                         sorted(sources + [target]))
        await members._lock_attribution_scope(seeds[0]['banana'], conn)
        eligible = await conn.fetch("""
            SELECT i.id, i.matter_id FROM items i JOIN meetings m ON m.id=i.meeting_id
            WHERE i.id = ANY($1::text[]) AND i.matter_id = ANY($2::text[]) AND i.matter_file IS NULL
            ORDER BY m.date DESC NULLS LAST, i.id FOR UPDATE OF i
        """, group.items, sources)
        if not eligible:
            return 0
        eligible_ids = [r['id'] for r in eligible]
        # Only a whole-source move can reuse an aggregate source summary.
        owners = await conn.fetch("SELECT matter_id, array_agg(id) AS ids FROM items "
                                  "WHERE matter_id=ANY($1::text[]) GROUP BY matter_id", sources)
        whole = sorted(r['matter_id'] for r in owners if r['matter_id'] in group.whole_sources
                       and set(r['ids']) <= set(eligible_ids))
        seed = next((r for r in seeds if r['id'] in whole), None)
        if seed:
            await conn.execute(CREATE_SQL, target, seed['id'], group.matter_file, group.matter_type)
        else:
            await conn.execute(CREATE_FROM_ITEM_SQL, target, group.matter_file, group.matter_type, eligible_ids[0])
        moved = await conn.fetch(MOVE_ITEMS_SQL, target, group.matter_file, group.matter_type, eligible_ids, sources)
        moved_ids = [r['id'] for r in moved]
        await conn.execute(MOVE_MOTIONS_SQL, target, moved_ids)
        await conn.execute(MOVE_APPEARANCES_SQL, target, moved_ids)
        await conn.execute(MOVE_VOTES_SQL, target, sources, moved_ids, whole)
        await conn.execute(MOVE_DELIBERATIONS_SQL, target, whole)
        await conn.execute(COPY_TOPICS_SQL, target, whole)
        await conn.execute("""INSERT INTO matter_topics(matter_id, topic)
            SELECT $1, topic FROM item_topics WHERE item_id = ANY($2::text[])
            ON CONFLICT DO NOTHING""", target, moved_ids)
        # Reconcile aggregates only where items still establish their identity.
        retained = await conn.fetch("SELECT DISTINCT matter_id FROM items WHERE matter_id=ANY($1::text[])", sources)
        await members.reconcile_matter_sponsorships(banana=seeds[0]['banana'],
            affected_matter_ids=[target] + [r['matter_id'] for r in retained], conn=conn)
        # Preserve legacy sponsorships on an unambiguously moved whole source.
        await conn.execute(COPY_SPONSORSHIPS_SQL, target, whole)
        affected = await conn.fetch("SELECT DISTINCT council_member_id FROM sponsorships WHERE matter_id=ANY($1::text[])", whole + [target])
        await conn.execute("DELETE FROM sponsorships WHERE matter_id=ANY($1::text[])", whole)
        await conn.execute("DELETE FROM matter_topics WHERE matter_id=ANY($1::text[])", whole)
        await members.recompute_attribution_counts({r['council_member_id'] for r in affected}, conn)
        await conn.execute(RECOUNT_SQL, sources + [target])
        await conn.fetch(DELETE_EMPTY_SQL, whole)
    return len(moved_ids)


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
                moved_total += await apply_group(conn, target, g, db.council_members)
                if n % 500 == 0:
                    logger.info("progress", groups_done=n, items_moved=moved_total)
        logger.info("relink complete", items_moved=moved_total, target_matters=len(groups))
    finally:
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
