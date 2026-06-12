"""Async QueueRepository for job queue operations

Handles job queue management with PostgreSQL optimizations:
- Atomic dequeue using FOR UPDATE SKIP LOCKED
- Smart retry logic with exponential backoff
- Dead letter queue for failed jobs
- Priority-based processing
"""

from typing import Any, Dict, List, Optional

from database.repositories_async.base import BaseRepository
from pipeline.models import QueueJob, MeetingJob, MatterJob
from config import get_logger

logger = get_logger(__name__).bind(component="queue_repository")


class QueueRepository(BaseRepository):
    """Repository for job queue operations

    Provides:
    - Enqueue jobs with deduplication
    - Atomic dequeue with row-level locking
    - Mark jobs complete/failed with retry logic
    - Queue statistics for monitoring
    """

    async def enqueue_job(
        self,
        source_url: str,
        job_type: str,
        payload: Dict[str, Any],
        meeting_id: Optional[str] = None,
        banana: Optional[str] = None,
        priority: int = 0,
        processing_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add job to processing queue with deduplication

        Uses source_url as unique key. If job already exists, resets status to pending.

        Args:
            source_url: Unique identifier for job (used for deduplication)
            job_type: Type of job (e.g., "meeting", "item", "matter")
            payload: Job data (will be JSON-serialized)
            meeting_id: Associated meeting ID
            banana: Associated city banana
            priority: Job priority (higher = processed first, default: 0)
            processing_metadata: Diagnostic trail (e.g. chunker cascade audit);
                on re-enqueue, a None keeps the previously stored value
        """
        await self._execute(
            """
            INSERT INTO queue (
                source_url, meeting_id, banana, job_type, payload,
                status, priority, retry_count, processing_metadata
            )
            VALUES ($1, $2, $3, $4, $5, 'pending', $6, 0, $7)
            ON CONFLICT (source_url) DO UPDATE SET
                status = 'pending',
                priority = EXCLUDED.priority,
                retry_count = 0,
                error_message = NULL,
                failed_at = NULL,
                processing_metadata = COALESCE(
                    EXCLUDED.processing_metadata, queue.processing_metadata
                )
            """,
            source_url,
            meeting_id,
            banana,
            job_type,
            payload,
            priority,
            processing_metadata,
        )

        logger.debug("job enqueued", source_url=source_url, job_type=job_type)

    async def get_chunker_hints(self) -> list:
        """Latest winning chunker rung per (vendor, slug, ladder).

        Read from the cascade audits persisted in processing_metadata —
        the audit trail doubles as the sticky-routing hint store, so
        hints survive restarts without a dedicated table.
        """
        rows = await self._fetch(
            """
            SELECT DISTINCT ON (
                j.vendor, j.slug, q.processing_metadata->'chunk'->>'winning_ladder'
            )
                j.vendor,
                j.slug,
                q.processing_metadata->'chunk'->>'winning_ladder' AS ladder,
                q.processing_metadata->'chunk'->>'winning_rung' AS rung
            FROM queue q
            JOIN jurisdictions j USING (banana)
            WHERE q.processing_metadata->'chunk'->>'winning_rung' IS NOT NULL
              AND q.processing_metadata->'chunk'->>'winning_ladder' IS NOT NULL
            ORDER BY
                j.vendor, j.slug,
                q.processing_metadata->'chunk'->>'winning_ladder',
                q.created_at DESC
            """
        )
        return [dict(r) for r in (rows or [])]

    async def reset_stale_processing_jobs(self, stale_minutes: int = 10) -> int:
        """Reset jobs stuck in 'processing' state after crash

        Jobs that have been 'processing' for longer than stale_minutes are
        assumed to have been abandoned due to a crash or restart. Reset them
        to 'pending' so they can be retried.

        Args:
            stale_minutes: Consider jobs stale after this many minutes (default: 10)

        Returns:
            Number of jobs reset
        """
        result = await self._fetch(
            """
            UPDATE queue
            SET status = 'pending',
                retry_count = retry_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
              AND started_at < NOW() - INTERVAL '1 minute' * $1
            RETURNING id
            """,
            stale_minutes,
        )
        count = len(result) if result else 0
        if count:
            logger.info("reset stale processing jobs", count=count, stale_minutes=stale_minutes)
        return count

    async def get_next_job(self) -> Optional[Dict[str, Any]]:
        """Get next pending job from queue (highest priority first)

        Uses FOR UPDATE SKIP LOCKED for safe concurrent access.

        Returns:
            Job dict with id, source_url, job_type, payload, etc., or None if queue empty
        """
        async with self.transaction() as conn:
            # Atomic SELECT-UPDATE with row-level locking
            row = await conn.fetchrow(
                """
                SELECT id, source_url, meeting_id, banana, job_type, payload,
                       priority, retry_count
                FROM queue
                WHERE status = 'pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )

            if not row:
                return None

            # Mark as processing
            await conn.execute(
                """
                UPDATE queue
                SET status = 'processing', started_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                row["id"],
            )

            return {
                "id": row["id"],
                "source_url": row["source_url"],
                "meeting_id": row["meeting_id"],
                "banana": row["banana"],
                "job_type": row["job_type"],
                "payload": row["payload"],  # Already deserialized by asyncpg
                "priority": row["priority"],
                "retry_count": row["retry_count"],
            }

    # Meeting jobs whose date falls outside the urgent window are batch-lane
    # eligible: nobody is waiting on them minute-to-minute, so they can take
    # the Gemini Batch API's multi-hour turnaround for the 50% discount.
    # Matter jobs, undated meetings, and orphaned meeting_ids stay streaming.
    _BATCH_ELIGIBLE_SQL = """q.job_type = 'meeting' AND m.date IS NOT NULL
                  AND (m.date < NOW() - make_interval(days => $__PAST__)
                       OR m.date > NOW() + make_interval(days => $__FUTURE__))"""

    async def get_next_for_processing(
        self,
        banana: Optional[str] = None,
        bananas: Optional[List[str]] = None,
        lane: Optional[str] = None,
        urgent_past_days: int = 0,
        urgent_future_days: int = 1,
    ) -> Optional[QueueJob]:
        """Get next typed job from processing queue

        Returns QueueJob with typed payload (MeetingJob or MatterJob).
        Uses atomic UPDATE-RETURNING to prevent race conditions.

        Args:
            banana: Optional single-city filter
            bananas: Optional multi-city filter (used by the CLI batch drain,
                which serves one worker pool across all cities in the run)
            lane: None claims any job (legacy behavior). 'streaming' claims
                jobs needing fresh summaries (urgent-window meetings, matters,
                undated). 'batch' claims meeting jobs outside the urgent
                window [now - urgent_past_days, now + urgent_future_days].
            urgent_past_days / urgent_future_days: urgent window bounds

        Returns:
            QueueJob object or None if queue empty
        """
        conditions = ["q.status = 'pending'"]
        params: list = []
        if banana:
            params.append(banana)
            conditions.append(f"q.banana = ${len(params)}")
        if bananas:
            params.append(list(bananas))
            conditions.append(f"q.banana = ANY(${len(params)})")

        join = ""
        if lane:
            join = "LEFT JOIN meetings m ON q.meeting_id = m.id"
            params.append(urgent_past_days)
            past_idx = len(params)
            params.append(urgent_future_days)
            future_idx = len(params)
            eligible = self._BATCH_ELIGIBLE_SQL.replace(
                "$__PAST__", f"${past_idx}"
            ).replace("$__FUTURE__", f"${future_idx}")
            if lane == "batch":
                conditions.append(f"({eligible})")
            elif lane == "streaming":
                conditions.append(f"NOT ({eligible})")
            else:
                raise ValueError(f"unknown lane: {lane!r}")

        async with self.transaction() as conn:
            # Atomic SELECT-UPDATE; FOR UPDATE OF q so the meetings join
            # doesn't take row locks on the meetings table
            row = await conn.fetchrow(
                f"""
                SELECT q.id, q.source_url, q.meeting_id, q.banana, q.job_type,
                       q.payload, q.priority, q.retry_count, q.status,
                       q.created_at, q.started_at
                FROM queue q
                {join}
                WHERE {" AND ".join(conditions)}
                ORDER BY q.priority DESC, q.created_at ASC
                LIMIT 1
                FOR UPDATE OF q SKIP LOCKED
                """,
                *params,
            )

            if not row:
                return None

            # Mark as processing
            await conn.execute(
                """
                UPDATE queue
                SET status = 'processing', started_at = NOW()
                WHERE id = $1
                """,
                row["id"],
            )

            # Get payload (JSONB automatically deserialized by codec)
            payload_data = row["payload"]

            if row["job_type"] == "meeting":
                payload = MeetingJob.from_dict(payload_data)
            elif row["job_type"] == "matter":
                payload = MatterJob.from_dict(payload_data)
            else:
                raise ValueError(f"Unknown job_type: {row['job_type']}")

            return QueueJob(
                id=row["id"],
                job_type=row["job_type"],
                payload=payload,
                banana=row["banana"],
                priority=row["priority"],
                status="processing",
                retry_count=row.get("retry_count", 0),
                error_message=None,
                created_at=row.get("created_at").isoformat() if row.get("created_at") else None,
                started_at=row.get("started_at").isoformat() if row.get("started_at") else None
            )

    async def get_chunk_quality(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Latest persisted chunk-audit quality signals for a meeting.

        Lets the processor read extraction-quality verdicts (seg_smell,
        garbage_titles) at summarization time — e.g. diverting under-split
        meetings to the monolithic packet path before paying for item
        summaries of wrong slices.
        """
        row = await self._fetchrow(
            """
            SELECT processing_metadata->'chunk'->'quality' AS quality
            FROM queue
            WHERE meeting_id = $1
              AND processing_metadata->'chunk'->'quality' IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            meeting_id,
        )
        return row["quality"] if row else None

    async def heartbeat_job(self, queue_id: int) -> None:
        """Refresh a processing job's claim so the stale sweep doesn't reclaim it.

        Batch-lane jobs legitimately park on Gemini poll loops far past
        STALE_SWEEP_MINUTES; bumping started_at marks them alive.
        """
        await self._execute(
            """
            UPDATE queue
            SET started_at = NOW()
            WHERE id = $1 AND status = 'processing'
            """,
            queue_id,
        )

    async def mark_processing_complete(self, queue_id: int) -> None:
        """Mark job as completed

        Args:
            queue_id: Queue job ID
        """
        await self._execute(
            """
            UPDATE queue
            SET status = 'completed', completed_at = NOW()
            WHERE id = $1
            """,
            queue_id,
        )

        logger.info("job completed", queue_id=queue_id)

    async def mark_job_failed(self, queue_id: int, error_message: str) -> None:
        """Mark job as failed with retry logic

        Implements retry logic:
        - retry_count < 3: Increment retry, set status back to pending
        - retry_count >= 3: Set status to dead_letter

        Args:
            queue_id: Queue entry ID
            error_message: Error description
        """
        async with self.transaction() as conn:
            # Get current retry count with row lock to prevent race
            row = await conn.fetchrow(
                "SELECT retry_count FROM queue WHERE id = $1 FOR UPDATE",
                queue_id,
            )

            if not row:
                return

            retry_count = row["retry_count"]

            if retry_count < 3:
                # Retry
                await conn.execute(
                    """
                    UPDATE queue
                    SET status = 'pending',
                        retry_count = retry_count + 1,
                        error_message = $2,
                        failed_at = NOW()
                    WHERE id = $1
                    """,
                    queue_id,
                    error_message,
                )
                logger.warning("job failed, retrying", queue_id=queue_id, retry_count=retry_count + 1)
            else:
                # Dead letter
                await conn.execute(
                    """
                    UPDATE queue
                    SET status = 'dead_letter',
                        retry_count = retry_count + 1,
                        error_message = $2,
                        failed_at = NOW()
                    WHERE id = $1
                    """,
                    queue_id,
                    error_message,
                )
                logger.error("job dead lettered", queue_id=queue_id, error=error_message)

    async def mark_processing_failed(
        self, queue_id: int, error_message: str, increment_retry: bool = True
    ) -> None:
        """Mark job as failed with smart retry logic

        Implements exponential backoff retry (3 attempts) before moving to DLQ.
        - retry_count < 3: Reset to 'pending' with lower priority
        - retry_count >= 3: Move to 'dead_letter' status

        Args:
            queue_id: Queue job ID
            error_message: Error description
            increment_retry: If False, mark as failed without retry logic
                           (used for non-retryable errors)
        """
        async with self.transaction() as conn:
            if not increment_retry:
                # Non-retryable error
                await conn.execute(
                    """
                    UPDATE queue
                    SET status = 'failed',
                        error_message = $2,
                        completed_at = NOW()
                    WHERE id = $1
                    """,
                    queue_id,
                    error_message,
                )
                logger.warning("marked queue item as failed (non-retryable)", queue_id=queue_id, error=error_message)
                return

            # Get current retry_count and priority with row lock to prevent race
            row = await conn.fetchrow(
                "SELECT retry_count, priority FROM queue WHERE id = $1 FOR UPDATE",
                queue_id,
            )

            if not row:
                logger.error("queue item not found", queue_id=queue_id)
                return

            current_retry_count = row["retry_count"]
            current_priority = row["priority"]

            if current_retry_count < 2:  # Will be 3 after increment (0 -> 1 -> 2)
                # Retry with exponential backoff priority
                new_priority = current_priority - (20 * (current_retry_count + 1))

                await conn.execute(
                    """
                    UPDATE queue
                    SET status = 'pending',
                        priority = $2,
                        retry_count = retry_count + 1,
                        error_message = $3,
                        completed_at = NULL
                    WHERE id = $1
                    """,
                    queue_id,
                    new_priority,
                    error_message,
                )
                logger.warning(
                    "job retry scheduled",
                    queue_id=queue_id,
                    attempt=current_retry_count + 1,
                    max_attempts=3,
                    priority_old=current_priority,
                    priority_new=new_priority,
                    error=error_message
                )
            else:
                # Move to dead letter queue
                await conn.execute(
                    """
                    UPDATE queue
                    SET status = 'dead_letter',
                        retry_count = retry_count + 1,
                        error_message = $2,
                        failed_at = NOW(),
                        completed_at = NOW()
                    WHERE id = $1
                    """,
                    queue_id,
                    error_message,
                )
                logger.error(
                    "job moved to dead letter queue",
                    queue_id=queue_id,
                    total_failures=current_retry_count + 1,
                    error=error_message
                )

    async def get_queue_stats(self) -> dict:
        """Get queue statistics for Prometheus

        Returns:
            Dict with {status}_count for each status and avg_processing_seconds
        """
        async with self.pool.acquire() as conn:
            # Count by status
            status_rows = await conn.fetch("""
                SELECT status, COUNT(*) as count
                FROM queue
                GROUP BY status
            """)

            # Build stats dict with {status}_count keys
            stats = {}
            for row in status_rows:
                stats[f"{row['status']}_count"] = row['count']

            # Ensure all statuses have defaults
            for status in ['pending', 'processing', 'completed', 'failed', 'dead_letter']:
                stats.setdefault(f"{status}_count", 0)

            # Average processing time (completed jobs only)
            avg_row = await conn.fetchrow("""
                SELECT AVG(EXTRACT(EPOCH FROM (completed_at - started_at))) as avg_seconds
                FROM queue
                WHERE status = 'completed'
                AND completed_at IS NOT NULL
                AND started_at IS NOT NULL
            """)

            stats['avg_processing_seconds'] = float(avg_row['avg_seconds']) if avg_row['avg_seconds'] else 0.0

            return stats

    async def get_dead_letter_jobs(self, limit: int = 100) -> List[dict]:
        """Get jobs in dead letter queue for admin review

        Args:
            limit: Maximum jobs to return (default: 100)

        Returns:
            List of dead letter jobs with full details
        """
        rows = await self._fetch(
            """
            SELECT
                id,
                job_type,
                meeting_id,
                banana,
                source_url,
                error_message,
                retry_count,
                created_at,
                failed_at
            FROM queue
            WHERE status = 'dead_letter'
            ORDER BY failed_at DESC
            LIMIT $1
            """,
            limit,
        )

        return [
            {
                "id": row["id"],
                "job_type": row["job_type"],
                "meeting_id": row["meeting_id"],
                "banana": row["banana"],
                "source_url": row["source_url"],
                "error_message": row["error_message"],
                "retry_count": row["retry_count"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "failed_at": row["failed_at"].isoformat() if row["failed_at"] else None,
            }
            for row in rows
        ]
