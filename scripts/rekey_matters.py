#!/usr/bin/env python3
"""
Re-key history onto the identity contract (parsing.identifiers), without a re-sync.

The sync funnel now validates every matter_file, keeps unassigned numbers and
cited authorities out of identity, and adds a numbering period where a city
restarts its numbers. New syncs key correctly; this brings stored items in
line, including archived meetings no sync will revisit.

For every keyed item the target is recomputed from what is stored:
  - CivicClerk items, and files the extractor itself produced ("Bill 66",
    "Resolution 2026-XX"), are re-derived from title/body with today's rules;
  - any other stored file is the vendor's and only has to pass
    canonical_matter_file;
  - numbering_period adds the year from the meeting date or the printed year.
An item with no valid identity left is unlinked (it stands alone, as the
funnel would now leave it), unless votes or motions hang on its matter, in
which case it stays put and is reported.

Rows move by item (items, matter_appearances, item_motions, votes), then the
sync's own boundaries run: reconcile_meeting_appearances per meeting and
_publish_authoritative_work per affected matter set. That is what decides,
exactly as a sync would, whether a matter keeps its canonical summary (a pure
rename carries its work_version), gets matter work enqueued (a split or a new
group), or is tombstoned (emptied).

Usage:
    uv run scripts/rekey_matters.py                      # dry run, all
    uv run scripts/rekey_matters.py --banana stlouisMO   # dry run, one city
    uv run scripts/rekey_matters.py --apply [--banana X]
"""

import argparse
import asyncio
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from config import get_logger
from database.db_postgres import Database
from database.id_generation import generate_matter_id
from parsing.identifiers import (
    SIBLING_WINDOW_DAYS,
    Identifier,
    canonical_matter_file,
    detect_numbering_runs,
    extract_identifier,
    is_placeholder_token,
    numbering_period,
)
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator

logger = get_logger(__name__).bind(component="rekey_matters")

# The exact shape parsing.identifiers emits ("Bill 66", "File L24-01403"); a
# stored file of this shape was derived, so it is re-derived. A prefix test
# is not enough: Pima County's Legistar files read "File ID 16752".
DERIVED_SHAPE = re.compile(r"^(?:Contract|File|Case|Petition|Ordinance|Resolution|Bill) [A-Z0-9][A-Z0-9,.\-]*$")
BARE_SERIES = re.compile(r"^(Bill|Resolution|Ordinance) ([0-9]{1,6})$")
# CivicClerk's retired adapter format ("RES2026", "ORD26", "BB66"), re-derived
# only in CivicClerk cities: the same shape is Tacoma's real Legistar file
# ("RES41422"), and Galveston/Columbus/Canton are CivicClerk today but hold
# other-vendor files ("26-175"), which this shape never matches.
LEGACY_CIVICCLERK_SHAPE = re.compile(r"^(?:BB|RES|ORD)[0-9]+$")

BANANAS_SQL = """
    SELECT DISTINCT m.banana FROM items i JOIN meetings m ON m.id = i.meeting_id
    WHERE i.matter_id IS NOT NULL AND i.matter_file IS NOT NULL
      AND ($1::text IS NULL OR m.banana = $1)
    ORDER BY 1
"""
CANDIDATES_SQL = """
    SELECT i.id, i.title, i.body_text, i.matter_id, i.matter_file, i.matter_type,
           m.id AS meeting_id, m.date, j.vendor, cm.matter_id AS vendor_matter_id,
           (SELECT count(*) FROM items s WHERE s.matter_id = i.matter_id) AS source_items
    FROM items i
    JOIN meetings m ON m.id = i.meeting_id
    JOIN jurisdictions j ON j.banana = m.banana
    JOIN city_matters cm ON cm.id = i.matter_id
    WHERE m.banana = $1 AND i.matter_id IS NOT NULL AND i.matter_file IS NOT NULL
"""
ANCHORED_SQL = """
    SELECT DISTINCT matter_id FROM (
        SELECT matter_id FROM votes WHERE matter_id = ANY($1::text[])
        UNION SELECT matter_id FROM item_motions WHERE matter_id = ANY($1::text[])
    ) anchored
"""


@dataclass
class Move:
    item_id: str
    meeting_id: str
    source: str
    target: Optional[str]  # None = unlink
    identity: Optional[Identifier]
    year: Optional[str]
    reason: str
    vendor: str
    old_file: str = ""
    title: str = ""


