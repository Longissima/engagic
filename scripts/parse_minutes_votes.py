#!/usr/bin/env python3
"""
Turn ingested minutes into per-member votes and appearance outcomes.

This is the minutes route: for the cities whose vendor exposes no votes API,
the minutes document IS the record. Two parsers, both deterministic and both
behind a publish gate that abstains on any inconsistency:

  driver   per-city template (parsing.rollcall.spike) for Legistar-generated
           minutes with a file number beside every motion
  engine   parsing.rollcall.engine for everyone else: align the minutes to
           the meeting's own items, read attendance, publish what the clerk
           recorded at the attribution the document supports

Per-member attribution lands in `votes` (source='minutes', item_id,
motion_text, byte-offset receipt into the corpus text). Outcome and tally
land on matter_appearances for every published item, including tally-only
ones where no member can be named. Roster members named by the minutes are
created in council_members on first publish (source in metadata).

Every motion on an item is stored, in document order, at its own
motion_index (migration 043 removed the constraint that allowed only one).
A single item can carry an amendment that failed and an adoption that
passed, and collapsing them to the disposition threw away the dissent.
Meetings that already carry API votes are left alone.

Usage:
    uv run scripts/parse_minutes_votes.py --banana denverCO         # dry run
    uv run scripts/parse_minutes_votes.py --apply --days-back 180
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import get_logger
from corpus.store import close_corpus, get_corpus, init_corpus
from database.db_postgres import Database
from database.id_generation import generate_matter_id
from database.vote_utils import compute_vote_tally, determine_vote_outcome
from parsing.rollcall import DIALECTS, load_spike_parser, norm_file
from parsing.rollcall.engine import parse_meeting

logger = get_logger(__name__).bind(component="parse_minutes_votes")

CANON_TO_DB = {
    "AYE": "yes",
    "NO": "no",
    "ABSTAIN": "abstain",
    "EXCUSED": "absent",
    "ABSENT": "absent",
    "RECUSED": "recused",
    "PRESENT": "present",
    "NONVOTING": "not_voting",
}
OUTCOME_TO_DB = {"PASS": "passed", "FAIL": "failed"}

MEETINGS_SQL = """
    SELECT DISTINCT ON (md.meeting_id)
           md.meeting_id, md.content_sha256, m.banana, m.date,
           EXISTS (SELECT 1 FROM votes v WHERE v.meeting_id = md.meeting_id AND v.source = 'api') AS has_api_votes
    FROM minutes_documents md
    JOIN meetings m ON m.id = md.meeting_id
    WHERE ($1::text IS NULL OR m.banana = $1)
      AND ($2::int IS NULL OR m.date >= now() - make_interval(days => $2))
    ORDER BY md.meeting_id, md.ingested_at DESC
"""
ITEMS_SQL = """
    SELECT i.id, i.sequence, i.agenda_number, i.title, i.matter_id,
           COALESCE(i.matter_file, cm.matter_file) AS matter_file
    FROM items i
    LEFT JOIN city_matters cm ON cm.id = i.matter_id
    WHERE i.meeting_id = $1
"""
ROSTER_SQL = "SELECT id, name FROM council_members WHERE banana = $1"
UPSERT_VOTE_SQL = """
    INSERT INTO votes (council_member_id, matter_id, meeting_id, vote, vote_date, sequence, metadata,
                       item_id, motion_index, motion_text, source, content_sha256, receipt)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $12, $9, 'minutes', $10, $11)
    ON CONFLICT (council_member_id, matter_id, meeting_id, motion_index) DO UPDATE SET
        vote = EXCLUDED.vote, sequence = EXCLUDED.sequence, item_id = EXCLUDED.item_id,
        metadata = EXCLUDED.metadata, motion_text = EXCLUDED.motion_text,
        content_sha256 = EXCLUDED.content_sha256, receipt = EXCLUDED.receipt
    WHERE votes.source = 'minutes'
"""
# An item the council voted on is a matter even when no vendor or text gave
# it a file number; key it by normalized title (the PrimeGov-era fallback in
# generate_matter_id) so the vote has a row to hang on. Generic titles
# ("Approval of Minutes") return no id and stay unkeyed.
CREATE_TITLE_MATTER_SQL = """
    INSERT INTO city_matters (id, banana, title, first_seen, last_seen, appearance_count, status)
    VALUES ($1, $2, $3, $4, $4, 1, 'active')
    ON CONFLICT (id) DO UPDATE SET
        last_seen = GREATEST(city_matters.last_seen, EXCLUDED.last_seen),
        updated_at = CURRENT_TIMESTAMP
