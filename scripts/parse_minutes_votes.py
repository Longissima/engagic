#!/usr/bin/env python3
"""Observe existing minutes, retain evidence, and publish confirmed motion facts.

Format drivers and generic evidence use shared claim-level validation. Internal
runs preserve exact text and unresolved observations. Public minutes projection
is atomically replaceable; API evidence stays independent. No vendor refetches.
See docs/MOTION_DATA_CONTRACT.md for lineage, replay and migration details.

    python -m scripts.parse_minutes_votes --banana denverCO  # dry run
    python -m scripts.parse_minutes_votes --apply --concurrency 4
"""

import argparse
import asyncio
import asyncpg
import json
import hashlib
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import get_logger
from corpus.store import close_corpus, get_corpus, init_corpus
from database.db_postgres import Database
from database.id_generation import generate_matter_id
from database.repositories_async.council_members import CouncilMemberRepository
from database.repositories_async.minutes import MinutesRepository, digest, run_key, compare_api
from parsing.rollcall import DIALECTS
from parsing.rollcall.observations import parser_build
from parsing.rollcall.identity import MemberIdentities, reconcile_members
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
           b.extract_version, b.text_key, b.text_extracted_at, b.extract_method
    FROM minutes_documents md
    JOIN meetings m ON m.id = md.meeting_id
    JOIN document_blob b USING(content_sha256)
    WHERE ($1::text IS NULL OR m.banana = $1)
      AND ($2::int IS NULL OR m.date >= now() - make_interval(days => $2))
    ORDER BY md.meeting_id, md.ingested_at DESC, md.content_sha256 DESC
"""
ITEMS_SQL = """
    SELECT i.id, i.sequence, i.agenda_number, i.title, i.matter_id,
           COALESCE(i.matter_file, cm.matter_file) AS matter_file
    FROM items i
    LEFT JOIN city_matters cm ON cm.id = i.matter_id
    WHERE i.meeting_id = $1
"""
ROSTER_SQL = """SELECT cm.id, cm.name,
    EXISTS(SELECT 1 FROM votes v WHERE v.council_member_id=cm.id AND v.source='api') AS has_api_votes
    FROM council_members cm WHERE cm.banana=$1"""
UPSERT_VOTE_SQL = """
    INSERT INTO votes (council_member_id, matter_id, meeting_id, vote, vote_date, sequence, metadata,
                       item_id, motion_index, motion_text, source, content_sha256, receipt, parse_run_id, observation_ordinal)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $12, $9, 'minutes', $10, $11, $13, $14)
    ON CONFLICT (council_member_id, matter_id, meeting_id, item_key, motion_index, source) DO UPDATE SET
        vote = EXCLUDED.vote, vote_date = EXCLUDED.vote_date, sequence = EXCLUDED.sequence, item_id = EXCLUDED.item_id,
        metadata = EXCLUDED.metadata, motion_text = EXCLUDED.motion_text,
        content_sha256 = EXCLUDED.content_sha256, receipt = EXCLUDED.receipt,
        parse_run_id=EXCLUDED.parse_run_id, observation_ordinal=EXCLUDED.observation_ordinal
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
    INSERT INTO matter_appearances (matter_id, meeting_id, item_id, appeared_at, vote_outcome, vote_tally, vote_source)
    VALUES ($1, $2, $3, $4, $5, $6, 'minutes')
    ON CONFLICT (matter_id, meeting_id, item_id) DO UPDATE SET
        vote_outcome = EXCLUDED.vote_outcome, vote_tally = EXCLUDED.vote_tally,
        vote_source = 'minutes'
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
    observation_index: Optional[int] = None
    tally_basis: Optional[str] = None
    reported_body: Optional[str] = None


def locate(text: str, motion_text: str, sha: str, hint: int = -1) -> Dict[str, Any]:
    """Half-open Unicode character offsets into the hash-pinned corpus text."""
    if hint >= 0:
        return {"sha256": sha, "unit": "unicode_codepoint", "start": hint, "end": hint + len(motion_text)}
    probe = re.sub(r"\s+", " ", motion_text)[:80].strip()
    if probe:
        pattern = re.compile(r"\s+".join(re.escape(w) for w in probe.split(" ")))
        match = pattern.search(text)
        if match:
            return {"sha256": sha, "unit": "unicode_codepoint", "start": match.start(), "end": match.end()}
    return {"sha256": sha, "unit": "unicode_codepoint", "start": -1, "end": -1}