@dataclass
class Group:
    target: Optional[str]
    identity: Optional[Identifier] = None
    year: Optional[str] = None
    moves: List[Move] = field(default_factory=list)

    @property
    def sources(self) -> List[str]:
        return sorted({move.source for move in self.moves})

    @property
    def meetings(self) -> List[str]:
        return sorted({move.meeting_id for move in self.moves})


def recompute_identity(row) -> Tuple[Optional[Identifier], str]:
    """Identity and the reason it differs, mirroring the sync funnel."""
    stored = row["matter_file"]
    legacy = row["vendor"] == "civicclerk" and bool(LEGACY_CIVICCLERK_SHAPE.match(stored))
    rederive = legacy or bool(DERIVED_SHAPE.match(stored))
    supplied = None if rederive else canonical_matter_file(stored)
    identity = (
        Identifier(supplied, row["matter_type"])
        if supplied
        else extract_identifier(row["title"], row["body_text"])
    )
    if identity is None:
        if not rederive:
            return None, "invalid_vendor_file"
        token = stored.split(" ", 1)[-1]
        return None, "placeholder" if is_placeholder_token(token) else "cited_or_unkeyable"
    if legacy:
        return identity, "civicclerk_adapter"
    return identity, "rederived" if identity.file != stored else "unchanged"


def bare_number(identity: Optional[Identifier]) -> Optional[Tuple[str, int]]:
    """(series, number) for an unprinted bare instrument number, else None."""
    if identity is None or identity.year:
        return None
    match = BARE_SERIES.match(identity.file)
    return (match.group(1), int(match.group(2))) if match else None


def run_high(occurrences: List[Tuple[date, int]], start: date, day: date) -> Optional[int]:
    """The run's high mark from `start` through `day`: the funnel's get_run_high, in memory."""
    numbers = [n for d, n in occurrences if start <= d <= day]
    return max(numbers) if numbers else None


def nearest_printed(printed: List[Tuple[date, str]], day: date) -> Optional[str]:
    """The funnel's get_printed_sibling_year, in memory."""
    near = [(abs((d - day).days), year) for d, year in printed if abs((d - day).days) <= SIBLING_WINDOW_DAYS]
    return min(near)[1] if near else None


async def plan_banana(conn, banana: str) -> Tuple[List[Move], Dict[str, List[date]]]:
    rows = await conn.fetch(CANDIDATES_SQL, banana)
    identities = {row["id"]: recompute_identity(row) for row in rows}

    # The city's own numbering decides whether its numbers restart.
    occurrences: Dict[str, List[Tuple[date, int]]] = defaultdict(list)
    for row in rows:
        bare = bare_number(identities[row["id"]][0])
        if bare and row["date"]:
            occurrences[bare[0]].append((row["date"].date(), bare[1]))
    runs = {series: starts for series, occ in occurrences.items() if (starts := detect_numbering_runs(occ))}
    printed: Dict[str, List[Tuple[date, str]]] = defaultdict(list)
    for row in rows:
        identity = identities[row["id"]][0]
        if identity and identity.year and row["date"]:
            printed[identity.file].append((row["date"].date(), identity.year))

    moves = []
    for row in rows:
        identity, reason = identities[row["id"]]
        year = None
        if identity and row["date"]:
            series = identity.file.partition(" ")[0]
            starts = runs.get(series, [])
            day = row["date"].date()
            if starts:
                latest = max((start for start in starts if start <= day), default=None)
                high = run_high(occurrences[series], latest, day) if latest else None
                year = numbering_period(identity, row["date"], starts, high)
            else:
                year = numbering_period(identity, row["date"],
                                        printed_sibling=nearest_printed(printed[identity.file], day))
            if year:
                reason = "period"
        target = (
            generate_matter_id(banana, matter_file=identity.file, matter_year=year)
            if identity
            else None
        )
        # A junk vendor file ("This professional services contract ...") over a
        # vendor matter id: the funnel now keys on that id. Only provable when
        # the old matter held this item alone; a shared one may be a merge of
        # several vendor ids, so those items wait for their next sync.
        if target is None and reason == "invalid_vendor_file" and row["vendor_matter_id"] and row["source_items"] == 1:
            target = generate_matter_id(banana, matter_id=row["vendor_matter_id"])
            reason = "vendor_id_fallback"
        if target == row["matter_id"]:
            continue
        moves.append(Move(row["id"], row["meeting_id"], row["matter_id"], target,
                          identity, year, reason, row["vendor"], row["matter_file"],
                          (row["title"] or "")[:160]))
    unlink_sources = sorted({m.source for m in moves if m.target is None})
    if unlink_sources:
        # Votes and motions need a matter row. An item that loses its identity
        # but carries them moves to its title-keyed matter, the same fallback
        # parse_minutes_votes uses; a generic title keeps it where it is.
        anchored = {r["matter_id"] for r in await conn.fetch(ANCHORED_SQL, unlink_sources)}
        titles = {r["id"]: r["title"] for r in rows}
        for move in moves:
            if move.target is None and move.source in anchored:
                move.target = generate_matter_id(banana, title=titles[move.item_id])
                move.reason = "title_keyed_for_votes"
                if move.target in (None, move.source):
                    move.reason = "kept_anchored_by_votes"
    return moves, runs


