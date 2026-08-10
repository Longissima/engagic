#!/usr/bin/env python3
"""
Re-summarize items produced by an older prompts version.

The batch lane makes historical reprocessing affordable (50% token cost,
separate quota pool from streaming). This script unfreezes matching item
summaries — setting summary NULL lifts the freeze-on-summary invariant —
and re-enqueues their meetings. Past-dated meetings land in the batch lane
automatically; only the urgent window streams.

Matters are invalidated alongside their items. Matter work versions hash
appearance inputs only — never prompts_version — so without intervention
the matter lane would keep seeing a current canonical projection and take
its "canonical summary current" skip forever, serving old-prompt output
indefinitely. After the meeting pass, every matter linked to an actually
unfrozen item has its canonical summary cleared and its matter job
published (enqueue plus exact-version reactivation) in one per-matter
transaction, forcing one aggregate re-summarization under the current
prompt. Appearances outside the target set are unaffected: the matter lane
re-summarizes from the aggregate attachment set, not from item summaries.

prompts_version semantics: NULL = summarized before provenance existed
(pre-2026-06), the oldest cohort. Versions compare lexicographically,
which is correct for the single-digit 'v2'/'v3' scheme.

Deferral runs before any write so deferred work stays targetable: a
meeting with open batch chunks is skipped whole, and a matter with an open
chunk owning any of its appearance snapshots has its items excluded from
the unfreeze. city_matters carries no prompts provenance of its own, so a
matter is only ever re-targeted through its below-version items — which is
why deferral must never fire after those items have been NULLed.

Caveat: if a re-sync runs before the queue drains, the sync-time
attachments-unchanged copy can refill a NULLed row from a prior appearance
(carrying that appearance's old prompts_version). Such rows stay targeted
by future runs of this script and are replaced when the meeting job lands.

--attachments-matching narrows targeting to items whose attachments JSON
matches a case-insensitive regex (i.attachments::text ~* pattern). This
reproduces the transactional-document targeting of the retired
scripts/backfill_v32_summaries.py shadow pipeline, whose selection regex
was:

    "name": *"[^"]*(quote|contract|agreement|proposal|exhibit|purchase order|order form|pricing|bid tab|statement of work|sow)[^"]*"

That script also carried its own corpus-only reconstruction; this is now
unnecessary because document acquisition is corpus-first with fail-open,
so items with expired vendor URLs re-summarize from the corpus revision
through the normal queue path.

Usage:
    uv run python scripts/resummarize_items.py --below v3 --dry-run
    uv run python scripts/resummarize_items.py --below v3 --banana gainesvilleFL --yes
    uv run python scripts/resummarize_items.py --below v3 --limit 200 --yes
    uv run python scripts/resummarize_items.py --below v3.2 \
        --attachments-matching '"name": *"[^"]*(quote|contract|...)[^"]*"' --yes
"""

import argparse
import asyncio

from config import get_logger
from database.db_postgres import Database
from pipeline.utils import matter_work_version, meeting_work_version

logger = get_logger(__name__)

# Re-enqueued backfill work must never outrank fresh meetings.
BACKFILL_PRIORITY = -10


