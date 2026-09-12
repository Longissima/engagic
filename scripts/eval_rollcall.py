#!/usr/bin/env python3
"""
Run the roll-call parser over ingested minutes and measure it two ways.

Scored mode (default): meetings whose vendor API also supplied per-member
votes (Legistar cities, Chicago). Parsed votes are compared tuple-by-tuple to
the `votes` table and outcomes to matter_appearances.vote_outcome. This is
the only place the parser can be tuned against truth.

Consistency mode (--all): every meeting with minutes text and a dialect
driver, API votes or not. There is no truth for a minutes-only city, so what
is reported is what the publish gate reports: passages found, published,
abstained and why. Pair it with --audit N to print N published passages with
their motion sentence and per-member votes for a human to check against the
PDF. That audit sample is the accuracy claim for minutes-only cities until
one is contradicted.

Nothing is written. The parser is the 2026-08-04 spike, loaded from
scripts/spikes/rollcall/parse.py until it is promoted into the pipeline.

Usage:
    uv run scripts/eval_rollcall.py                      # scored, all overlap cities
    uv run scripts/eval_rollcall.py --banana denverCO --all --audit 10
    uv run scripts/eval_rollcall.py --json /tmp/rollcall_scores.json
"""

import argparse
import asyncio
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config import get_logger
from corpus.store import close_corpus, get_corpus, init_corpus
from database.db_postgres import Database
from parsing.rollcall import DIALECTS, load_spike_parser, norm_file
from parsing.rollcall.evidence import RESULT_RE

logger = get_logger(__name__).bind(component="eval_rollcall")

DB_TO_CANON = {
    "yes": "AYE",
    "no": "NO",
    "abstain": "ABSTAIN",
    "absent": "ABSENT",
    "present": "PRESENT",
    "recused": "RECUSED",
    "not_voting": "NONVOTING",
}
OUTCOME_TO_CANON = {"passed": "PASS", "failed": "FAIL"}

# Deliberately broader than anything the parser matches: if a clerk recorded
# a vote at all, one of these words is in the document. A document with none
# of them recorded no vote, and belongs in no coverage denominator -- a
# cancelled meeting or a discussion-only committee is not a miss.
VOTE_LANGUAGE_RE = re.compile(
    r"\b(?:motion|motioned|moved|second(?:ed)?|ayes?|nays?|yeas?|abstain\w*|"
    r"roll\s*call|carried|unanimous\w*|in\s+favor|opposed)\b",
    re.IGNORECASE,
)

COVERAGE_SQL = """
    SELECT DISTINCT ON (md.meeting_id)
           md.meeting_id, md.content_sha256, m.banana, j.vendor
    FROM minutes_documents md
    JOIN meetings m ON m.id = md.meeting_id
    JOIN jurisdictions j USING (banana)
    WHERE ($1::text IS NULL OR m.banana = $1)
    ORDER BY md.meeting_id, md.ingested_at DESC
"""
COVERAGE_ITEMS_SQL = """
    SELECT i.id, i.sequence, i.agenda_number, i.title, i.matter_id,
           COALESCE(i.matter_file, cm.matter_file) AS matter_file
    FROM items i
    LEFT JOIN city_matters cm ON cm.id = i.matter_id
    WHERE i.meeting_id = $1
"""

MEETINGS_SQL = """
    SELECT DISTINCT ON (md.meeting_id)
           md.meeting_id, md.content_sha256, m.banana, m.title, m.date,
           EXISTS (SELECT 1 FROM votes v WHERE v.meeting_id = md.meeting_id) AS has_api_votes
    FROM minutes_documents md
    JOIN meetings m ON m.id = md.meeting_id
    WHERE ($1::text IS NULL OR m.banana = $1)
    ORDER BY md.meeting_id, md.ingested_at DESC
"""

GT_SQL = """
    SELECT cm.matter_file, c.name AS person, v.vote, ma.vote_outcome
    FROM votes v
    JOIN council_members c ON c.id = v.council_member_id
    JOIN city_matters cm ON cm.id = v.matter_id
    LEFT JOIN matter_appearances ma
           ON ma.matter_id = v.matter_id AND ma.meeting_id = v.meeting_id
    WHERE v.meeting_id = $1
"""

ROSTER_SQL = "SELECT name FROM council_members WHERE banana = $1"


@dataclass
class GroundTruthItem:
    tuples: Set[Tuple[str, str]] = field(default_factory=set)
    outcome: Optional[str] = None


def parse_meeting(parse, dialect: str, text: str) -> Tuple[list, Dict[Optional[str], list]]:
    passages = parse.PARSERS[dialect](text)
    by_file: Dict[Optional[str], list] = defaultdict(list)
    for passage in passages:
        if passage.sections:
            by_file[norm_file(dialect, passage.matter_file)].append(passage)
    return passages, by_file