def group_moves(moves: List[Move]) -> Dict[Optional[str], Group]:
    groups: Dict[Optional[str], Group] = {}
    for move in moves:
        if move.reason == "kept_anchored_by_votes":
            continue
        group = groups.setdefault(move.target, Group(move.target, move.identity, move.year))
        group.moves.append(move)
    return groups


CREATE_FROM_SOURCE_SQL = """
    INSERT INTO city_matters (
        id, banana, matter_id, matter_file, matter_type, title, sponsors,
        canonical_summary, canonical_topics, attachments, metadata,
        first_seen, last_seen, appearance_count, status, matter_year
    )
    SELECT $1, banana, matter_id, $3, COALESCE($4, matter_type), title, sponsors,
           canonical_summary, canonical_topics, attachments, metadata,
           first_seen, last_seen, appearance_count, status, $5
    FROM city_matters WHERE id = $2
    ON CONFLICT (id) DO NOTHING
"""
CREATE_FROM_ITEM_SQL = """
    INSERT INTO city_matters (id, banana, matter_file, matter_type, title,
        attachments, first_seen, last_seen, matter_year)
    SELECT $1, m.banana, $2, COALESCE($3, i.matter_type), i.title, i.attachments, m.date, m.date, $5
    FROM items i JOIN meetings m ON m.id = i.meeting_id WHERE i.id = $4
    ON CONFLICT (id) DO NOTHING
"""
MOVE_ITEMS_SQL = """
    UPDATE items SET matter_id = $1, matter_file = $2, matter_type = COALESCE($3, matter_type)
    WHERE id = ANY($4::text[]) AND matter_id = ANY($5::text[])
    RETURNING id
"""
UNLINK_ITEMS_SQL = """
    UPDATE items SET matter_id = NULL, matter_file = NULL
    WHERE id = ANY($1::text[]) AND matter_id = ANY($2::text[])
    RETURNING id
"""
# Preserve appearance outcomes by moving one row per item; reconcile deletes
# the rest. An item can carry stale rows under several old matters
# (jeffersoncountyAL_27f349a3_15553 had two), and moving them all in one
# statement collides on (matter_id, meeting_id, item_id). Prefer a row with a
# vote outcome, then the row of the item's current matter.
MOVE_APPEARANCES_SQL = """
    UPDATE matter_appearances SET matter_id = $1
    WHERE id IN (
        SELECT DISTINCT ON (a.meeting_id, a.item_id) a.id
        FROM matter_appearances a JOIN items i ON i.id = a.item_id
        WHERE a.item_id = ANY($2::text[]) AND a.matter_id <> $1
          AND NOT EXISTS (SELECT 1 FROM matter_appearances e
                          WHERE e.matter_id = $1 AND e.meeting_id = a.meeting_id AND e.item_id = a.item_id)
        ORDER BY a.meeting_id, a.item_id, a.vote_outcome IS NULL, a.matter_id <> i.matter_id, a.id
    )
"""
MOVE_MOTIONS_SQL = "UPDATE item_motions SET matter_id = $1 WHERE item_id = ANY($2::text[]) AND matter_id = ANY($3::text[])"
MOVE_VOTES_SQL = """
    UPDATE votes v SET matter_id = $1
    WHERE v.matter_id = ANY($2::text[])
      AND (v.item_id = ANY($3::text[]) OR v.item_key = ANY($3::text[]))
      AND NOT EXISTS (
          SELECT 1 FROM votes e WHERE e.matter_id = $1
            AND e.council_member_id = v.council_member_id AND e.meeting_id = v.meeting_id
            AND e.item_key = v.item_key AND e.motion_index = v.motion_index AND e.source = v.source)
"""
# A whole source moving to one target is a rename: carry matter-grain rows.
CARRY_MATTER_ROWS_SQL = [
    "INSERT INTO matter_topics (matter_id, topic) SELECT $1, topic FROM matter_topics "
    "WHERE matter_id = ANY($2::text[]) ON CONFLICT DO NOTHING",
    "INSERT INTO matter_actors (matter_id, role, person_id, body_id, is_primary, actor_order, derived_at) "
    "SELECT $1, role, person_id, body_id, is_primary, actor_order, derived_at FROM matter_actors "
    "WHERE matter_id = ANY($2::text[]) ON CONFLICT DO NOTHING",
    "UPDATE deliberations SET matter_id = $1 WHERE matter_id = ANY($2::text[])",
]
DELETE_EMPTY_SQL = """
    DELETE FROM city_matters cm WHERE cm.id = ANY($1::text[])
      AND NOT EXISTS (SELECT 1 FROM items i WHERE i.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM votes v WHERE v.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM item_motions im WHERE im.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM matter_appearances a WHERE a.matter_id = cm.id)
      AND NOT EXISTS (SELECT 1 FROM deliberations d WHERE d.matter_id = cm.id)
    RETURNING cm.id
"""