"""
LINK_ITEM_SQL = "UPDATE items SET matter_id = $1 WHERE id = $2 AND matter_id IS NULL"

UPSERT_APPEARANCE_SQL = """
    INSERT INTO matter_appearances (matter_id, meeting_id, item_id, appeared_at, vote_outcome, vote_tally)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (matter_id, meeting_id, item_id) DO UPDATE SET
        vote_outcome = EXCLUDED.vote_outcome, vote_tally = EXCLUDED.vote_tally
"""


@dataclass
class Publishable:
    motion_index: int
    item_id: str
    matter_id: Optional[str]
    method: str
    votes: List[Tuple[str, str]]
    outcome: Optional[str]
    tally: Dict[str, int]
    motion_text: str
    receipt: Dict[str, Any]


def locate(text: str, motion_text: str, sha: str, hint: int = -1) -> Dict[str, Any]:
    """Byte-offset receipt for the motion sentence, whitespace-insensitive."""
    if hint >= 0:
        return {"sha256": sha, "start": hint, "end": hint + len(motion_text)}
    probe = re.sub(r"\s+", " ", motion_text)[:80].strip()
    if probe:
        pattern = re.compile(r"\s+".join(re.escape(w) for w in probe.split(" ")))
        match = pattern.search(text)
        if match:
            return {"sha256": sha, "start": match.start(), "end": match.end()}
    return {"sha256": sha, "start": -1, "end": -1}


def via_driver(parse, dialect: str, text: str, items, roster: Dict[str, str], sha: str, counts: Counter, reasons: Counter) -> Dict[Tuple[str, int], Publishable]:
    by_file: Dict[str, Any] = {}
    for item in items:
        key = norm_file(dialect, item["matter_file"])
        if key and item["matter_id"]:
            by_file.setdefault(key, item)
    gazetteer = parse.Gazetteer(sorted(roster))
    out: Dict[Tuple[str, int], Publishable] = {}
    motion_counts: Counter = Counter()
    for passage in parse.PARSERS[dialect](text):
        if not passage.sections:
            continue
        counts["passages"] += 1
        item = by_file.get(norm_file(dialect, passage.matter_file) or "")
        if item is None:
            counts["unaligned"] += 1
            continue
        decision = passage.evaluate_publish_gate(gazetteer)
        if not decision.publishable:
            counts["abstained"] += 1
            for reason in decision.reasons:
                reasons[reason.split(":")[0]] += 1
            continue
        motion_index = motion_counts[item["id"]]
        motion_counts[item["id"]] += 1
        tally = compute_vote_tally([{"vote": CANON_TO_DB.get(v, "present")} for _, v in decision.votes])
        out[(item["id"], motion_index)] = Publishable(
            motion_index=motion_index,
            item_id=item["id"], matter_id=item["matter_id"], method="driver",
            votes=decision.votes, outcome=OUTCOME_TO_DB.get(passage.outcome or ""), tally=tally,
            motion_text=passage.motion_text, receipt=locate(text, passage.motion_text, sha),
        )
    return out


def via_engine(text: str, items, roster: Dict[str, str], sha: str, counts: Counter, reasons: Counter) -> Tuple[Dict[Tuple[str, int], Publishable], List[str]]:
    parsed = parse_meeting(text, [dict(i) for i in items], list(roster))
    counts["items_anchored"] += parsed.items_anchored
    counts["items_total"] += parsed.items_total
    counts["passages"] += parsed.evidence_seen
    counts["abstained"] += len(parsed.abstained)
    for ab in parsed.abstained:
        for reason in ab.reasons:
            reasons[reason.split(":")[0]] += 1
    out: Dict[Tuple[str, int], Publishable] = {}
    for iv in parsed.published:
        counts[f"method_{iv.method}"] += 1
        tally = dict(iv.tally) if iv.tally else compute_vote_tally(
            [{"vote": CANON_TO_DB.get(v, "present")} for _, v in iv.member_votes]
        )
        out[(iv.item["id"], iv.motion_index)] = Publishable(
            motion_index=iv.motion_index,
            item_id=iv.item["id"], matter_id=iv.item["matter_id"], method=iv.method,
            votes=iv.member_votes, outcome=OUTCOME_TO_DB.get(iv.outcome or ""), tally=tally,
            motion_text=iv.motion_text, receipt=locate(text, iv.motion_text, sha, hint=iv.offset),
        )
    return out, parsed.attendance.present + parsed.attendance.absent


async def ensure_members(db, banana: str, names: List[str], roster: Dict[str, str]) -> None:
    """Create roster rows for members the minutes named; the minutes are the roster source here."""
    for name in names:
        if name in roster:
            continue
        member = await db.council_members.find_or_create_member(banana, name)
        if member is not None:
            roster[name] = member.id
            await db.council_members.update_member_metadata(member.id, metadata={"source": "minutes"})


async def main() -> int:
    ap = argparse.ArgumentParser(description="Write per-member votes and outcomes from ingested minutes")
    ap.add_argument("--banana")
    ap.add_argument("--days-back", type=int, default=None)
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    args = ap.parse_args()

    parse = load_spike_parser()
    db = await Database.create()
    init_corpus(db.document_blobs)
    corpus = get_corpus()
    if corpus is None:
        logger.error("corpus unavailable")
        return 2
    counts: Counter = Counter()
    reasons: Counter = Counter()
    per_city: Counter = Counter()
    try:
        async with db.pool.acquire() as conn:
            meetings = await conn.fetch(MEETINGS_SQL, args.banana, args.days_back)
        for row in meetings:
            if row["has_api_votes"]:
                counts["skipped_api_meeting"] += 1
                continue
            result = await corpus.lookup_extraction(row["content_sha256"])
            text = (result or {}).get("text") or ""
            if not text:
                counts["no_text"] += 1
                continue
            async with db.pool.acquire() as conn:
                roster = {r["name"]: r["id"] for r in await conn.fetch(ROSTER_SQL, row["banana"])}
                items = await conn.fetch(ITEMS_SQL, row["meeting_id"])
            counts["meetings"] += 1
            sha = row["content_sha256"]
            dialect = DIALECTS.get(row["banana"])
            if dialect:
                published = via_driver(parse, dialect, text, items, roster, sha, counts, reasons)
                new_names: List[str] = []
            else:
                published, new_names = via_engine(text, items, roster, sha, counts, reasons)
            titles = {i["id"]: i["title"] for i in items}
            to_create: List[Tuple[str, str, str]] = []
            for pub in published.values():
                if pub.matter_id:
                    continue
                matter_id = generate_matter_id(row["banana"], title=titles.get(pub.item_id) or "")
                if matter_id:
                    pub.matter_id = matter_id
                    if pub.motion_index == 0:
                        to_create.append((matter_id, pub.item_id, titles[pub.item_id]))
                        counts["title_keyed_matters"] += 1
                else:
                    counts["generic_title_unkeyed"] += 1
            published = {k: v for k, v in published.items() if v.matter_id}
            last_motion: Dict[str, int] = {}
            for pub in published.values():
                last_motion[pub.item_id] = max(last_motion.get(pub.item_id, 0), pub.motion_index)
            counts["motions_published"] += len(published)
            counts["publishable_items"] += len(last_motion)
            counts["extra_motions"] += len(published) - len(last_motion)
            counts["vote_rows"] += sum(len(p.votes) for p in published.values())
            per_city[row["banana"]] += len(published)
            if not args.apply or not published:
                continue
            needed = sorted({name for p in published.values() for name, _ in p.votes} | set(new_names))
            await ensure_members(db, row["banana"], needed, roster)
            async with db.pool.acquire() as conn:
                async with conn.transaction():
                    for matter_id, item_id, title in to_create:
                        await conn.execute(CREATE_TITLE_MATTER_SQL, matter_id, row["banana"], title, row["date"])
                        await conn.execute(LINK_ITEM_SQL, matter_id, item_id)
                    for pub in published.values():
                        for seq, (member, canon) in enumerate(pub.votes, 1):
                            member_id = roster.get(member)
                            if member_id is None:
                                counts["member_unmapped"] += 1
                                continue
                            await conn.execute(
                                UPSERT_VOTE_SQL, member_id, pub.matter_id, row["meeting_id"],
                                CANON_TO_DB.get(canon, "present"), row["date"], seq,
                                {"method": pub.method}, pub.item_id, pub.motion_text, sha,
                                pub.receipt, pub.motion_index,
                            )
                        # matter_appearances holds one disposition per
                        # appearance, so the last motion on the item wins
                        # there while every motion survives in votes.
                        if pub.motion_index != last_motion.get(pub.item_id):
                            continue
                        outcome = pub.outcome or determine_vote_outcome(pub.tally)
                        await conn.execute(
                            UPSERT_APPEARANCE_SQL, pub.matter_id, row["meeting_id"], pub.item_id,
                            row["date"], outcome, {**pub.tally, "method": pub.method},
                        )
            counts["meetings_written"] += 1
        logger.info("minutes votes", apply=args.apply, **counts)
        print(json.dumps({
            "counts": dict(counts),
            "abstained": dict(reasons),
            "top_cities": per_city.most_common(15),
        }, indent=1))
        return 0
    finally:
        await close_corpus()
        await db.pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