async def ensure_members(db, banana: str, names: List[str], roster: Dict[str, str]) -> None:
    """Create roster rows for members the minutes named; the minutes are the roster source here."""
    for name in names:
        if name in roster:
            continue
        try:
            member = await db.council_members.find_or_create_member(banana, name)
        except asyncpg.UniqueViolationError:
            # Another meeting in the same city may establish the same person.
            member = await db.council_members.find_or_create_member(banana, name)
        if member is not None:
            roster[name] = member.id
            await db.council_members.update_member_metadata(member.id, metadata={"source": "minutes"})


UPSERT_MOTION_SQL = """
    INSERT INTO item_motions (item_id, motion_index, matter_id, meeting_id,
        motion_text, outcome, tally, method, source, content_sha256, receipt, parse_run_id, observation_ordinal, tally_basis, reported_body)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'minutes', $9, $10, $11, $12, $13, $14)
    ON CONFLICT (item_id, motion_index, source) DO UPDATE SET
        matter_id = EXCLUDED.matter_id, meeting_id = EXCLUDED.meeting_id,
        motion_text = EXCLUDED.motion_text, outcome = EXCLUDED.outcome,
        tally = EXCLUDED.tally, method = EXCLUDED.method,
        content_sha256 = EXCLUDED.content_sha256, receipt = EXCLUDED.receipt,
        parse_run_id=EXCLUDED.parse_run_id, observation_ordinal=EXCLUDED.observation_ordinal,
        tally_basis=EXCLUDED.tally_basis, reported_body=EXCLUDED.reported_body,
        updated_at = CURRENT_TIMESTAMP
    WHERE item_motions.source = 'minutes'
"""


REFERRER_SQL = """
    UPDATE matter_appearances SET reported_referrer = $3,
        referrer_receipt = $4, referrer_parse_run_id = $5
    WHERE meeting_id = $1 AND item_id = $2
"""


