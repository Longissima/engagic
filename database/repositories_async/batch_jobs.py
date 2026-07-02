"""Async BatchJobRepository for decoupled Gemini Batch API jobs.

A submitted batch job is a durable record, not an in-flight coroutine: the
submit path writes a row and releases its lane slot, and a collector polls open
rows and ingests results once Gemini reports a terminal state. This survives
process restarts (the job name lives in Postgres, not on a stack) and never
holds compute hostage -- we poll, we never cancel.

Lifecycle: submitted -> collected (results ingested) | failed (Gemini's
terminal FAILED/EXPIRED/CANCELLED, or an unrecoverable ingest error).
"""

from typing import Any, Dict, List, Optional

from database.repositories_async.base import BaseRepository
from config import get_logger

logger = get_logger(__name__).bind(component="batch_job_repository")


class BatchJobRepository(BaseRepository):
    """Repository for tracking submitted Gemini Batch API jobs."""

    async def record_submission(
        self,
        gemini_job_name: str,
        meeting_id: str,
        item_ids: List[str],
        chunk_num: int = 1,
        banana: Optional[str] = None,
        cache_name: Optional[str] = None,
        prompts_version: Optional[str] = None,
        meeting_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a freshly submitted batch job so the collector can find it.

        gemini_job_name is unique: a resubmit of the same job name (should not
        happen, but defends against a double-submit race) refreshes the row
        back to 'submitted' rather than erroring.
        """
        await self._execute(
            """
            INSERT INTO batch_jobs (
                gemini_job_name, meeting_id, banana, chunk_num, item_ids,
                cache_name, prompts_version, meeting_meta, status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'submitted')
            ON CONFLICT (gemini_job_name) DO UPDATE SET
                status = 'submitted',
                error_message = NULL,
                collected_at = NULL,
                item_ids = EXCLUDED.item_ids,
                meeting_meta = EXCLUDED.meeting_meta
            """,
            gemini_job_name,
            meeting_id,
            banana,
            chunk_num,
            item_ids,
            cache_name,
            prompts_version,
            meeting_meta,
        )
        logger.info(
            "recorded batch submission",
            gemini_job_name=gemini_job_name,
            meeting_id=meeting_id,
            chunk_num=chunk_num,
            item_count=len(item_ids),
        )

    async def list_open(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Open (submitted, not yet terminal) jobs, oldest first.

        Oldest-first so the longest-waiting jobs are polled every tick; the
        collector decides per-job whether it has reached a terminal state.
        """
        rows = await self._fetch(
            """
            SELECT id, gemini_job_name, meeting_id, banana, chunk_num,
                   item_ids, cache_name, prompts_version, meeting_meta,
                   created_at
            FROM batch_jobs
            WHERE status = 'submitted'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    async def list_open_for_meetings(
        self, meeting_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """Open jobs for a specific set of meetings (scoped CLI drain)."""
        if not meeting_ids:
            return []
        rows = await self._fetch(
            """
            SELECT id, gemini_job_name, meeting_id, banana, chunk_num,
                   item_ids, cache_name, prompts_version, meeting_meta,
                   created_at
            FROM batch_jobs
            WHERE status = 'submitted' AND meeting_id = ANY($1)
            ORDER BY created_at ASC
            """,
            list(meeting_ids),
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    async def list_open_for_bananas(
        self, bananas: List[str], limit: int = 500
    ) -> List[Dict[str, Any]]:
        """Open jobs for a set of cities (scoped CLI collect-drain)."""
        if not bananas:
            return []
        rows = await self._fetch(
            """
            SELECT id, gemini_job_name, meeting_id, banana, chunk_num,
                   item_ids, cache_name, prompts_version, meeting_meta,
                   created_at
            FROM batch_jobs
            WHERE status = 'submitted' AND banana = ANY($1)
            ORDER BY created_at ASC
            LIMIT $2
            """,
            list(bananas),
            limit,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    async def count_open_for_meeting(self, meeting_id: str) -> int:
        """In-flight chunk count for a meeting -- the double-submit guard."""
        row = await self._fetchrow(
            """
            SELECT COUNT(*) AS n
            FROM batch_jobs
            WHERE meeting_id = $1 AND status = 'submitted'
            """,
            meeting_id,
        )
        return int(row["n"]) if row else 0

    async def mark_polled(self, job_id: int) -> None:
        """Record a poll that found the job still running (observability)."""
        await self._execute(
            "UPDATE batch_jobs SET last_polled_at = NOW() WHERE id = $1",
            job_id,
        )

    async def mark_collected(self, job_id: int) -> None:
        """Mark a job's results successfully ingested."""
        await self._execute(
            """
            UPDATE batch_jobs
            SET status = 'collected', collected_at = NOW(), last_polled_at = NOW()
            WHERE id = $1
            """,
            job_id,
        )
        logger.info("batch job collected", job_id=job_id)

    async def mark_failed(self, job_id: int, error_message: str) -> None:
        """Mark a job failed (Gemini terminal failure or ingest error)."""
        await self._execute(
            """
            UPDATE batch_jobs
            SET status = 'failed', error_message = $2,
                collected_at = NOW(), last_polled_at = NOW()
            WHERE id = $1
            """,
            job_id,
            error_message,
        )
        logger.warning("batch job failed", job_id=job_id, error=error_message)

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        # item_ids / meeting_meta arrive already decoded by the jsonb codec.
        return {
            "id": row["id"],
            "gemini_job_name": row["gemini_job_name"],
            "meeting_id": row["meeting_id"],
            "banana": row["banana"],
            "chunk_num": row["chunk_num"],
            "item_ids": row["item_ids"] or [],
            "cache_name": row["cache_name"],
            "prompts_version": row["prompts_version"],
            "meeting_meta": row["meeting_meta"] or {},
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
