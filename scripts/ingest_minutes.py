"""Pull minutes documents into the ground-truth corpus (R2 originals + text).

Rides the analyzer's existing extraction path -- download, sha256, archive
original, extract text, persist -- so minutes bytes enter the same
content-addressed corpus as agendas and packets. The extraction-only analyzer
does not construct an LLM client or require a Gemini key.

Incomplete corpus entries are retried, and completed URL identities are
periodically revalidated because vendors commonly replace a draft with
approved minutes at the same stable URL. Repeated download/extraction failures
back off and are suppressed after a bounded number of attempts for the current
extractor version; known HTML-only minutes viewers are excluded up front.

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
from exceptions import DocumentDownloadError, ExtractionError
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
        s.content_sha256,
        (
            b.original_key IS NOT NULL
            AND b.text_key IS NOT NULL
            AND b.extract_version = ANY($2::text[])
        ) AS corpus_ready,
        (COALESCE(cardinality(b.ocr_pending_pages), 0) > 0
         OR COALESCE(b.extract_method, '') LIKE '%-partial') AS extraction_incomplete,
        COALESCE(s.last_validated_at, s.first_seen) <= CURRENT_TIMESTAMP - make_interval(days => $3) AS recheck_due
    FROM document_source s
    JOIN document_blob b USING (content_sha256)
    WHERE s.source_identity = ANY($1::text[])
    ORDER BY s.source_identity, s.last_validated_at DESC NULLS LAST, s.first_seen DESC
"""

# Durable meeting -> minutes text link (migration 040). One row per revision;
# the newest ingested_at is what the roll-call parser reads.
LINK_SQL = """
    INSERT INTO minutes_documents (meeting_id, content_sha256, source_identity)
    VALUES ($1, $2, $3)
    ON CONFLICT (meeting_id, content_sha256) DO UPDATE
        SET ingested_at = CURRENT_TIMESTAMP, source_identity = EXCLUDED.source_identity
"""

RELINK_SQL = """
    INSERT INTO minutes_documents (meeting_id, content_sha256, source_identity)
    VALUES ($1, $2, $3)
    ON CONFLICT (meeting_id, content_sha256) DO NOTHING
"""

# Extractor versions whose text the corpus serves as-is (corpus.store keeps
# the same set); a blob at either is ready.
COMPATIBLE_EXTRACT_VERSIONS = ["1", EXTRACT_VERSION]

CORPUS_READY_SQL = """
    SELECT EXISTS (
        SELECT 1
        FROM document_blob
        WHERE content_sha256 = $1
          AND original_key IS NOT NULL
          AND text_key IS NOT NULL
          AND extract_version = ANY($2::text[])
    )
"""

FAILURE_STATE_SQL = """
    SELECT source_identity, attempt_count, permanent,
           retry_after <= CURRENT_TIMESTAMP AS retry_due
    FROM document_ingest_failure
    WHERE source_identity = ANY($1::text[])
      AND extract_version = $2
"""

RECORD_FAILURE_SQL = """
    INSERT INTO document_ingest_failure AS current_failure (
        source_identity, extract_version, banana, failure_stage,
        attempt_count, permanent, last_error, retry_after
    )
    VALUES (
        $1, $2, $3, $4, 1, ($6::boolean OR 1 >= $7), $5,
        CURRENT_TIMESTAMP + make_interval(days => $8)
    )
    ON CONFLICT (source_identity, extract_version) DO UPDATE SET
        banana = COALESCE(current_failure.banana, EXCLUDED.banana),
        failure_stage = EXCLUDED.failure_stage,
        attempt_count = current_failure.attempt_count + 1,
        permanent = (
            current_failure.permanent
            OR $6::boolean
            OR current_failure.attempt_count + 1 >= $7
        ),
        last_error = EXCLUDED.last_error,
        last_failed_at = CURRENT_TIMESTAMP,
        retry_after = CURRENT_TIMESTAMP + make_interval(days => $8)
    RETURNING attempt_count, permanent
"""

CLEAR_FAILURE_SQL = """
    DELETE FROM document_ingest_failure
    WHERE source_identity = $1 AND extract_version = $2
"""

UNBOUNDED_FAILURE_ATTEMPTS = 2_147_483_647


Candidate = Tuple[Any, str, str]


