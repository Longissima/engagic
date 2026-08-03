"""Re-summarize corpus-covered transactional items under the v3.1 prompt.

Targets items whose attachments carry transactional documents (quote,
contract, agreement, proposal, exhibit) summarized under pre-v3.1 prompts,
where every attachment's extracted text is present in the ground-truth corpus.
Input text is rebuilt from the corpus exactly as the pipeline would build it
(=== name === sections, budget-fitted), submitted through the production
Gemini Batch API path (v3.1 prompt, token-aware chunking), then ingested via
the production repositories (items + normalized topics + matter canonical).

Scope is deliberately corpus-only: items whose documents predate the corpus
cannot be faithfully reconstructed (expired vendor URLs) and are left alone.

Usage:
    uv run scripts/backfill_v31_summaries.py --since-days 30 [--limit N] [--dry-run]

State persists to data/backfill_v31_state.jsonl; re-running resumes: already
submitted items are not resubmitted, already ingested items are not rewritten.
Confidence: 8/10 on canonical-update ordering (items processed in meeting-date
order so the latest appearance writes canonical last).
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
from pipeline.processor import MAX_ITEM_INPUT_CHARS, fit_parts_to_budget

logger = get_logger(__name__).bind(component="backfill_v31")

STATE_PATH = Path("/opt/engagic/data/backfill_v31_state.jsonl")
WAVE_SIZE = 200
R2_FETCH_CONCURRENCY = 12
POLL_INTERVAL_SECONDS = 120
TRANSACTIONAL_NAME_RE = '"name": *"[^"]*(quote|contract|agreement|proposal|exhibit)[^"]*"'
ALLOWED_TYPES = ("pdf", "doc", "document", "unknown")

SELECT_TARGETS = f"""
WITH cand AS (
    SELECT i.id, i.title, i.sequence, i.matter_id, i.attachments, m.date AS mdate
    FROM items i JOIN meetings m ON i.meeting_id = m.id
    WHERE i.summary IS NOT NULL
      AND i.prompts_version IS DISTINCT FROM 'v3.1'
      AND m.date >= now() - ($1 || ' days')::interval
      AND i.attachments::text ~* $2
)
SELECT c.id, c.title, c.sequence, c.matter_id, c.attachments, c.mdate
FROM cand c
JOIN LATERAL jsonb_array_elements(c.attachments::jsonb) att ON att->>'url' IS NOT NULL
LEFT JOIN document_source ds
       ON split_part(ds.source_identity, '?', 1) = split_part(att->>'url', '?', 1)
GROUP BY c.id, c.title, c.sequence, c.matter_id, c.attachments, c.mdate
HAVING count(*) = count(ds.content_sha256)
ORDER BY c.mdate ASC
"""

CORPUS_TEXT_FOR_URL = """
SELECT b.text_key, b.page_count
FROM document_source s
JOIN document_blob b ON s.content_sha256 = b.content_sha256
WHERE split_part(s.source_identity, '?', 1) = split_part($1, '?', 1)
  AND b.text_key IS NOT NULL