async def apply_group(conn, db, orchestrator: MeetingSyncOrchestrator, group: Group) -> int:
    sources = group.sources
    item_ids = [move.item_id for move in group.moves]
    async with conn.transaction():
        # Lock hierarchy shared with sync: meetings, then matters, then items.
        meetings = await conn.fetch(
            "SELECT id, banana, date, title, committee_id FROM meetings "
            "WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE", group.meetings)
        banana = meetings[0]["banana"]
        await conn.fetch("SELECT id FROM city_matters WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE",
                         sorted(set(sources) | ({group.target} if group.target else set())))
        await db.council_members._lock_attribution_scope(banana, conn)
        owners = await conn.fetch("SELECT matter_id, array_agg(id) AS ids FROM items "
                                  "WHERE matter_id = ANY($1::text[]) GROUP BY matter_id", sources)
        whole = sorted(r["matter_id"] for r in owners if set(r["ids"]) <= set(item_ids))

        if group.target is None:
            moved = [r["id"] for r in await conn.fetch(UNLINK_ITEMS_SQL, item_ids, sources)]
            await conn.execute("DELETE FROM matter_appearances WHERE item_id = ANY($1::text[]) "
                               "AND matter_id = ANY($2::text[])", moved, sources)
        else:
            file = group.identity.file if group.identity else None
            kind = group.identity.type if group.identity else None
            if whole:
                # A rename keeps its summary: the seed carries canonical text and
                # the work_version the publish step compares against.
                await conn.execute(CREATE_FROM_SOURCE_SQL, group.target, whole[0], file, kind, group.year)
            else:
                await conn.execute(CREATE_FROM_ITEM_SQL, group.target, file, kind, item_ids[0], group.year)
            await conn.execute("UPDATE city_matters SET matter_year = COALESCE(matter_year, $2) WHERE id = $1",
                               group.target, group.year)
            await conn.execute(MOVE_APPEARANCES_SQL, group.target, item_ids)
            moved = [r["id"] for r in await conn.fetch(MOVE_ITEMS_SQL, group.target, file, kind,
                                                       item_ids, sources)]
            await conn.execute(MOVE_MOTIONS_SQL, group.target, moved, sources)
            await conn.execute(MOVE_VOTES_SQL, group.target, sources, moved)
            if whole:
                for sql in CARRY_MATTER_ROWS_SQL:
                    await conn.execute(sql, group.target, whole)
        if not moved:
            return 0

        for meeting in meetings:
            await db.matters.reconcile_meeting_appearances(
                meeting_id=meeting["id"], appeared_at=meeting["date"],
                committee=meeting["title"].split("-")[0].strip() if meeting["title"] else None,
                committee_id=meeting["committee_id"], conn=conn)
        affected = sorted(set(sources) | ({group.target} if group.target else set()))
        sponsor_members = await conn.fetch("SELECT DISTINCT council_member_id FROM sponsorships "
                                           "WHERE matter_id = ANY($1::text[])", affected)
        await db.council_members.reconcile_matter_sponsorships(
            banana=banana, affected_matter_ids=affected, conn=conn)
        await db.council_members.recompute_attribution_counts(
            {r["council_member_id"] for r in sponsor_members}, conn)
        # The sync's post-write boundary: tracking, keep/enqueue/tombstone.
        # Every affected matter is read in full regardless of meeting_id; the
        # meeting only scopes the unchanged-summary copy, which a re-key never
        # needs (an item's own summary is untouched by moving it).
        await orchestrator._publish_authoritative_work(
            meeting_id=meetings[0]["id"], affected_matter_ids=set(affected),
            procedural_matter_ids=set(), conn=conn, publish_meeting=False)
        await conn.execute("DELETE FROM matter_topics WHERE matter_id = ANY($1::text[]) "
                           "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.matter_id = matter_topics.matter_id)",
                           sources)
        await conn.execute("DELETE FROM matter_actors WHERE matter_id = ANY($1::text[]) "
                           "AND NOT EXISTS (SELECT 1 FROM items i WHERE i.matter_id = matter_actors.matter_id)",
                           sources)
        await conn.fetch(DELETE_EMPTY_SQL, sources)
    return len(moved)


