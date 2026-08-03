"""Re-summarize corpus-covered transactional items under the v3.1 prompt.

Targets items whose attachments carry transactional documents (quote,
contract, agreement, proposal, exhibit, purchase order, pricing, or SOW)
summarized under pre-v3.1 prompts, where every production-eligible attachment's
extracted text is present in the ground-truth corpus.
Input text is rebuilt from the corpus using the pipeline's document selection,
section, and budget conventions, submitted through the production Gemini Batch
API path (v3.1 prompt, token-aware chunking), then ingested via the production
repositories (items + normalized topics + matter canonical).

Scope is deliberately corpus-only: items whose documents predate the corpus
cannot be faithfully reconstructed (expired vendor URLs) and are left alone.

Usage:
    uv run scripts/backfill_v31_summaries.py --since-days 30 [--limit N] [--dry-run]

State persists to data/backfill_v31_state.jsonl; re-running resumes open jobs,
does not rewrite ingested items, and makes terminal failures eligible for a
later retry. Canonical matters are refreshed after collection from their newest
summarized appearance, independent of Gemini job completion order.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.llm.summarizer import GeminiSummarizer
from analysis.topics.normalizer import get_normalizer
from config import config, get_logger
from corpus.r2 import R2Client
from database.db_postgres import Database
from database.repositories_async.helpers import replace_entity_topics
from analysis.llm.input_budget import (
    DOCUMENT_ATTACHMENT_TYPES,
    MAX_ITEM_INPUT_CHARS,
    PUBLIC_COMMENT_EXCERPT_CHARS,
    render_document_parts,
)
from pipeline.filters import is_public_comment_attachment
from pipeline.utils import attachment_identity, filter_document_version_urls

logger = get_logger(__name__).bind(component="backfill_v31")

STATE_PATH = Path("/opt/engagic/data/backfill_v31_state.jsonl")
WAVE_SIZE = 200
R2_FETCH_CONCURRENCY = 12
POLL_INTERVAL_SECONDS = 120
TRANSACTIONAL_NAME_RE = (
    '"name": *"[^"]*(quote|contract|agreement|proposal|exhibit|purchase order|'
    'order form|pricing|bid tab|statement of work|sow)[^"]*"'
)

SELECT_TARGETS = """
SELECT i.id, i.title, i.sequence, i.matter_id, i.attachments, m.date AS mdate
FROM items i
JOIN meetings m ON i.meeting_id = m.id
WHERE i.summary IS NOT NULL
  AND i.prompts_version IS DISTINCT FROM 'v3.2'
  AND m.date >= now() - ($1 || ' days')::interval
  AND i.attachments::text ~* $2
ORDER BY m.date ASC, i.sequence ASC, i.id ASC
"""

CORPUS_TEXT_FOR_IDENTITY = """
SELECT b.text_key, b.page_count
FROM document_source s
JOIN document_blob b ON s.content_sha256 = b.content_sha256
WHERE s.source_identity = $1
  AND b.text_key IS NOT NULL
ORDER BY s.first_seen DESC
LIMIT 1
"""

LATEST_MATTER_ITEM = """
SELECT i.id, i.summary
FROM items i
JOIN meetings m ON i.meeting_id = m.id
WHERE i.matter_id = $1
  AND i.summary IS NOT NULL
