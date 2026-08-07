"""Async BatchJobRepository for decoupled Gemini Batch API jobs.

A submitted batch job is a durable record, not an in-flight coroutine: the
submit path writes a row and releases its lane slot, and a collector polls open
rows and ingests results once Gemini reports a terminal state. This survives
process restarts (the job name lives in Postgres, not on a stack) and never
holds compute hostage -- we poll, we never cancel.

Lifecycle: submitted -> collected (results ingested) | failed (Gemini's
terminal FAILED/EXPIRED/CANCELLED, or an unrecoverable ingest error).
"""

import uuid
from typing import Any, Dict, List, Optional

from database.repositories_async.base import BaseRepository
from config import get_logger

logger = get_logger(__name__).bind(component="batch_job_repository")


class BatchJobRepository(BaseRepository):
    """Repository for tracking submitted Gemini Batch API jobs."""

    async def reserve_submission(
        self,
        submission_key: str,
        meeting_id: str,
        item_ids: List[str],
        chunk_num: int = 1,
        banana: Optional[str] = None,
        cache_name: Optional[str] = None,
        prompts_version: Optional[str] = None,
        meeting_meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Reserve one logical chunk before making the provider create call.

        The partial unique index on submission_key serializes concurrent
        submitters while the chunk is open.  A terminal row releases the key,
        allowing a genuinely failed/partial item set to be submitted again.
        """
        # gemini_job_name is globally unique history; submission_key is only
        # unique while open. A fresh suffix lets a terminal logical chunk be
        # retried without colliding with its prior intent row.
        intent_name = f"intent:{submission_key}:{uuid.uuid4()}"
        row = await self._fetchrow(
            """
            INSERT INTO batch_jobs (
                gemini_job_name, submission_key, meeting_id, banana, chunk_num,
                item_ids, cache_name, prompts_version, meeting_meta, status,
                submit_attempts, next_poll_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'submitted', 0, NOW())
            ON CONFLICT (submission_key) WHERE status = 'submitted'
            DO NOTHING
            RETURNING id
            """,
            intent_name,
            submission_key,
            meeting_id,
            banana,
            chunk_num,
            item_ids,
            cache_name,
            prompts_version,
            meeting_meta,
        )
        reserved = row is not None
        logger.info(
            "batch submission intent",
            submission_key=submission_key,
            meeting_id=meeting_id,
            chunk_num=chunk_num,
            reserved=reserved,
        )
        return reserved

    async def activate_submission(
        self,
        submission_key: str,
        gemini_job_name: str,
        submit_attempts: int,
    ) -> None:
        """Attach the provider job name to its durable pre-create intent."""
        row = await self._fetchrow(
            """
            UPDATE batch_jobs
            SET gemini_job_name = $2,
                submit_attempts = GREATEST(submit_attempts, $3),
                error_message = NULL,
                next_poll_at = NOW()
            WHERE submission_key = $1
              AND status = 'submitted'
              AND gemini_job_name LIKE 'intent:%'
            RETURNING id
            """,
            submission_key,
            gemini_job_name,
            submit_attempts,
        )
        if row is None:
            raise RuntimeError(
                f"No reserved batch submission intent for {submission_key}"
            )
        logger.info(
            "activated batch submission",
            submission_key=submission_key,
            gemini_job_name=gemini_job_name,
        )

    async def mark_submission_intent_failed(
        self,
        submission_key: str,
        error_message: str,
        submit_attempts: int,
    ) -> None:
        """Release a pre-create intent after provider submission exhausts retries."""
        await self._execute(
            """
            UPDATE batch_jobs
            SET status = 'failed', error_message = $2,
                submit_attempts = GREATEST(submit_attempts, $3),
                collected_at = NOW()
            WHERE submission_key = $1
              AND status = 'submitted'
              AND gemini_job_name LIKE 'intent:%'
            """,
            submission_key,
            error_message,
            submit_attempts,
        )

    async def expire_stale_submission_intents(
        self, meeting_id: str, max_age_minutes: int = 30
    ) -> None:
        """Release intents abandoned before their provider name was recorded."""
        await self._execute(
            """
            UPDATE batch_jobs
            SET status = 'failed',
                error_message = 'Submission intent expired before provider activation',
                collected_at = NOW()
            WHERE meeting_id = $1
              AND status = 'submitted'
              AND gemini_job_name LIKE 'intent:%'
              AND created_at < NOW() - ($2 * INTERVAL '1 minute')
            """,
            meeting_id,
            max_age_minutes,
        )

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
        submission_key: Optional[str] = None,
        submit_attempts: int = 1,
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
                cache_name, prompts_version, meeting_meta, status,
                submission_key, submit_attempts, next_poll_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'submitted', $9, $10, NOW())
            ON CONFLICT (gemini_job_name) DO UPDATE SET
                status = 'submitted',
                error_message = NULL,
                collected_at = NULL,
                item_ids = EXCLUDED.item_ids,
                meeting_meta = EXCLUDED.meeting_meta,
                submission_key = EXCLUDED.submission_key,
                submit_attempts = EXCLUDED.submit_attempts,
                next_poll_at = NOW()
            """,
            gemini_job_name,
            meeting_id,
            banana,
            chunk_num,
            item_ids,
            cache_name,
            prompts_version,
            meeting_meta,
            submission_key,
            submit_attempts,
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
                   created_at, submission_key, submit_attempts, poll_attempts,
                   poll_error_count, consecutive_poll_errors, next_poll_at
            FROM batch_jobs
            WHERE status = 'submitted'
              AND gemini_job_name NOT LIKE 'intent:%'
            ORDER BY created_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    async def claim_open(
        self,
        collector_id: str,
        limit: int = 100,
        lease_seconds: int = 900,
    ) -> List[Dict[str, Any]]:
        """Lease due provider jobs atomically for one collector tick."""
        rows = await self._fetch(
            """
            WITH candidates AS (
                SELECT id
                FROM batch_jobs
                WHERE status = 'submitted'
                  AND gemini_job_name NOT LIKE 'intent:%'
                  AND next_poll_at <= NOW()
                  AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                ORDER BY next_poll_at ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            )
            UPDATE batch_jobs AS job
            SET lease_owner = $2,
                lease_expires_at = NOW() + ($3 * INTERVAL '1 second'),
                poll_attempts = poll_attempts + 1
            FROM candidates
            WHERE job.id = candidates.id
            RETURNING job.id, job.gemini_job_name, job.meeting_id, job.banana,
                      job.chunk_num, job.item_ids, job.cache_name,
                      job.prompts_version, job.meeting_meta, job.created_at,
                      job.submission_key, job.submit_attempts,
                      job.poll_attempts, job.poll_error_count,
                      job.consecutive_poll_errors, job.next_poll_at
            """,
            limit,
            collector_id,
            lease_seconds,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    async def claim_open_for_bananas(
        self,
        bananas: List[str],
        collector_id: str,
        limit: int = 100,
        lease_seconds: int = 900,
    ) -> List[Dict[str, Any]]:
        """Lease due provider jobs for a finite/scoped supervisor."""
        if not bananas:
            return []
        rows = await self._fetch(
            """
            WITH candidates AS (
                SELECT id
                FROM batch_jobs
                WHERE status = 'submitted'
                  AND banana = ANY($1)
                  AND gemini_job_name NOT LIKE 'intent:%'
                  AND next_poll_at <= NOW()
                  AND (lease_expires_at IS NULL OR lease_expires_at < NOW())
                ORDER BY next_poll_at ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT $3
            )
            UPDATE batch_jobs AS job
            SET lease_owner = $2,
                lease_expires_at = NOW() + ($4 * INTERVAL '1 second'),
                poll_attempts = poll_attempts + 1
            FROM candidates
            WHERE job.id = candidates.id
            RETURNING job.id, job.gemini_job_name, job.meeting_id, job.banana,
                      job.chunk_num, job.item_ids, job.cache_name,
                      job.prompts_version, job.meeting_meta, job.created_at,
                      job.submission_key, job.submit_attempts,
                      job.poll_attempts, job.poll_error_count,
                      job.consecutive_poll_errors, job.next_poll_at
            """,
            list(bananas),
            collector_id,
            limit,
            lease_seconds,
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
                   created_at, submission_key, submit_attempts, poll_attempts,
                   poll_error_count, consecutive_poll_errors, next_poll_at
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
                   created_at, submission_key, submit_attempts, poll_attempts,
                   poll_error_count, consecutive_poll_errors, next_poll_at
            FROM batch_jobs
            WHERE status = 'submitted' AND banana = ANY($1)
            ORDER BY created_at ASC
            LIMIT $2
            """,
            list(bananas),
            limit,
        )
        return [self._row_to_dict(r) for r in (rows or [])]

    async def count_open_for_bananas(self, bananas: List[str]) -> int:
        """Count all scoped open rows, including not-yet-due jobs and intents."""
        if not bananas:
            return 0
        row = await self._fetchrow(
            """
            SELECT COUNT(*) AS n
            FROM batch_jobs
            WHERE status = 'submitted' AND banana = ANY($1)
            """,
            list(bananas),
        )
        return int(row["n"]) if row else 0

    async def get_open_item_ids_for_meeting(self, meeting_id: str) -> set[str]:
        """Item identities already owned by active chunks or submit intents."""
        rows = await self._fetch(
            """
            SELECT DISTINCT jsonb_array_elements_text(item_ids) AS item_id
            FROM batch_jobs
            WHERE meeting_id = $1 AND status = 'submitted'
            """,
            meeting_id,
        )
        return {str(row["item_id"]) for row in (rows or [])}

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

    async def mark_polled(self, job_id: int, poll_after_seconds: int = 60) -> None:
        """Record a poll that found the job still running (observability)."""
        await self._execute(
            """
            UPDATE batch_jobs
            SET last_polled_at = NOW(),
                next_poll_at = NOW() + ($2 * INTERVAL '1 second'),
                consecutive_poll_errors = 0,
                error_message = NULL,
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = $1 AND status = 'submitted'
            """,
            job_id,
            poll_after_seconds,
        )

    async def mark_transient_failure(self, job_id: int, error_message: str) -> int:
        """Persist a collector failure and release its lease with backoff.

        Returns the new consecutive error count.  Backoff starts at 30 seconds
        and caps at 30 minutes; a later successful running poll resets only the
        consecutive counter while preserving the cumulative error count.
        """
        row = await self._fetchrow(
            """
            UPDATE batch_jobs
            SET poll_error_count = poll_error_count + 1,
                consecutive_poll_errors = consecutive_poll_errors + 1,
                error_message = $2,
                last_error_at = NOW(),
                next_poll_at = NOW() + (
                    LEAST(
                        1800,
                        30 * POWER(2, LEAST(consecutive_poll_errors, 6))
                    ) * INTERVAL '1 second'
                ),
                lease_owner = NULL,
                lease_expires_at = NULL
            WHERE id = $1 AND status = 'submitted'
            RETURNING consecutive_poll_errors
            """,
            job_id,
            error_message[:2000],
        )
        return int(row["consecutive_poll_errors"]) if row else 0

    async def mark_collected(self, job_id: int) -> None:
        """Mark a job's results successfully ingested."""
        await self._execute(
            """
            UPDATE batch_jobs
            SET status = 'collected', collected_at = NOW(), last_polled_at = NOW(),
                lease_owner = NULL, lease_expires_at = NULL,
                consecutive_poll_errors = 0, error_message = NULL
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
                collected_at = NOW(), last_polled_at = NOW(),
                lease_owner = NULL, lease_expires_at = NULL
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
            "submission_key": row["submission_key"],
            "submit_attempts": row["submit_attempts"],
            "poll_attempts": row["poll_attempts"],
            "poll_error_count": row["poll_error_count"],
            "consecutive_poll_errors": row["consecutive_poll_errors"],
            "next_poll_at": (
                row["next_poll_at"].isoformat() if row["next_poll_at"] else None
            ),
        }