def select_candidates(
    rows: Iterable[Any],
    states: Dict[str, Dict[str, Any]],
    limit: int,
    failure_states: Dict[str, Dict[str, Any]] | None = None,
) -> Tuple[List[Candidate], Dict[str, int]]:
    """Choose unique URL identities that need first ingest, repair, or recheck."""
    selected: List[Candidate] = []
    counts = {
        "new": 0,
        "incomplete": 0,
        "revision_recheck": 0,
        "current": 0,
        "failure_backoff": 0,
        "permanent_failure": 0,
        "unsupported_url": 0,
    }
    considered = set()
    failure_states = failure_states or {}

    for row in rows:
        identity = attachment_identity(row["minutes_url"])
        if identity in considered:
            continue
        considered.add(identity)

        if unsupported_minutes_url_reason(row["minutes_url"]):
            counts["unsupported_url"] += 1
            continue

        state = states.get(identity)
        failure = failure_states.get(identity)
        if failure:
            # Older ingestion marked partial OCR permanent. The available text
            # proves this is repairable extraction work; keep its retry delay.
            if failure["permanent"] and not (state and state.get("extraction_incomplete")):
                counts["permanent_failure"] += 1
                continue
            if not failure["retry_due"]:
                counts["failure_backoff"] += 1
                continue

        if state is None:
            reason = "new"
        elif not state["corpus_ready"] or state.get("extraction_incomplete"):
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


def unsupported_minutes_url_reason(url: str) -> str | None:
    """Known HTML viewers that the PDF extraction path cannot ingest."""
    lowered = (url or "").lower()
    if "meetings.boardbook.org/public/minutes/" in lowered:
        return "boardbook_minutes_viewer"
    if "novusagenda.com" in lowered and "meetingview.aspx" in lowered and "doctype=minutes" in lowered:
        return "novusagenda_minutes_viewer"
    return None


def is_transient_failure(error: Exception) -> bool:
    """Failures caused by the host, not the document: retry soon, never cap."""
    if isinstance(error, DocumentDownloadError) and error.is_retryable:
        return True
    # The memory admission gate refuses extraction when the sync and other
    # backfills hold the budget; the document itself is fine.
    return isinstance(error, ExtractionError) and "memory capacity" in str(error)


def failure_attempt_limit(error: Exception, configured_max: int) -> int:
    """Return the retry cap for a classified ingestion failure."""
    if is_transient_failure(error):
        return UNBOUNDED_FAILURE_ATTEMPTS
    return configured_max


# A minutes URL that resolves to an HTML page with less text than this is a
# viewer shell (Granicus MinutesViewer wrapping a Google Docs embed, a portal
# tab), not the minutes. Persisting the shell as "ingested" would hand the
# roll-call parser an empty document and never retry the URL.
MIN_HTML_MINUTES_CHARS = 500


def failure_error_text(error: Exception, source_url: str) -> str:
    """Format a ledger-safe error without volatile URL credentials."""
    safe_url = attachment_identity(source_url)
    message = str(error)
    if source_url and source_url != safe_url:
        message = message.replace(source_url, safe_url)
    return f"{type(error).__name__}: {message}"[:1000]


async def record_failure(
    db,
    *,
    identity: str,
    source_url: str,
    banana: str,
    stage: str,
    error: Exception,
    max_failures: int,
    retry_days: int,
    permanent: bool = False,
) -> Dict[str, Any] | None:
    """Persist bounded retry state without hiding the original job failure."""
    try:
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow(
                RECORD_FAILURE_SQL,
                identity,
                EXTRACT_VERSION,
                banana,
                stage,
                failure_error_text(error, source_url),
                permanent,
                max_failures,
                retry_days,
            )
        return dict(row) if row else None
    except Exception as ledger_error:
        logger.warning(
            "could not persist document failure state",
            source_identity=identity[:110],
            error=str(ledger_error)[:200],
        )
        return None


async def clear_failure(db, identity: str) -> None:
    try:
        async with db.pool.acquire() as conn:
            await conn.execute(CLEAR_FAILURE_SQL, identity, EXTRACT_VERSION)
    except Exception as e:
        logger.warning(
            "could not clear document failure state",
            source_identity=identity[:110],
            error=str(e)[:200],
        )