async def persist_meeting(conn, row, published, roster, to_create=(), *, run_id=None, final_indices=None, expected_items=None, referrals=()) -> bool:
    """Atomically replace a successfully parsed minutes projection, even empty.

    Missing corpus text and parser exceptions never call this function. API
    rows belong to their own writer. Revision changes abort this publication.
    Retained vote ids survive reruns; removed motions/members are retracted.
    """
    async with conn.transaction():
        await conn.fetchval("SELECT id FROM meetings WHERE id = $1 FOR UPDATE", row["meeting_id"])
        current_sha = await conn.fetchval("""
            SELECT content_sha256 FROM minutes_documents WHERE meeting_id = $1
            ORDER BY ingested_at DESC, content_sha256 DESC LIMIT 1
        """, row["meeting_id"])
        if current_sha != row["content_sha256"]:
            return False
        if expected_items is not None:
            observed_items = [dict(i) for i in await conn.fetch(ITEMS_SQL, row['meeting_id'])]
            if sorted(observed_items, key=lambda i:i['id']) != sorted(expected_items, key=lambda i:i['id']):
                return False
        current_items = {i["id"]: i["matter_id"] for i in await conn.fetch(
            "SELECT id, matter_id FROM items WHERE meeting_id = $1", row["meeting_id"])}
        for pub in published.values():
            if pub.item_id not in current_items or current_items[pub.item_id] not in (None, pub.matter_id):
                return False
        old_members = await conn.fetch(
            "SELECT DISTINCT council_member_id FROM votes WHERE meeting_id = $1",
            row["meeting_id"],
        )
        member_ids = {r["council_member_id"] for r in old_members}
        for pub in published.values():
            for name, _ in pub.votes:
                if name not in roster:
                    raise ValueError(f"Unmapped minutes voter: {name}")
                member_ids.add(roster[name])
        # Lock counters before changing their underlying rows, in one order.
        await conn.fetch("SELECT id FROM council_members WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE", sorted(member_ids))
        for matter_id, item_id, title in to_create:
            await conn.execute(CREATE_TITLE_MATTER_SQL, matter_id, row["banana"], title, row["date"])
            await conn.execute(LINK_ITEM_SQL, matter_id, item_id)
        # Explicit ownership also covers older tally-only publications.
        await conn.execute("""
            UPDATE matter_appearances SET vote_outcome = NULL, vote_tally = NULL, vote_source = NULL
            WHERE meeting_id = $1 AND vote_source = 'minutes'
        """, row["meeting_id"])
        await conn.execute("""
            UPDATE matter_appearances SET reported_referrer = NULL,
                referrer_receipt = NULL, referrer_parse_run_id = NULL
            WHERE meeting_id = $1 AND reported_referrer IS NOT NULL
        """, row["meeting_id"])
        vote_keys, motion_keys = [], []
        final = {}
        for pub in published.values():
            outcome = pub.outcome
            await conn.execute(UPSERT_MOTION_SQL, pub.item_id, pub.motion_index,
                pub.matter_id, row["meeting_id"], pub.motion_text, outcome,
                pub.tally or None, pub.method, row["content_sha256"], pub.receipt, run_id, pub.observation_index,
                pub.tally_basis, pub.reported_body)
            motion_keys.append({"item_id": pub.item_id, "motion_index": pub.motion_index})
            for seq, (name, canon) in enumerate(pub.votes, 1):
                member_id = roster[name]
                await conn.execute(UPSERT_VOTE_SQL, member_id, pub.matter_id, row["meeting_id"],
                    CANON_TO_DB[canon], row["date"], seq, {"method": pub.method},
                    pub.item_id, pub.motion_text, row["content_sha256"], pub.receipt, pub.motion_index, run_id, pub.observation_index)
                vote_keys.append({"member_id": member_id, "matter_id": pub.matter_id,
                                  "item_id": pub.item_id, "motion_index": pub.motion_index})
            if pub.item_id not in final or pub.motion_index > final[pub.item_id].motion_index:
                final[pub.item_id] = pub
        await conn.execute("""
            DELETE FROM votes v WHERE v.meeting_id = $1 AND v.source = 'minutes'
            AND NOT EXISTS (
                SELECT 1 FROM jsonb_to_recordset($2::jsonb)
                    AS k(member_id text, matter_id text, item_id text, motion_index int)
                WHERE k.member_id = v.council_member_id AND k.matter_id = v.matter_id
                  AND k.item_id = v.item_id AND k.motion_index = v.motion_index
            )
        """, row["meeting_id"], vote_keys)
        await conn.execute("""
            DELETE FROM item_motions m WHERE m.meeting_id = $1 AND m.source = 'minutes'
            AND NOT EXISTS (SELECT 1 FROM jsonb_to_recordset($2::jsonb)
                AS k(item_id text, motion_index int)
                WHERE k.item_id = m.item_id AND k.motion_index = m.motion_index)
        """, row["meeting_id"], motion_keys)
        for pub in final.values():
            if final_indices is not None and final_indices.get(pub.item_id) != pub.motion_index:
                continue
            outcome = pub.outcome
            await conn.execute(UPSERT_APPEARANCE_SQL, pub.matter_id, row["meeting_id"],
                pub.item_id, row["date"], outcome, {**pub.tally, "method": pub.method})
        # The repository owns both counters; this writer lands after its recompute,
        # so it calls that one implementation instead of counting again.
        await CouncilMemberRepository.recompute_attribution_counts(member_ids, conn)
        # last_seen is this writer's own business and takes no source preference:
        # it answers "when did we last see this member", which a superseded API
        # ballot still answers truthfully.
        await conn.execute("""
            UPDATE council_members cm
            SET last_seen = GREATEST(last_seen, (SELECT max(vote_date) FROM votes v
                WHERE v.council_member_id = cm.id))
            WHERE cm.id = ANY($1::text[])
        """, sorted(member_ids))
        for item_id, body, receipt in referrals:
            await conn.execute(REFERRER_SQL, row["meeting_id"], item_id, body, receipt, run_id)
        if run_id:
            await conn.execute("""INSERT INTO minutes_publications(meeting_id,run_id) VALUES($1,$2)
                ON CONFLICT(meeting_id) DO UPDATE SET run_id=EXCLUDED.run_id,published_at=CURRENT_TIMESTAMP""",
                row['meeting_id'], run_id)
    return True