ORDER BY s.first_seen DESC
LIMIT 1
"""


def load_state() -> tuple[set, set, list]:
    """Return (submitted_item_ids, ingested_item_ids, open_jobs)."""
    submitted, ingested, jobs = set(), set(), []
    if not STATE_PATH.exists():
        return submitted, ingested, jobs
    with open(STATE_PATH) as f:
        for line in f:
            rec = json.loads(line)
            if rec["kind"] == "submitted":
                submitted.update(rec["item_ids"])
                jobs.append(rec)
            elif rec["kind"] == "ingested":
                ingested.add(rec["item_id"])
            elif rec["kind"] == "job_done":
                jobs = [j for j in jobs if j["gemini_job_name"] != rec["gemini_job_name"]]
    return submitted, ingested, jobs


def append_state(rec: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")


async def build_item_text(pool, r2, attachments: list) -> tuple:
    """Rebuild combined_text from corpus. Returns (text, page_count) or (None, 0)."""
    parts = []
    pages = 0
    async with pool.acquire() as conn:
        for att in attachments:
            if att.get("type") not in ALLOWED_TYPES or not att.get("url"):
                continue
            row = await conn.fetchrow(CORPUS_TEXT_FOR_URL, att["url"])
            if not row:
                return None, 0
            blob = await r2.get(row["text_key"])
            if not blob:
                return None, 0
            parts.append((att.get("name") or att["url"], blob.decode("utf-8", errors="replace")))
            pages += row["page_count"] or 0

    if not parts:
        return None, 0
    parts, trim_notes = fit_parts_to_budget(parts, MAX_ITEM_INPUT_CHARS)
    sections = [f"=== {name} ===\n{text}" for name, text in parts]
    if trim_notes:
        sections.append(
            "[PIPELINE NOTE: input trimmed to fit the model context window -- "
            + "; ".join(trim_notes) + "]"
        )
    return "\n\n".join(sections), pages


async def ingest_result(db, meta: dict, result: dict, normalizer) -> bool:
    """Write one successful batch result through production semantics."""
    if not result.get("success") or not result.get("summary"):
        return False
    item_id = result["item_id"]
    topics = normalizer.normalize(result.get("topics", []))
    await db.items.update_agenda_item(
        item_id=item_id,
        summary=result["summary"],
        topics=topics,
        prompts_version="v3.1",
    )
    matter_id = meta.get("matter_id")
    if matter_id:
        # Mirror MattersRepository.update_matter_summary minus the attachment
        # hash: backfill must not perturb change-detection state.
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.execute(
                    """
                    UPDATE city_matters
                    SET canonical_summary = $2, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    matter_id,
                    result["summary"],
                )
                if updated != "UPDATE 0" and topics:
                    await replace_entity_topics(
                        conn, "matter_topics", "matter_id", matter_id, topics
                    )
    return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0, help="cap item count (0 = no cap)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db = await Database.create()
    r2 = R2Client(
        account_id=config.CLOUDFLARE_ACCOUNT_ID,
        access_key_id=config.R2_ACCESS_KEY_ID,
        secret_access_key=config.R2_SECRET_ACCESS_KEY,
        bucket=config.CORPUS_BUCKET,
    )
    summarizer = GeminiSummarizer()
    normalizer = get_normalizer()
    assert summarizer.prompts_version == "v3.1", "backfill requires the v3.1 prompt"

    submitted_ids, ingested_ids, open_jobs = load_state()
    logger.info(
        "backfill starting",
        since_days=args.since_days,
        resumed_submitted=len(submitted_ids),
        resumed_ingested=len(ingested_ids),
        resumed_open_jobs=len(open_jobs),
    )

    async with db.pool.acquire() as conn:
        rows = await conn.fetch(SELECT_TARGETS, str(args.since_days), TRANSACTIONAL_NAME_RE)
    targets = [r for r in rows if r["id"] not in submitted_ids]
    if args.limit:
        targets = targets[: args.limit]
    logger.info("targets selected", total_candidates=len(rows), to_submit=len(targets))

    if args.dry_run:
        for r in targets[:20]:
            print(r["mdate"], r["id"], "|", r["title"][:80])
        print(f"dry run: {len(targets)} items would be submitted")
        await r2.close()
        await db.close()
        return

    # Item metadata needed at ingest time, keyed by item_id. Persisted in the
    # submitted state records so a resumed run can still ingest.
    meta_by_id = {}
    for job in open_jobs:
        meta_by_id.update(job.get("meta", {}))

    # Submit in waves so text buffers stay bounded.
    sem = asyncio.Semaphore(R2_FETCH_CONCURRENCY)
    skipped_no_text = 0
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
        logger.info(
            "wave submitted",
            wave=wave_start // WAVE_SIZE + 1,
            items=len(requests),
            jobs=len(descriptors),
            skipped_no_text=skipped_no_text,
        )

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
                for result in results:
                    item_id = result.get("item_id")
                    if not item_id or item_id in ingested_ids:
                        continue
                    ok = await ingest_result(
                        db, meta_by_id.get(item_id, {}), result, normalizer
                    )
                    if ok:
                        ingested += 1
                        ingested_ids.add(item_id)
                        append_state({"kind": "ingested", "item_id": item_id})
                    else:
                        failed += 1
            else:
                failed += len(job["item_ids"])
                logger.warning("job terminal without results", job=job["gemini_job_name"])
            append_state({"kind": "job_done", "gemini_job_name": job["gemini_job_name"]})
        open_jobs = still_open
        logger.info("collect cycle", open_jobs=len(open_jobs), ingested=ingested, failed=failed)
        if open_jobs:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    logger.info(
        "backfill complete",
        ingested=ingested,
        failed=failed,
        skipped_no_text=skipped_no_text,
    )
    await r2.close()
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
