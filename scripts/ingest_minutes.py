"""Pull minutes documents into the ground-truth corpus (R2 originals + text).

Rides the analyzer's existing extraction path -- download, sha256, archive
original, extract text, persist -- so minutes bytes enter the same
content-addressed corpus as agendas and packets. The extraction-only analyzer
does not construct an LLM client or require a Gemini key.

Incomplete corpus entries are retried, and completed URL identities are
periodically revalidated because vendors commonly replace a draft with
approved minutes at the same stable URL.

This is the substrate step for the roll-call track (spygov docs/MODEL_DOCTRINE.md):
the parser reads corpus text, never live vendor URLs.

Usage:
    uv run python scripts/ingest_minutes.py --dry-run
    uv run python scripts/ingest_minutes.py --days-back 120 --limit 500
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.analyzer_async import AsyncAnalyzer
from config import get_logger
from corpus.store import EXTRACT_VERSION, get_corpus
from database.db_postgres import Database
from exceptions import ExtractionError
from pipeline.utils import attachment_identity

logger = get_logger(__name__).bind(component="ingest_minutes")


CANDIDATES_SQL = """
    SELECT id, banana, minutes_url
    FROM meetings
    WHERE minutes_url IS NOT NULL
      AND date >= CURRENT_TIMESTAMP - make_interval(days => $1)
      AND ($2::text IS NULL OR banana = $2)
    ORDER BY date DESC
"""

SOURCE_STATE_SQL = """
    SELECT DISTINCT ON (s.source_identity)
        s.source_identity,
        (
            b.original_key IS NOT NULL
            AND b.text_key IS NOT NULL
            AND b.extract_version = $2
        ) AS corpus_ready,
        s.last_seen <= CURRENT_TIMESTAMP - make_interval(days => $3) AS recheck_due
    FROM document_source s
    JOIN document_blob b USING (content_sha256)
    WHERE s.source_identity = ANY($1::text[])
    ORDER BY s.source_identity, s.last_seen DESC, s.first_seen DESC
"""

CORPUS_READY_SQL = """
    SELECT EXISTS (
        SELECT 1
        FROM document_blob
        WHERE content_sha256 = $1
          AND original_key IS NOT NULL
          AND text_key IS NOT NULL
          AND extract_version = $2
    )
"""


Candidate = Tuple[Any, str, str]


def select_candidates(
    rows: Iterable[Any],
    states: Dict[str, Dict[str, Any]],
    limit: int,
) -> Tuple[List[Candidate], Dict[str, int]]:
    """Choose unique URL identities that need first ingest, repair, or recheck."""
    selected: List[Candidate] = []
    counts = {"new": 0, "incomplete": 0, "revision_recheck": 0, "current": 0}
    considered = set()

    for row in rows:
        identity = attachment_identity(row["minutes_url"])
        if identity in considered:
            continue
        considered.add(identity)

        state = states.get(identity)
        if state is None:
            reason = "new"
        elif not state["corpus_ready"]:
            reason = "incomplete"
        elif state["recheck_due"]:
            reason = "revision_recheck"
        else:
            counts["current"] += 1
            continue

        counts[reason] += 1
        if len(selected) < limit:
            selected.append((row, identity, reason))

    return selected, counts


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=120,
                        help="meeting-date window to consider (default 120)")
    parser.add_argument("--limit", type=int, default=500,
                        help="max documents to ingest or recheck this run (default 500)")
    parser.add_argument("--recheck-days", type=int, default=7,
                        help="re-fetch completed stable URLs after N days (default 7)")
    parser.add_argument("--banana", default=None, help="restrict to one city")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.limit < 1 or args.recheck_days < 1 or args.concurrency < 1:
        parser.error("--limit, --recheck-days, and --concurrency must be positive")

    db = await Database.create()
    analyzer = None
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(CANDIDATES_SQL, args.days_back, args.banana)

        identities = list({attachment_identity(r["minutes_url"]) for r in rows})
        state_rows = []
        if identities:
            async with db.pool.acquire() as conn:
                state_rows = await conn.fetch(
                    SOURCE_STATE_SQL,
                    identities,
                    EXTRACT_VERSION,
                    args.recheck_days,
                )
        states = {r["source_identity"]: dict(r) for r in state_rows}
        todo, candidate_counts = select_candidates(rows, states, args.limit)
        due = sum(candidate_counts[k] for k in ("new", "incomplete", "revision_recheck"))
        logger.info(
            "ingest starting",
            candidates=len(rows),
            unique_identities=len(set(identities)),
            due=due,
            this_run=len(todo),
            recheck_days=args.recheck_days,
            dry_run=args.dry_run,
            **candidate_counts,
        )

        if args.dry_run:
            for row, _, reason in todo[:20]:
                print(f"would ingest  {row['banana']:<20} {reason:<17} {row['minutes_url'][:90]}")
            print(
                f"ingest_minutes (dry-run): {due} due in window "
                f"({candidate_counts}), {len(todo)} this run at --limit {args.limit}"
            )
            return 0

        if get_corpus() is None:
            logger.error("minutes ingest requires an enabled, configured corpus")
            print("ingest_minutes: corpus unavailable; no documents were processed")
            return 2

        analyzer = AsyncAnalyzer(enable_llm=False)

        sem = asyncio.Semaphore(args.concurrency)
        counts = {"ingested": 0, "failed_extract": 0, "failed_persist": 0}

        async def ingest_one(candidate: Candidate):
            row, identity, reason = candidate
            async with sem:
                try:
                    result = await analyzer.extract_pdf_async(
                        row["minutes_url"], banana=row["banana"]
                    )
                    content_sha256 = result.get("content_sha256")
                    ready = False
                    if content_sha256 and result.get("corpus_persisted"):
                        async with db.pool.acquire() as conn:
                            ready = await conn.fetchval(
                                CORPUS_READY_SQL, content_sha256, EXTRACT_VERSION
                            )
                    if not ready:
                        counts["failed_persist"] += 1
                        logger.warning(
                            "minutes extraction was not durably persisted",
                            banana=row["banana"],
                            meeting_id=row["id"],
                            source_identity=identity[:110],
                            content_sha256=(content_sha256 or "")[:16],
                        )
                        return
                    counts["ingested"] += 1
                    logger.info(
                        "minutes ingested",
                        banana=row["banana"],
                        meeting_id=row["id"],
                        reason=reason,
                        chars=len(result.get("text") or ""),
                    )
                except ExtractionError as e:
                    counts["failed_extract"] += 1
                    logger.warning(
                        "minutes download or extraction failed (will retry)",
                        banana=row["banana"],
                        url=row["minutes_url"][:110],
                        error=str(e)[:200],
                    )
                except Exception as e:
                    counts["failed_persist"] += 1
                    logger.warning(
                        "minutes ingest failed (will retry)",
                        banana=row["banana"],
                        url=row["minutes_url"][:110],
                        error=str(e)[:200],
                    )

        await asyncio.gather(*(ingest_one(candidate) for candidate in todo))

        logger.info("ingest complete", **counts)
        print(f"ingest_minutes: {counts}")
        return 0
    finally:
        if analyzer is not None:
            await analyzer.close()
        await db.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