async def find_targets(
    db: Database,
    below: str,
    banana: str | None,
    limit: int | None,
    attachments_matching: str | None = None,
):
    """Meetings (newest first) holding summaries from before `below`."""
    conditions = [
        "i.summary IS NOT NULL",
        "(i.prompts_version IS NULL OR i.prompts_version < $1)",
    ]
    params: list = [below]
    if banana:
        params.append(banana)
        conditions.append(f"m.banana = ${len(params)}")
    if attachments_matching:
        params.append(attachments_matching)
        conditions.append(f"i.attachments::text ~* ${len(params)}")

    sql = f"""
        SELECT m.id AS meeting_id, m.banana, m.date,
               array_agg(i.id) AS item_ids,
               array_agg(DISTINCT i.matter_id)
                   FILTER (WHERE i.matter_id IS NOT NULL) AS matter_ids
        FROM items i
        JOIN meetings m ON m.id = i.meeting_id
        WHERE {" AND ".join(conditions)}
        GROUP BY m.id, m.banana, m.date
        ORDER BY m.date DESC NULLS LAST
    """
    if limit:
        params.append(limit)
        sql += f" LIMIT ${len(params)}"

    async with db.pool.acquire() as conn:
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def apply(db: Database, targets: list, below: str) -> tuple[int, int, int]:
    unfrozen = 0
    enqueued = 0
    unfrozen_matter_ids: set[str] = set()
    for t in targets:
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                meeting = await db.meetings.get_meeting(
                    t["meeting_id"],
                    conn=conn,
                    lock_for_update=True,
                )
                if meeting is None:
                    logger.warning(
                        "skipping missing meeting during resummarization",
                        meeting_id=t["meeting_id"],
                    )
                    continue
                # Output invalidation preserves mv1 because prompts/summary are
                # projections. An older open provider row would therefore stay
                # version-compatible and could refill the just-unfrozen item.
                # The meeting lock serializes this check with both reservation
                # and collection; defer the whole meeting until its batch group
                # is terminal.
                if await db.batch_jobs.count_open_for_meeting(
                    t["meeting_id"], conn=conn
                ):
                    logger.info(
                        "deferring resummarization with open batch work",
                        meeting_id=t["meeting_id"],
                    )
                    continue
                # Locked before the unfreeze so the same rows ground both the
                # matter-deferral gate and the meeting work version (which
                # hashes stable inputs only, never summary/prompts_version).
                items = await db.items.get_agenda_items(
                    t["meeting_id"],
                    conn=conn,
                    lock_for_update=True,
                )
                # Matter deferral must precede the unfreeze: NULLing an item
                # burns the below-version provenance that makes its matter
                # targetable (city_matters has no prompts provenance), so a
                # matter deferred any later could strand a stale canonical
                # forever. Excluding its items keeps them — and therefore the
                # matter — targeted by a future run, mirroring the
                # defer-before-write shape of the meeting gate above.
                candidates = set(t["item_ids"])
                matter_by_item = {item.id: item.matter_id for item in items}
                deferred_matters: set[str] = set()
                for matter_id in sorted(
                    {
                        item.matter_id
                        for item in items
                        if item.id in candidates and item.matter_id
                    }
                ):
                    if await db.batch_jobs.count_open_for_matter(
                        matter_id, conn=conn
                    ):
                        deferred_matters.add(matter_id)
                        logger.info(
                            "deferring matter resummarization with open batch work",
                            matter_id=matter_id,
                            meeting_id=t["meeting_id"],
                        )
                unfreeze_ids = [
                    item_id
                    for item_id in t["item_ids"]
                    if matter_by_item.get(item_id) not in deferred_matters
                ]
                if not unfreeze_ids:
                    continue
                rows = await conn.fetch(
                    """
                    UPDATE items
                    SET summary = NULL, prompts_version = NULL
                    WHERE id = ANY($1::text[])
                      AND meeting_id = $2
                      AND summary IS NOT NULL
                      AND (prompts_version IS NULL OR prompts_version < $3)
                    RETURNING matter_id
                    """,
                    unfreeze_ids,
                    t["meeting_id"],
                    below,
                )
                changed = len(rows)
                if changed == 0:
                    continue
                work_version = meeting_work_version(meeting, items)
                source_url = f"meeting://{t['meeting_id']}"

                # Output invalidation deliberately preserves the same input
                # work_version, so its stable outbox event may already be
                # published. Exact-version reactivation is the explicit retry
                # authority; keep it in this transaction with the NULL writes.
                await db.queue.enqueue_job(
                    source_url=source_url,
                    job_type="meeting",
                    payload={"meeting_id": t["meeting_id"]},
                    meeting_id=t["meeting_id"],
                    priority=BACKFILL_PRIORITY,
                    banana=meeting.banana,
                    work_version=work_version,
                    conn=conn,
                )
                if not await db.queue.reactivate_job_version(
                    source_url=source_url,
                    work_version=work_version,
                    priority=BACKFILL_PRIORITY,
                    conn=conn,
                ):
                    raise RuntimeError(
                        f"authoritative meeting version was not reactivated: "
                        f"{t['meeting_id']}"
                    )
                unfrozen += changed
                enqueued += 1
                unfrozen_matter_ids.update(
                    row["matter_id"] for row in rows if row["matter_id"]
                )

    # Matters aggregate across meetings, so a matter reached through several
    # targeted meetings is invalidated exactly once, in its own transaction
    # after the meeting loop: a per-meeting boundary would double-touch shared
    # matters and drag cross-meeting item locks into meeting-scoped work.
    matters_enqueued = 0
    for matter_id in sorted(unfrozen_matter_ids):
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                # Processor lock order (matter row, then all its appearance
                # rows) serializes this invalidation against every matter-lane
                # CAS transaction and against batch reservation/collection,
                # which lock the same item rows.
                matter = await db.matters.get_matter(
                    matter_id,
                    conn=conn,
                    lock_for_update=True,
                )
                if matter is None:
                    logger.warning(
                        "skipping missing matter during resummarization",
                        matter_id=matter_id,
                    )
                    continue
                appearances = await db.items.get_all_items_for_matter(
                    matter_id,
                    conn=conn,
                    lock_for_update=True,
                )
                if not appearances:
                    logger.warning(
                        "skipping matter without appearances during resummarization",
                        matter_id=matter_id,
                    )
                    continue
                await db.matters.invalidate_canonical_summary(matter_id, conn=conn)
                # Same publication contract as the meeting side: appearance
                # inputs are unchanged so this exact version may already have
                # a settled queue row; exact-version reactivation is the
                # explicit retry authority, committed with the invalidation.
                work_version = matter_work_version(appearances)
                source_url = f"matter://{matter_id}"
                await db.queue.enqueue_job(
                    source_url=source_url,
                    job_type="matter",
                    payload={"matter_id": matter_id},
                    meeting_id=None,
                    priority=BACKFILL_PRIORITY,
                    banana=matter.banana,
                    work_version=work_version,
                    conn=conn,
                )
                if not await db.queue.reactivate_job_version(
                    source_url=source_url,
                    work_version=work_version,
                    priority=BACKFILL_PRIORITY,
                    conn=conn,
                ):
                    raise RuntimeError(
                        f"authoritative matter version was not reactivated: "
                        f"{matter_id}"
                    )
                matters_enqueued += 1
    return unfrozen, enqueued, matters_enqueued


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-summarize items produced by an older prompts version"
    )
    parser.add_argument("--below", required=True, help="target items with prompts_version < this (NULL always matches)")
    parser.add_argument("--banana", help="restrict to one city")
    parser.add_argument(
        "--attachments-matching",
        metavar="REGEX",
        help="restrict to items whose attachments JSON matches this "
        "case-insensitive regex (attachments::text ~* REGEX); reproduces the "
        "retired backfill_v32_summaries.py transactional-document targeting "
        "(see module docstring for that regex)",
    )
    parser.add_argument("--limit", type=int, help="max meetings to re-enqueue (newest first)")
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument("--yes", action="store_true", help="apply without prompting")
    args = parser.parse_args()

    db = await Database.create()
    try:
        targets = await find_targets(
            db,
            args.below,
            args.banana,
            args.limit,
            attachments_matching=args.attachments_matching,
        )
        item_count = sum(len(t["item_ids"]) for t in targets)
        matter_count = len(
            {matter_id for t in targets for matter_id in (t["matter_ids"] or [])}
        )
        print(f"{item_count} items across {len(targets)} meetings "
              f"({matter_count} linked matters) below {args.below!r}"
              + (f" in {args.banana}" if args.banana else "")
              + (f" with attachments matching {args.attachments_matching!r}"
                 if args.attachments_matching else ""))
        for t in targets[:10]:
            print(f"  {t['banana']:<30} {str(t['date'])[:10]:<12} {len(t['item_ids'])} items")
        if len(targets) > 10:
            print(f"  ... and {len(targets) - 10} more meetings")

        if args.dry_run or not targets:
            return
        if not args.yes:
            print("\nre-run with --yes to unfreeze these summaries and re-enqueue")
            return

        unfrozen, enqueued, matters = await apply(db, targets, args.below)
        print(f"\nunfroze {unfrozen} item summaries, re-enqueued {enqueued} meetings "
              f"and {matters} matters at priority {BACKFILL_PRIORITY} "
              f"(past dates take the batch lane)")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