API_VOTES_SQL = """
    SELECT id, council_member_id, matter_id, item_id, motion_index, vote
    FROM votes WHERE meeting_id=$1 AND source='api' ORDER BY id
"""


async def process_one(db, corpus, audit, row, build, apply, counts, reasons):
    text = None
    parsed = None
    inputs = {}
    try:
        async with db.pool.acquire() as conn:
            items = [dict(i) for i in await conn.fetch(ITEMS_SQL, row['meeting_id'])]
            roster_rows = [dict(i) for i in await conn.fetch(ROSTER_SQL + ' ORDER BY cm.id', row['banana'])]
            api_votes = [dict(i) for i in await conn.fetch(API_VOTES_SQL, row['meeting_id'])]
        fingerprint = digest([row.get(k) for k in ('text_key', 'text_extracted_at', 'extract_method', 'extract_version')])
        inputs = {'items': sorted(items, key=lambda i:i['id']), 'roster': roster_rows,
                  'api_votes': api_votes, 'extraction_fingerprint': fingerprint,
                  'dialect': DIALECTS.get(row['banana'])}
        if row.get('text_extracted_at'):
            text = await audit.cached_text(row['content_sha256'], row['extract_version'], fingerprint)
        if text is not None:
            counts['text_cache_hits'] += 1
        else:
            result = await corpus.lookup_extraction(row['content_sha256'])
            text = (result or {}).get('text')
        if text and "\x00" in text:
            # PDF extraction leaves NUL bytes in some Buffalo minutes. Postgres
            # refuses them in a text column, so the audit save itself raised and
            # the meeting disappeared with a log line and no failed run recorded.
            # A NUL carries no content; drop it before the hash so the cleaned
            # text is what we store and what the receipt offsets refer to.
            counts['nul_bytes_stripped'] += 1
            text = text.replace("\x00", "")
        if not text:
            counts['missing_text'] += 1
            if apply:
                async with db.pool.acquire() as conn:
                    async with conn.transaction():
                        await audit.save_run(conn,row,None,build,inputs,status='missing_text')
            return
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        if apply and await audit.is_current(row['meeting_id'],run_key(row,text_sha,build,inputs)):
            counts['unchanged_runs'] += 1
            return
        identities = MemberIdentities(roster_rows, [v['council_member_id'] for v in api_votes])
        roster, _ = identities.roster_map()
        parsed = parse_meeting(text,items,[r['name'] for r in roster_rows],DIALECTS.get(row['banana']))
        reconcile_members(parsed,identities)
        compare_api(parsed,api_votes,items,roster_rows)
        published = {}
        to_create = {}
        final_indices = {}
        for obs in parsed.observations:
            if obs.item_id and obs.kind == 'motion':
                final_indices[obs.item_id] = max(final_indices.get(obs.item_id,-1),obs.interpretation['motion_index'])
            for check in obs.checks:
                if check['status'] == 'withheld':
                    reasons[check['reason']] += 1
        for iv in parsed.published:
            obs = parsed.observations[iv.observation_index]
            matter_id = iv.item.get('matter_id') or generate_matter_id(row['banana'],title=iv.item.get('title') or '')
            if not matter_id:
                obs.checks.append(dict(field='matter',status='withheld',reason='no_durable_matter_identity'))
                obs.publication = None
                continue
            if not iv.item.get('matter_id'):
                to_create[iv.item['id']] = (matter_id,iv.item['id'],iv.item['title'])
            receipt = {'sha256':row['content_sha256'],'text_sha256':text_sha,
                       'extract_version':row['extract_version'],'unit':'unicode_codepoint',
                       'start':obs.start,'end':obs.end}
            pub = Publishable(iv.motion_index,iv.item['id'],matter_id,iv.method,iv.member_votes,
                OUTCOME_TO_DB.get(iv.outcome),dict(iv.tally),iv.motion_text,receipt,obs.ordinal,
                iv.tally_basis,iv.reported_body)
            obs.publication = asdict(pub)
            published[(iv.item['id'],iv.motion_index)] = pub
        counts['meetings'] += 1
        counts['observations'] += len(parsed.observations)
        counts['published_motions'] += len(published)
        counts['vote_rows'] += sum(len(p.votes) for p in published.values())
        if not apply:
            return
        needed = sorted({name for pub in published.values() for name,_ in pub.votes})
        await ensure_members(db,row['banana'],needed,roster)
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                # Serializes ledger/cache publication with another minutes worker.
                await conn.fetchval('SELECT id FROM meetings WHERE id=$1 FOR UPDATE',row['meeting_id'])
                run_id = await audit.save_run(conn,row,text,build,inputs,parsed)
                written = await persist_meeting(conn,row,published,roster,list(to_create.values()),
                                               run_id=run_id,final_indices=final_indices,expected_items=items,
                                               referrals=[(item_id, body, {
                                                   'sha256': row['content_sha256'], 'text_sha256': text_sha,
                                                   'extract_version': row['extract_version'],
                                                   'unit': 'unicode_codepoint',
                                                   **parsed.referral_receipts[item_id],
                                               }) for item_id, body in parsed.referrals])
        counts['meetings_written' if written else 'changed_during_parse'] += 1
    except Exception as exc:
        counts['failed'] += 1
        logger.exception('minutes interpretation failed',meeting_id=row['meeting_id'])
        if apply:
            # Recording the failure must not be able to cause one. A NUL byte in
            # the text made this write raise out of the handler and kill the
            # worker, which asyncio.gather then propagated, ending a 8,000-meeting
            # run at 1,088; a connection left mid-operation by the first failure
            # did the same at 3,789 with an asyncpg InternalClientError. One
            # unreadable meeting is not a reason to abandon the rest.
            try:
                async with db.pool.acquire() as conn:
                    async with conn.transaction():
                        await audit.save_run(conn,row,text,build,inputs,parsed,status='failed',
                            error={'type':type(exc).__name__,'message':str(exc)[:2000]})
            except Exception:
                counts['failed_unrecorded'] += 1
                logger.exception('could not record the failure',meeting_id=row['meeting_id'])


