#!/usr/bin/env python3
"""Audit or apply current item filters and requeue newly eligible meetings."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from typing import Any, Mapping

from database.db_postgres import Database
from pipeline.filters import get_filter_decision, system_filter_decision
from pipeline.orchestrators.enqueue_decider import EnqueueDecider
from pipeline.utils import meeting_work_version
from vendors.adapters.parsers.morphology import is_bare_document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--sample-limit", type=int, default=10)
    return parser


def _chunk_is_bare(processing_metadata: Mapping[str, Any] | None) -> bool:
    chunk = (processing_metadata or {}).get("chunk") or {}
    profiles = [
        run.get("profile")
        for run in chunk.get("runs", [])
        if isinstance(run.get("profile"), dict)
    ]
    return bool(profiles) and all(is_bare_document(profile) for profile in profiles)


def desired_filter(row: Mapping[str, Any], processing_metadata=None):
    decision = get_filter_decision(str(row.get("title") or ""))
    if decision:
        return decision
    if not row.get("attachments") and not row.get("body_text"):
        return system_filter_decision("no_content")
    if row.get("filter_reason") == "bare_agenda" and _chunk_is_bare(
        processing_metadata
    ):
        return system_filter_decision("bare_agenda")
    return None


async def _meeting_rows(conn, meeting_id: str, *, lock: bool = False):
    lock_clause = "FOR UPDATE" if lock else ""
    return await conn.fetch(
        f"""
        SELECT id, title, attachments, body_text, summary, filter_reason,
               filter_rule_id, filter_version
        FROM items
        WHERE meeting_id = $1
          AND filter_reason IS NOT NULL
        ORDER BY sequence, id
        {lock_clause}
        """,
        meeting_id,
    )


async def _latest_metadata(conn, meeting_id: str):
    row = await conn.fetchrow(
        """
        SELECT processing_metadata
        FROM queue
        WHERE meeting_id = $1 AND job_type = 'meeting'
        ORDER BY last_enqueued_at DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        meeting_id,
    )
    return dict(row["processing_metadata"] or {}) if row else {}


async def _apply_meeting(db: Database, meeting_id: str) -> Counter:
    counts: Counter = Counter()
    became_eligible = False
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            meeting = await db.meetings.get_meeting(
                meeting_id, conn=conn, lock_for_update=True
            )
            if meeting is None:
                counts["missing_meeting"] += 1
                return counts
            rows = await _meeting_rows(conn, meeting_id, lock=True)
            metadata = await _latest_metadata(conn, meeting_id)
            for raw in rows:
                row = dict(raw)
                decision = desired_filter(row, metadata)
                new_reason = decision.reason if decision else None
                if row["filter_reason"] != new_reason:
                    counts[
                        "cleared" if new_reason is None else "recategorized"
                    ] += 1
                    became_eligible |= new_reason is None
                elif (
                    row["filter_version"] != (decision.version if decision else None)
                    or row["filter_rule_id"] != (decision.rule_id if decision else None)
                ):
                    counts["restamped"] += 1
                else:
                    counts["unchanged"] += 1
                    continue
                await db.items.update_filter_reason(
                    row["id"],
                    new_reason,
                    rule_id=decision.rule_id if decision else None,
                    filter_version=(
                        decision.version
                        if decision
                        else system_filter_decision("cleared").version
                    ),
                    source="filter_replay",
                    conn=conn,
                )

            if became_eligible:
                items = await db.items.get_agenda_items(
                    meeting_id, conn=conn, lock_for_update=True
                )
                chunk = metadata.get("chunk")
                should_enqueue, _ = EnqueueDecider().should_enqueue(
                    meeting, items, bool(items), chunk
                )
            else:
                should_enqueue = False
            if should_enqueue:
                work_version = meeting_work_version(meeting, items)
                if chunk:
                    metadata["chunk"] = {**chunk, "work_version": work_version}
                await db.pipeline_lifecycle.enqueue_queue_job(
                    source_url=f"meeting://{meeting_id}",
                    job_type="meeting",
                    payload={"meeting_id": meeting_id},
                    aggregate_id=meeting_id,
                    meeting_id=meeting_id,
                    banana=meeting.banana,
                    priority=EnqueueDecider().calculate_priority(meeting.date),
                    work_version=work_version,
                    processing_metadata=metadata or None,
                    conn=conn,
                )
                counts["meetings_requeued"] += 1
    return counts