ORDER BY m.date DESC NULLS LAST, i.sequence DESC, i.id DESC
LIMIT 1
"""


def load_state() -> tuple[set, set, list, dict]:
    """Return submitted IDs, ingested IDs, open jobs, and all item metadata."""
    submitted, ingested, jobs, meta_by_id = set(), set(), [], {}
    if not STATE_PATH.exists():
        return submitted, ingested, jobs, meta_by_id
    with open(STATE_PATH) as f:
        for line in f:
            rec = json.loads(line)
            if rec["kind"] == "submitted":
                submitted.update(rec["item_ids"])
                meta_by_id.update(rec.get("meta", {}))
                jobs.append(rec)
            elif rec["kind"] == "ingested":
                ingested.add(rec["item_id"])
            elif rec["kind"] == "job_done":
                jobs = [j for j in jobs if j["gemini_job_name"] != rec["gemini_job_name"]]
    return submitted, ingested, jobs, meta_by_id


def append_state(rec: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


async def build_item_text(pool, r2, attachments: list) -> tuple:
    """Rebuild combined_text from corpus. Returns (text, page_count) or (None, 0)."""
    parts = []
    pages = 0
    eligible = [
        att
        for att in attachments
        if att.get("type") in DOCUMENT_ATTACHMENT_TYPES and att.get("url")
    ]
    selected_urls = set(
        filter_document_version_urls([att["url"] for att in eligible])
    )
    eligible = [att for att in eligible if att["url"] in selected_urls]

    async with pool.acquire() as conn:
        for att in eligible:
            identity = attachment_identity(att["url"])
            row = await conn.fetchrow(CORPUS_TEXT_FOR_IDENTITY, identity)
            if not row:
                return None, 0
            blob = await r2.get(row["text_key"])
            if not blob:
                return None, 0
            name = att.get("name") or att["url"]
            text = blob.decode("utf-8", errors="replace")
            if is_public_comment_attachment(name) and len(text) > PUBLIC_COMMENT_EXCERPT_CHARS:
                text = (
                    text[:PUBLIC_COMMENT_EXCERPT_CHARS]
                    + "\n\n[PIPELINE NOTE: this attachment appears to be a public-comment"
                    + f" document ({row['page_count'] or 0} pages, {len(text):,} characters);"
                    + " only the excerpt above is included]"
                )
            parts.append((name, text))
            pages += row["page_count"] or 0

    if not parts:
        return None, 0
    text, trim_notes = render_document_parts(parts, MAX_ITEM_INPUT_CHARS)
    if trim_notes:
        logger.warning("backfill item input trimmed", notes=trim_notes)
    return text, pages


async def ingest_result(db, result: dict, normalizer) -> bool:
    """Write one successful item result; canonical matters refresh afterward."""
    if not result.get("success") or not result.get("summary"):
        return False
    item_id = result["item_id"]
    topics = normalizer.normalize(result.get("topics", []))
    await db.items.update_agenda_item(
        item_id=item_id,
        summary=result["summary"],
        topics=topics,
        prompts_version="v3.2",
    )
    return True


async def refresh_canonical_summaries(db, matter_ids: set[str]) -> int:
    """Refresh affected matters from their newest summarized appearance."""
    refreshed = 0
    for matter_id in sorted(matter_ids):
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                latest = await conn.fetchrow(LATEST_MATTER_ITEM, matter_id)
                if not latest:
                    continue
                topic_rows = await conn.fetch(
                    "SELECT topic FROM item_topics WHERE item_id = $1 ORDER BY topic",
                    latest["id"],
                )
                topics = [row["topic"] for row in topic_rows]
                updated = await conn.execute(
                    """
                    UPDATE city_matters
                    SET canonical_summary = $2,
                        canonical_topics = $3,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    matter_id,
                    latest["summary"],
                    topics,
                )
                if updated != "UPDATE 0":
                    await replace_entity_topics(
                        conn, "matter_topics", "matter_id", matter_id, topics
                    )
                    refreshed += 1
    return refreshed


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0, help="cap item count (0 = no cap)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = await Database.create()
    submitted_ids, ingested_ids, open_jobs, meta_by_id = load_state()
    open_item_ids = {
        item_id for job in open_jobs for item_id in job.get("item_ids", [])
    }
    logger.info(
        "backfill starting",
        since_days=args.since_days,
        resumed_submitted=len(submitted_ids),
        resumed_ingested=len(ingested_ids),
        resumed_open_jobs=len(open_jobs),
    )

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(SELECT_TARGETS, str(args.since_days), TRANSACTIONAL_NAME_RE)
    # Completed failures are eligible on the next run. Only an already ingested
    # item or one with a currently open Gemini job is excluded.
    targets = [
        r
        for r in rows
        if r["id"] not in ingested_ids and r["id"] not in open_item_ids
    ]
    logger.info("targets selected", total_candidates=len(rows), to_submit=len(targets))

    if args.dry_run:
        dry_count = min(len(targets), args.limit) if args.limit else len(targets)
        for r in targets[: min(20, dry_count)]:
            print(r["mdate"], r["id"], "|", r["title"][:80])
        print(
            f"dry run: {dry_count} candidate items; exact corpus coverage "
            "is checked before submission"
        )
        await db.close()
        return

    r2 = R2Client(
        account_id=config.CLOUDFLARE_ACCOUNT_ID,
        access_key_id=config.R2_ACCESS_KEY_ID,
        secret_access_key=config.R2_SECRET_ACCESS_KEY,
        bucket=config.CORPUS_BUCKET,
    )
    summarizer = GeminiSummarizer()
    normalizer = get_normalizer()
    assert summarizer.prompts_version == "v3.2", "backfill requires the v3.2 prompt"

    # Submit in waves so text buffers stay bounded.
    sem = asyncio.Semaphore(R2_FETCH_CONCURRENCY)
    skipped_no_text = 0
    submitted_this_run = 0
    for wave_start in range(0, len(targets), WAVE_SIZE):
        wave = targets[wave_start : wave_start + WAVE_SIZE]

        async def fetch_one(row):
            async with sem:
                # Pool jsonb codec may deliver attachments already decoded
                raw = row["attachments"]
                attachments = raw if isinstance(raw, list) else json.loads(raw or "[]")
                text, pages = await build_item_text(db.pool, r2, attachments)
                return row, text, pages

        fetched = await asyncio.gather(*[fetch_one(r) for r in wave])
        requests = []
        wave_meta = {}
        for row, text, pages in fetched:
            if not text:
                skipped_no_text += 1
                continue
            requests.append({
                "item_id": row["id"],
                "title": row["title"],
                "text": text,
                "sequence": row["sequence"],
                "page_count": pages or None,
            })
            wave_meta[row["id"]] = {"matter_id": row["matter_id"]}

        if not requests:
            continue
        if args.limit:
            remaining = args.limit - submitted_this_run
            if remaining <= 0:
                break
            requests = requests[:remaining]
        descriptors = await summarizer.submit_item_batches(requests)
        for d in descriptors:
            rec = {
                "kind": "submitted",
                "gemini_job_name": d["gemini_job_name"],
                "item_ids": d["item_ids"],
                "meta": {iid: wave_meta.get(iid, {}) for iid in d["item_ids"]},
            }
            append_state(rec)
            open_jobs.append(rec)
            meta_by_id.update(rec["meta"])
        submitted_this_run += sum(len(d["item_ids"]) for d in descriptors)
        logger.info(
            "wave submitted",
            wave=wave_start // WAVE_SIZE + 1,
            items=len(requests),
            jobs=len(descriptors),
            skipped_no_text=skipped_no_text,
        )
        if args.limit and submitted_this_run >= args.limit:
            break

    # Collect until every job is terminal.
    ingested = failed = 0
    while open_jobs:
        still_open = []
        for job in open_jobs:
            state, results = await summarizer.collect_item_batch(
                job["gemini_job_name"], job["item_ids"]
            )
            if state == "running":
                still_open.append(job)
                continue
            if state == "succeeded" and results:
                returned_ids = set()
                for result in results:
                    item_id = result.get("item_id")
                    if not item_id or item_id in ingested_ids:
                        continue
                    returned_ids.add(item_id)
                    ok = await ingest_result(db, result, normalizer)
                    if ok:
                        ingested += 1
                        ingested_ids.add(item_id)
                        append_state({"kind": "ingested", "item_id": item_id})
                    else:
                        failed += 1
                missing_ids = set(job["item_ids"]) - returned_ids - ingested_ids
                if missing_ids:
                    failed += len(missing_ids)
                    logger.warning(
                        "batch response omitted items",
                        job=job["gemini_job_name"],
                        item_ids=sorted(missing_ids),
                    )
            else:
                failed += len(job["item_ids"])
                logger.warning("job terminal without results", job=job["gemini_job_name"])
            append_state({"kind": "job_done", "gemini_job_name": job["gemini_job_name"]})
        open_jobs = still_open
        logger.info("collect cycle", open_jobs=len(open_jobs), ingested=ingested, failed=failed)
        if open_jobs:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    affected_matter_ids = {
        meta_by_id[item_id]["matter_id"]
        for item_id in ingested_ids
        if meta_by_id.get(item_id, {}).get("matter_id")
    }
    canonicals_refreshed = await refresh_canonical_summaries(
        db,
        affected_matter_ids,
    )

    logger.info(
        "backfill complete",
        ingested=ingested,
        failed=failed,
        skipped_no_text=skipped_no_text,
        canonicals_refreshed=canonicals_refreshed,
    )
    await r2.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