async def record_ingest_result(db, result, identity, source_url, banana):
    """Keep readable partial text while scheduling another OCR attempt."""
    if result.get("extraction_incomplete"):
        await record_failure(db, identity=identity, source_url=source_url,
            banana=banana, stage="extract",
            error=ExtractionError("OCR pages still pending", document_url=identity),
            max_failures=UNBOUNDED_FAILURE_ATTEMPTS, retry_days=1)
    else:
        await clear_failure(db, identity)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days-back", type=int, default=120,
                        help="meeting-date window to consider (default 120)")
    parser.add_argument("--limit", type=int, default=500,
                        help="max documents to ingest or recheck this run (default 500)")
    parser.add_argument("--recheck-days", type=int, default=7,
                        help="re-fetch completed stable URLs after N days (default 7)")
    parser.add_argument("--max-failures", type=int, default=3,
                        help="suppress an identity after N failures for this extractor (default 3)")
    parser.add_argument("--failure-retry-days", type=int, default=7,
                        help="days between retries of failed identities (default 7)")
    parser.add_argument("--banana", default=None, help="restrict to one city")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if any(value < 1 for value in (
        args.limit,
        args.recheck_days,
        args.concurrency,
        args.max_failures,
        args.failure_retry_days,
    )):
        parser.error("limit, retry windows, max failures, and concurrency must be positive")

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
                    COMPATIBLE_EXTRACT_VERSIONS,
                    args.recheck_days,
                )
        states = {r["source_identity"]: dict(r) for r in state_rows}
        # Documents the corpus already holds (any compatible extractor
        # version) are linked to their meetings without a refetch; the link
        # is what the roll-call parser reads, and a first pass that judged
        # the older version "not ready" left 177 of them unlinked.
        relinked = 0
        async with db.pool.acquire() as conn:
            for r in ([] if args.dry_run else rows):
                state = states.get(attachment_identity(r["minutes_url"]))
                if state and state["corpus_ready"] and state.get("content_sha256"):
                    status = await conn.execute(
                        RELINK_SQL, r["id"], state["content_sha256"], attachment_identity(r["minutes_url"])
                    )
                    relinked += status == "INSERT 0 1"
        if relinked:
            logger.info("linked already-ingested minutes", meetings=relinked)
        failure_rows = []
        if identities:
            async with db.pool.acquire() as conn:
                failure_rows = await conn.fetch(
                    FAILURE_STATE_SQL, identities, EXTRACT_VERSION
                )
        failure_states = {r["source_identity"]: dict(r) for r in failure_rows}
        todo, candidate_counts = select_candidates(
            rows, states, args.limit, failure_states
        )
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
        counts = {
            "ingested": 0,
            "failed_download": 0,
            "failed_extract": 0,
            "failed_persist": 0,
            "failed_other": 0,
        }

        async def ingest_one(candidate: Candidate):
            row, identity, reason = candidate
            async with sem:
                try:
                    result = await analyzer.extract_document_async(
                        row["minutes_url"], banana=row["banana"], retry_incomplete=True
                    )
                    content_sha256 = result.get("content_sha256")
                    if (
                        result.get("method") == "html_sanitized"
                        and len(result.get("text") or "") < MIN_HTML_MINUTES_CHARS
                    ):
                        raise ExtractionError(
                            "HTML minutes page is a viewer shell "
                            f"({len(result.get('text') or '')} chars)",
                            document_url=attachment_identity(row["minutes_url"]),
                            document_type="html",
                        )
                    ready = False
                    if content_sha256 and result.get("corpus_persisted"):
                        async with db.pool.acquire() as conn:
                            ready = await conn.fetchval(
                                CORPUS_READY_SQL, content_sha256, COMPATIBLE_EXTRACT_VERSIONS
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
                    async with db.pool.acquire() as conn:
                        await conn.execute(
                            LINK_SQL, row["id"], content_sha256, identity
                        )
                    await record_ingest_result(db, result, identity, row["minutes_url"], row["banana"])
                    counts["ingested"] += 1
                    logger.info(
                        "minutes ingested",
                        banana=row["banana"],
                        meeting_id=row["id"],
                        reason=reason,
                        chars=len(result.get("text") or ""),
                    )
                except DocumentDownloadError as e:
                    counts["failed_download"] += 1
                    failure = await record_failure(
                        db,
                        identity=identity,
                        source_url=row["minutes_url"],
                        banana=row["banana"],
                        stage="download",
                        error=e,
                        # Transient network/status failures keep backing off;
                        # deterministic 4xx and resolution failures use the
                        # configured cap for this extractor version.
                        max_failures=failure_attempt_limit(e, args.max_failures),
                        retry_days=args.failure_retry_days,
                    )
                    logger.warning(
                        "minutes download failed",
                        banana=row["banana"],
                        url=identity[:110],
                        attempt=(failure or {}).get("attempt_count"),
                        suppressed=(failure or {}).get("permanent", False),
                        error=failure_error_text(e, row["minutes_url"])[:200],
                    )
                except ExtractionError as e:
                    counts["failed_extract"] += 1
                    failure = await record_failure(
                        db,
                        identity=identity,
                        source_url=row["minutes_url"],
                        banana=row["banana"],
                        stage="extract",
                        error=e,
                        max_failures=args.max_failures,
                        retry_days=1 if is_transient_failure(e) else args.failure_retry_days,
                    )
                    logger.warning(
                        "minutes extraction failed",
                        banana=row["banana"],
                        url=identity[:110],
                        attempt=(failure or {}).get("attempt_count"),
                        suppressed=(failure or {}).get("permanent", False),
                        error=failure_error_text(e, row["minutes_url"])[:200],
                    )
                except Exception as e:
                    counts["failed_other"] += 1
                    logger.warning(
                        "minutes ingest failed (will retry)",
                        banana=row["banana"],
                        url=identity[:110],
                        error=failure_error_text(e, row["minutes_url"])[:200],
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
