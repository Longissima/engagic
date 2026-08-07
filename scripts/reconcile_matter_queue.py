#!/usr/bin/env python3
"""Audit or explicitly reconcile historical matter queue descriptors.

Dry-run is the default. Pass --execute to publish/reactivate planned work.
This utility never deletes data and prints an action summary suitable for a
deployment record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from typing import Any

from database.db_postgres import Database
from pipeline.reconciliation import ReconciliationAction, plan_matter_reconciliation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="apply planned enqueue/reactivation actions (default: dry-run)",
    )
    parser.add_argument("--limit", type=int, help="inspect at most this many matters")
    parser.add_argument("--batch-size", type=int, default=500)
    return parser


def _plan(
    *,
    matter_id: str,
    appearances: list[Any],
    queue_row: Any,
    canonical_summary: str | None,
    metadata: Any,
    canonical_title: str | None,
):
    metadata = metadata or {}
    get = metadata.get if isinstance(metadata, dict) else lambda key, default=None: getattr(
        metadata, key, default
    )
    return plan_matter_reconciliation(
        matter_id=matter_id,
        appearances=appearances,
        queue_row=queue_row,
        canonical_summary=canonical_summary,
        canonical_attachment_hash=get("attachment_hash"),
        canonical_work_version=get("work_version"),
        canonical_title=canonical_title,
        canonical_disposition=get("disposition"),
        canonical_attempts=int(get("attempts", 0) or 0),
    )


async def _apply_current_plan(db: Database, matter_id: str):
    """Recompute and apply under aggregate + queue locks; never publish stale work."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            matter = await db.matters.get_matter(
                matter_id,
                conn=conn,
                lock_for_update=True,
            )
            if matter is None:
                return None
            active_outbox = (
                await db.pipeline_lifecycle.has_unresolved_outbox_for_aggregate(
                    event_type="queue.enqueue",
                    aggregate_type="matter",
                    aggregate_id=matter_id,
                    conn=conn,
                )
            )
            if active_outbox:
                return None
            appearances = await db.items.get_all_items_for_matter(
                matter_id,
                conn=conn,
                lock_for_update=True,
            )
            source_url = f"matter://{matter_id}"
            queue_row = await db.queue.lock_desired_state(
                source_url,
                conn=conn,
            )
            plan = _plan(
                matter_id=matter_id,
                appearances=appearances,
                queue_row=queue_row,
                canonical_summary=matter.canonical_summary,
                metadata=matter.metadata,
                canonical_title=matter.title,
            )
            if plan.action is ReconciliationAction.NONE:
                return plan

            await db.queue.enqueue_job(
                source_url=source_url,
                job_type="matter",
                payload={"matter_id": matter_id},
                meeting_id=None,
                banana=matter.banana,
                priority=150,
                work_version=plan.desired_version,
                conn=conn,
            )
            if plan.action is ReconciliationAction.REACTIVATE_VERSION:
                reactivated = await db.queue.reactivate_job_version(
                    source_url=source_url,
                    work_version=plan.desired_version,
                    priority=150,
                    conn=conn,
                )
                if not reactivated:
                    raise RuntimeError("current matter queue version was not reactivated")
            return plan


async def reconcile(*, execute: bool, limit: int | None, batch_size: int) -> dict[str, Any]:
    db = await Database.create(min_size=1, max_size=5)
    counts: Counter = Counter()
    samples: list[dict[str, Any]] = []
    cursor = ""
    inspected = 0
    try:
        while limit is None or inspected < limit:
            page_size = min(batch_size, limit - inspected) if limit is not None else batch_size
            async with db.pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT cm.id, cm.banana, cm.title, cm.canonical_summary, cm.metadata,
                           q.status AS queue_status, q.work_version
                    FROM city_matters cm
                    LEFT JOIN queue q ON q.source_url = 'matter://' || cm.id
                    WHERE cm.id > $1
                    ORDER BY cm.id
                    LIMIT $2
                    """,
                    cursor,
                    page_size,
                )
            if not rows:
                break

            page_ids = [row["id"] for row in rows]
            appearances_by_matter = await db.items.get_all_items_for_matters(page_ids)

            for row in rows:
                cursor = row["id"]
                inspected += 1
                appearances = appearances_by_matter.get(row["id"], [])
                metadata = row["metadata"] or {}
                queue_row = (
                    {"status": row["queue_status"], "work_version": row["work_version"]}
                    if row["queue_status"] is not None
                    else None
                )
                plan = _plan(
                    matter_id=row["id"],
                    appearances=appearances,
                    queue_row=queue_row,
                    canonical_summary=row["canonical_summary"],
                    metadata=metadata,
                    canonical_title=row["title"],
                )
                counts[plan.action.value] += 1
                counts[f"reason:{plan.reason}"] += 1
                if plan.action is not ReconciliationAction.NONE and len(samples) < 25:
                    samples.append(
                        {
                            "matter_id": plan.matter_id,
                            "action": plan.action.value,
                            "reason": plan.reason,
                            "desired_version": plan.desired_version,
                        }
                    )

                if not execute or plan.action is ReconciliationAction.NONE:
                    continue
                applied_plan = await _apply_current_plan(db, plan.matter_id)
                if (
                    applied_plan is None
                    or applied_plan.action is ReconciliationAction.NONE
                ):
                    counts["changed_before_apply"] += 1
                    continue
                counts["applied"] += 1
    finally:
        await db.close()

    return {
        "mode": "execute" if execute else "dry-run",
        "inspected": inspected,
        "counts": dict(counts),
        "samples": samples,
    }


def main() -> None:
    args = build_parser().parse_args()
    result = asyncio.run(
        reconcile(execute=args.execute, limit=args.limit, batch_size=args.batch_size)
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