async def recompute(
    *, execute: bool, limit: int | None, batch_size: int, sample_limit: int
):
    db = await Database.create(min_size=1, max_size=5)
    counts: Counter = Counter()
    transitions: Counter = Counter()
    samples = []
    sampled: Counter = Counter()
    cursor = ""
    inspected = 0
    try:
        while limit is None or inspected < limit:
            size = min(batch_size, limit - inspected) if limit else batch_size
            async with db.pool.acquire() as conn:
                meetings = await conn.fetch(
                    """
                    SELECT DISTINCT meeting_id
                    FROM items
                    WHERE filter_reason IS NOT NULL AND meeting_id > $1
                    ORDER BY meeting_id
                    LIMIT $2
                    """,
                    cursor,
                    size,
                )
            if not meetings:
                break
            page_ids = [row["meeting_id"] for row in meetings]
            if execute:
                page_counts = await asyncio.gather(
                    *(_apply_meeting(db, meeting_id) for meeting_id in page_ids)
                )
                for result in page_counts:
                    counts.update(result)
                inspected += len(page_ids)
                cursor = page_ids[-1]
                continue
            dry_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
            dry_metadata: dict[str, dict[str, Any]] = {}
            if not execute:
                async with db.pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT i.id, i.meeting_id, i.title, i.attachments,
                               i.body_text, i.summary, i.filter_reason,
                               i.filter_rule_id, i.filter_version
                        FROM items i
                        WHERE i.meeting_id = ANY($1::text[])
                          AND i.filter_reason IS NOT NULL
                        """,
                        page_ids,
                    )
                    metadata_rows = await conn.fetch(
                        """
                        SELECT DISTINCT ON (meeting_id)
                               meeting_id, processing_metadata
                        FROM queue
                        WHERE meeting_id = ANY($1::text[])
                          AND job_type = 'meeting'
                        ORDER BY meeting_id,
                                 last_enqueued_at DESC NULLS LAST,
                                 id DESC
                        """,
                        page_ids,
                    )
                for raw in rows:
                    dry_rows[raw["meeting_id"]].append(dict(raw))
                dry_metadata = {
                    raw["meeting_id"]: dict(raw["processing_metadata"] or {})
                    for raw in metadata_rows
                }
            for meeting_row in meetings:
                meeting_id = meeting_row["meeting_id"]
                cursor = meeting_id
                inspected += 1
                for row in dry_rows[meeting_id]:
                    decision = desired_filter(row, dry_metadata.get(meeting_id))
                    new_reason = decision.reason if decision else None
                    if row["filter_reason"] == new_reason:
                        counts["retained"] += 1
                    else:
                        key = "cleared" if new_reason is None else "recategorized"
                        counts[key] += 1
                        transition = f"{row['filter_reason']}->{new_reason or 'eligible'}"
                        transitions[transition] += 1
                        if sampled[transition] < sample_limit:
                            sampled[transition] += 1
                            samples.append({
                                "item_id": row["id"],
                                "old_reason": row["filter_reason"],
                                "new_reason": new_reason,
                                "title": str(row["title"] or "")[:200],
                            })
    finally:
        await db.close()
    return {
        "mode": "execute" if execute else "dry-run",
        "meetings_inspected": inspected,
        "counts": dict(counts),
        "transitions": dict(transitions),
        "samples": samples,
    }


def main() -> None:
    args = build_parser().parse_args()
    print(json.dumps(asyncio.run(recompute(
        execute=args.execute,
        limit=args.limit,
        batch_size=args.batch_size,
        sample_limit=args.sample_limit,
    )), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