async def main() -> int:
    ap = argparse.ArgumentParser(description='Observe minutes, validate claims, publish confirmed facts')
    ap.add_argument('--banana')
    ap.add_argument('--meeting-id')
    ap.add_argument('--days-back',type=int,default=None)
    ap.add_argument('--limit',type=int)
    ap.add_argument('--concurrency',type=int,default=4)
    ap.add_argument('--apply',action='store_true',help='persist ledger and public projection (default dry run)')
    args = ap.parse_args()
    db = await Database.create()
    init_corpus(db.document_blobs)
    corpus = get_corpus()
    if corpus is None:
        await db.pool.close()
        logger.error('corpus unavailable')
        return 2
    audit = MinutesRepository(db.pool)
    counts,reasons = Counter(),Counter()
    try:
        async with db.pool.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(MEETINGS_SQL,args.banana,args.days_back)]
        if args.meeting_id:
            rows = [r for r in rows if r['meeting_id']==args.meeting_id]
        if args.limit is not None:
            rows = rows[:args.limit]
        build = parser_build()
        queue = asyncio.Queue()
        for row in rows:
            queue.put_nowait(row)
        async def worker():
            while not queue.empty():
                row = queue.get_nowait()
                try:
                    await process_one(db,corpus,audit,row,build,args.apply,counts,reasons)
                except Exception:
                    # process_one already records what it can; a worker that dies
                    # here takes every remaining meeting with it.
                    counts['worker_errors'] += 1
                    logger.exception('worker error',meeting_id=row['meeting_id'])
                queue.task_done()
                if (len(rows)-queue.qsize()) % 100 == 0:
                    logger.info('minutes progress',remaining=queue.qsize(),**counts)
        await asyncio.gather(*(worker() for _ in range(max(1,min(args.concurrency,8)))))
        print(json.dumps({'counts':dict(counts),'withheld':dict(reasons)},indent=2))
        return 1 if counts['failed'] else 0
    finally:
        await close_corpus()
        await db.pool.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