def report(banana_moves: Dict[str, List[Move]]) -> None:
    moves = [m for ms in banana_moves.values() for m in ms]
    by_reason = Counter(m.reason for m in moves)
    by_vendor_reason = Counter((m.vendor, m.reason) for m in moves)
    split_sources = defaultdict(set)
    for m in moves:
        split_sources[m.source].add(m.target)
    logger.info("rekey plan", items=len(moves), source_matters=len(split_sources),
                splits=sum(1 for targets in split_sources.values() if len(targets) > 1),
                targets=len({m.target for m in moves if m.target}),
                unlinked=sum(1 for m in moves if m.target is None and m.reason != "kept_anchored_by_votes"),
                **{f"reason_{k}": v for k, v in by_reason.items()})
    for (vendor, reason), n in by_vendor_reason.most_common(25):
        logger.info("by vendor", vendor=vendor, reason=reason, items=n)
    per_city = Counter({b: len(ms) for b, ms in banana_moves.items()})
    for banana, n in per_city.most_common(15):
        logger.info("by city", banana=banana, items=n)


async def main() -> None:
    ap = argparse.ArgumentParser(description="Re-key stored items onto the identity contract")
    ap.add_argument("--apply", action="store_true", help="write changes (default is dry run)")
    ap.add_argument("--banana")
    ap.add_argument("--samples", type=int, default=0, help="log N sample moves per reason")
    ap.add_argument("--audit-csv", help="write every planned move to this CSV")
    args = ap.parse_args()

    db = await Database.create()
    orchestrator = MeetingSyncOrchestrator(db)
    try:
        async with db.pool.acquire() as conn:
            bananas = [r["banana"] for r in await conn.fetch(BANANAS_SQL, args.banana)]
            banana_moves: Dict[str, List[Move]] = {}
            banana_runs: Dict[str, Dict[str, List[date]]] = {}
            for banana in bananas:
                moves, runs = await plan_banana(conn, banana)
                banana_runs[banana] = runs
                if moves:
                    banana_moves[banana] = moves
        for banana, runs in banana_runs.items():
            for series, starts in runs.items():
                logger.info("numbering runs detected", banana=banana, series=series,
                            starts=[str(start) for start in starts])
        report(banana_moves)
        if args.audit_csv:
            with open(args.audit_csv, "w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["banana", "vendor", "reason", "item_id", "old_matter_id", "new_matter_id",
                                 "old_file", "new_file", "year", "title"])
                for banana, moves in banana_moves.items():
                    for m in moves:
                        writer.writerow([banana, m.vendor, m.reason, m.item_id, m.source, m.target or "",
                                         m.old_file,
                                         m.identity.file if m.identity else "", m.year or "", m.title])
        if args.samples:
            seen: Counter = Counter()
            for banana, moves in banana_moves.items():
                for m in moves:
                    if seen[m.reason] < args.samples:
                        seen[m.reason] += 1
                        logger.info("sample", reason=m.reason, banana=banana, item=m.item_id,
                                    to=m.identity.file if m.identity else None, year=m.year)
        if not args.apply:
            logger.info("DRY RUN - no changes made, pass --apply to write")
            return

        # Record what the data showed so the sync funnel keys new items the
        # same way; a city that stopped showing a reset loses its row.
        for banana, runs in banana_runs.items():
            if runs or args.banana:
                await db.matters.replace_numbering_runs(banana, runs)
        if not args.banana:
            await db.pool.execute("DELETE FROM numbering_runs WHERE NOT (banana = ANY($1::text[]))",
                                  [b for b, runs in banana_runs.items() if runs])
        moved_total = done = 0
        async with db.pool.acquire() as conn:
            for banana, moves in banana_moves.items():
                for group in group_moves(moves).values():
                    moved_total += await apply_group(conn, db, orchestrator, group)
                    done += 1
                    if done % 500 == 0:
                        logger.info("progress", groups_done=done, items_moved=moved_total)
                logger.info("city rekeyed", banana=banana, items=len(moves))
        logger.info("rekey complete", items_moved=moved_total, groups=done)
    finally:
        await db.pool.close()


if __name__ == "__main__":
    asyncio.run(main())