def consistency(passages: list, gazetteer, counts: Counter, taxonomy: Counter, audit: List[str], tag: str, audit_limit: int) -> None:
    counts["passages"] += len(passages)
    for passage in passages:
        if not passage.sections:
            counts["passages_without_votes"] += 1
            continue
        decision = passage.evaluate_publish_gate(gazetteer)
        if decision.publishable:
            counts["published"] += 1
            if len(audit) < audit_limit:
                votes = ", ".join(f"{m}={v}" for m, v in sorted(decision.votes))
                audit.append(
                    f"{tag} file={passage.matter_file} outcome={passage.outcome} action={passage.action}\n"
                    f"    motion: {passage.motion_text[:220]}\n    votes: {votes}"
                )
        else:
            counts["abstained"] += 1
            for reason in decision.reasons:
                taxonomy[f"gate:{reason.split(':')[0]}"] += 1


def score(dialect: str, by_file: Dict[Optional[str], list], gt_rows, gazetteer, counts: Counter, taxonomy: Counter, examples: Dict[str, List[str]], tag: str) -> None:
    gt_items: Dict[str, GroundTruthItem] = defaultdict(GroundTruthItem)
    for row in gt_rows:
        key = norm_file(dialect, row["matter_file"])
        if key is None:
            counts["gt_no_file"] += 1
            continue
        item = gt_items[key]
        item.tuples.add((row["person"], DB_TO_CANON.get(row["vote"], str(row["vote"]).upper())))
        if row["vote_outcome"] in OUTCOME_TO_CANON:
            item.outcome = OUTCOME_TO_CANON[row["vote_outcome"]]

    for key, gt in gt_items.items():
        counts["gt_items"] += 1
        counts["gt_tuples"] += len(gt.tuples)
        cands = by_file.get(key, [])
        if not cands:
            counts["item_not_found"] += 1
            taxonomy["item_not_found_in_minutes"] += 1
            examples["item_not_found_in_minutes"].append(f"{tag} {key}")
            continue
        # The votes table holds one roll call per matter per meeting; minutes
        # may hold several (amend, then adopt). Score the last, which is the
        # disposition the API records.
        passage = cands[-1]
        if len(cands) > 1:
            counts["multi_motion_items"] += 1
        decision = passage.evaluate_publish_gate(gazetteer)
        if not decision.publishable:
            counts["gate_abstained"] += 1
            for reason in decision.reasons:
                taxonomy[f"gate:{reason.split(':')[0]}"] += 1
            examples["gate_abstained"].append(f"{tag} {key} {decision.reasons[:2]}")
            if set(decision.votes) == gt.tuples:
                counts["false_abstention"] += 1
            continue
        counts["published"] += 1
        extracted = set(decision.votes)
        counts["ext_tuples"] += len(extracted)
        counts["tuples_matched"] += len(extracted & gt.tuples)
        if extracted != gt.tuples:
            counts["items_with_diff"] += 1
            taxonomy["published_diff"] += 1
            examples["published_diff"].append(
                f"{tag} {key} extra={sorted(extracted - gt.tuples)[:3]} missing={sorted(gt.tuples - extracted)[:3]}"
            )
        if gt.outcome and passage.outcome:
            counts["outcome_compared"] += 1
            if gt.outcome == passage.outcome:
                counts["outcome_correct"] += 1
            else:
                examples["outcome_wrong"].append(f"{tag} {key} gt={gt.outcome} parsed={passage.outcome}")


def summarize(counts: Counter) -> Dict[str, Any]:
    out: Dict[str, Any] = {"counts": dict(counts)}
    if counts["ext_tuples"]:
        out["precision"] = round(counts["tuples_matched"] / counts["ext_tuples"], 4)
    if counts["gt_tuples"]:
        out["recall"] = round(counts["tuples_matched"] / counts["gt_tuples"], 4)
    if counts["outcome_compared"]:
        out["outcome_acc"] = round(counts["outcome_correct"] / counts["outcome_compared"], 4)
    if counts["published"] + counts["abstained"]:
        out["publish_rate"] = round(counts["published"] / (counts["published"] + counts["abstained"]), 4)
    return out


async def coverage(db, corpus, banana: Optional[str], limit: Optional[int]) -> Dict[str, Any]:
    """Two numbers, kept apart: documents held, and votes recorded vs encoded.

    Supply ("minutes fetched") and extraction ("vote pattern outcome") fail
    for unrelated reasons and mixing them hides both. Only documents that
    record a vote enter the extraction denominator.
    """
    from parsing.rollcall.engine import parse_meeting

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(COVERAGE_SQL, banana)
    if limit:
        rows = rows[:limit]
    stage: Counter = Counter()
    per_vendor: Dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        vendor = row["vendor"]
        result = await corpus.lookup_extraction(row["content_sha256"])
        text = (result or {}).get("text") or ""
        if not text:
            stage["held_no_text"] += 1
            continue
        if not VOTE_LANGUAGE_RE.search(text):
            stage["held_records_no_vote"] += 1
            per_vendor[vendor]["no_vote"] += 1
            continue
        async with db.pool.acquire() as conn:
            items = [dict(r) for r in await conn.fetch(COVERAGE_ITEMS_SQL, row["meeting_id"])]
            roster = [r["name"] for r in await conn.fetch(ROSTER_SQL, row["banana"])]
        per_vendor[vendor]["denominator"] += 1
        if not items:
            stage["miss_meeting_has_no_items"] += 1
            continue
        parsed = parse_meeting(text, items, roster)
        if parsed.items_anchored == 0:
            stage["miss_no_item_anchored"] += 1
        elif parsed.evidence_seen == 0:
            # Split the biggest miss bucket by cause: a result the parser
            # recognizes somewhere in the document means the item blocks are
            # drawn wrong; none anywhere means the phrasing is unknown to us.
            # They need opposite fixes, so counting them together hides both.
            if RESULT_RE.search(text):
                stage["miss_evidence_outside_blocks"] += 1
            else:
                stage["miss_phrasing_unrecognized"] += 1
        elif not parsed.published:
            stage["miss_all_abstained"] += 1
        else:
            stage["hit_published"] += 1
            per_vendor[vendor]["published"] += 1
    denominator = sum(v for k, v in stage.items() if k.startswith(("miss_", "hit_")))
    return {
        "documents_held": len(rows),
        "held_records_no_vote": stage["held_records_no_vote"],
        "held_no_text": stage["held_no_text"],
        "extraction_denominator": denominator,
        "extraction_rate": round(stage["hit_published"] / denominator, 4) if denominator else None,
        "stages": dict(stage),
        "by_vendor": {
            v: {
                "denominator": c["denominator"],
                "published": c["published"],
                "records_no_vote": c["no_vote"],
                "rate": round(c["published"] / c["denominator"], 3) if c["denominator"] else None,
            }
            for v, c in sorted(per_vendor.items(), key=lambda kv: -kv[1]["denominator"])
        },
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="Roll-call parser over corpus minutes: scored vs API votes, or gate consistency")
    ap.add_argument("--banana")
    ap.add_argument("--all", action="store_true", help="consistency mode over every meeting with minutes text")
    ap.add_argument("--audit", type=int, default=0, help="print N published passages for human review")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--coverage", action="store_true",
                    help="report supply and extraction coverage separately")
    ap.add_argument("--json", help="write machine-readable results here")
    args = ap.parse_args()

    parse = load_spike_parser()
    db = await Database.create()
    init_corpus(db.document_blobs)
    corpus = get_corpus()
    if corpus is None:
        logger.error("corpus unavailable")
        return 2
    try:
        if args.coverage:
            report = await coverage(db, corpus, args.banana, args.limit)
            print(json.dumps(report, indent=1, default=str))
            return 0
        async with db.pool.acquire() as conn:
            meetings = await conn.fetch(MEETINGS_SQL, args.banana)
        if not args.all:
            meetings = [m for m in meetings if m["has_api_votes"]]
        if args.limit:
            meetings = meetings[: args.limit]

        scored: Dict[str, Counter] = defaultdict(Counter)
        consistent: Dict[str, Counter] = defaultdict(Counter)
        taxonomy: Counter = Counter()
        examples: Dict[str, List[str]] = defaultdict(list)
        audit: List[str] = []
        no_driver: Counter = Counter()
        no_text: Counter = Counter()

        for row in meetings:
            dialect = DIALECTS.get(row["banana"])
            if not dialect:
                no_driver[row["banana"]] += 1
                continue
            result = await corpus.lookup_extraction(row["content_sha256"])
            text = (result or {}).get("text") or ""
            if not text:
                no_text[row["banana"]] += 1
                continue
            async with db.pool.acquire() as conn:
                roster = [r["name"] for r in await conn.fetch(ROSTER_SQL, row["banana"])]
                gt_rows = await conn.fetch(GT_SQL, row["meeting_id"]) if row["has_api_votes"] else []
            gazetteer = parse.Gazetteer(sorted(set(roster) | {r["person"] for r in gt_rows}))
            passages, by_file = parse_meeting(parse, dialect, text)
            tag = row["meeting_id"]

            counts = consistent[row["banana"]]
            counts["meetings"] += 1
            consistency(passages, gazetteer, counts, taxonomy, audit, tag, args.audit)
            if gt_rows:
                scounts = scored[row["banana"]]
                scounts["meetings"] += 1
                score(dialect, by_file, gt_rows, gazetteer, scounts, taxonomy, examples, tag)

        report = {
            "meetings_considered": len(meetings),
            "no_driver": dict(no_driver),
            "no_text": dict(no_text),
            "scored": {city: summarize(c) for city, c in scored.items()},
            "consistency": {city: summarize(c) for city, c in consistent.items()},
            "taxonomy": dict(taxonomy),
            "examples": {k: v[:8] for k, v in examples.items()},
        }
        print(json.dumps(report, indent=1, default=str))
        if audit:
            print("\n=== AUDIT SAMPLE (check each against the PDF) ===")
            print("\n".join(audit))
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=1, default=str))
        return 0
    finally:
        await close_corpus()
        await db.pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
