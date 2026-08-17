"""Async QueueRepository for job queue operations

Handles job queue management with PostgreSQL optimizations:
- Atomic dequeue using FOR UPDATE SKIP LOCKED
- Smart retry logic with exponential backoff
- Dead letter queue for failed jobs
- Priority-based processing
"""

from typing import Any, Dict, List, Optional
from uuid import uuid4

from asyncpg import Connection

from database.repositories_async.base import BaseRepository
from pipeline.models import QueueJob, MeetingJob, MatterJob
from config import get_logger

logger = get_logger(__name__).bind(component="queue_repository")


def _validate_desired_generation(desired_generation: Optional[int]) -> None:
    if desired_generation is not None and (
        isinstance(desired_generation, bool)
        or not isinstance(desired_generation, int)
        or desired_generation <= 0
    ):
        raise ValueError("desired_generation must be a positive integer")


class QueueRepository(BaseRepository):
    """Repository for job queue operations

    Provides:
    - Enqueue jobs with deduplication
    - Atomic dequeue with row-level locking
    - Mark jobs complete/failed with retry logic
    - Queue statistics for monitoring
    """

    _RETRY_BASE_SECONDS = 30

    async def lock_desired_state(
        self,
        source_url: str,
        *,
        conn: Connection,
    ) -> Optional[Dict[str, Any]]:
        """Lock one desired-work slot in the canonical global order.

        Callers that must inspect and then upsert a queue descriptor inside a
        larger domain transaction use this boundary instead of locking the
        queue row directly.  The transaction-scoped advisory lock is acquired
        first, matching :meth:`enqueue_job`; the row lock follows.  A missing
        row still leaves the source slot serialized so a subsequent enqueue in
        the same transaction cannot race another writer.
        """
        await conn.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended('queue-intent:' || $1, 0)
            )
            """,
            source_url,
        )
        row = await conn.fetchrow(
            """
            SELECT status, work_version, desired_generation, claim_token
            FROM queue
            WHERE source_url = $1
            FOR UPDATE
            """,
            source_url,
        )
        return dict(row) if row is not None else None

    async def enqueue_job(
        self,
        source_url: str,
        job_type: str,
        payload: Dict[str, Any],
        meeting_id: Optional[str] = None,
        banana: Optional[str] = None,
        priority: int = 0,
        processing_metadata: Optional[Dict[str, Any]] = None,
        work_version: Optional[str] = None,
        desired_generation: Optional[int] = None,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Add job to processing queue with deduplication

        Uses source_url as the stable deduplication key. An accepted re-enqueue
        replaces the mutable work descriptor and starts a fresh attempt cycle.

        Versioned work is accepted only when its desired version changes. This
        protects retries and terminal jobs from being reset by an identical
        sync. Legacy unversioned work preserves the old re-enqueue behavior,
        except that a healthy active claim is never reset.

        Args:
            source_url: Unique identifier for job (used for deduplication)
            job_type: Type of job (e.g., "meeting", "item", "matter")
            payload: Job data (will be JSON-serialized)
            meeting_id: Associated meeting ID
            banana: Associated city banana
            priority: Job priority (higher = processed first, default: 0)
            processing_metadata: Diagnostic trail (e.g. chunker cascade audit).
                Re-enqueue replaces it when supplied; None retains the prior
                audit because it also serves as the sticky chunk-routing hint.
            work_version: Authoritative desired-work descriptor. Matter jobs
                use the versioned ``mw1`` attachment-and-title identity.
            desired_generation: Durable total-order value allocated with the
                originating outbox intent. Direct callers omit it and let the
                database allocate one from the shared work-generation sequence.

        Returns:
            True when the desired row was inserted or advanced; False when an
            equal/newer queue generation already made this request a no-op.
        """
        _validate_desired_generation(desired_generation)

        async with self._ensure_conn(conn) as connection:
            row = await connection.fetchrow(
            """
            WITH serialized AS MATERIALIZED (
                SELECT pg_advisory_xact_lock(
                    hashtextextended('queue-intent:' || $1, 0)
                )
            )
            INSERT INTO queue (
                source_url, meeting_id, banana, job_type, payload,
                status, priority, retry_count, processing_metadata, work_version,
                desired_generation
            )
            SELECT
                $1, $2, $3, $4, $5, 'pending', $6, 0, $7, $8,
                COALESCE($9, nextval('pipeline_work_generation_seq'))
            FROM serialized
            ON CONFLICT (source_url) DO UPDATE SET
                meeting_id = EXCLUDED.meeting_id,
                banana = EXCLUDED.banana,
                job_type = EXCLUDED.job_type,
                payload = EXCLUDED.payload,
                status = 'pending',
                priority = EXCLUDED.priority,
                retry_count = 0,
                started_at = NULL,
                completed_at = NULL,
                failed_at = NULL,
                error_message = NULL,
                processing_metadata = COALESCE(
                    EXCLUDED.processing_metadata, queue.processing_metadata
                ),
                work_version = EXCLUDED.work_version,
                desired_generation = EXCLUDED.desired_generation,
                retry_at = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                heartbeat_at = NULL,
                last_enqueued_at = CURRENT_TIMESTAMP,
                ready_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE EXCLUDED.desired_generation > queue.desired_generation
              AND (
                    (
                        EXCLUDED.work_version IS NOT NULL
                        AND queue.work_version IS DISTINCT FROM
                            EXCLUDED.work_version
                    )
                    OR (
                        EXCLUDED.work_version IS NULL
                        AND queue.work_version IS NULL
                        AND queue.status <> 'processing'
                    )
                  )
            RETURNING id
            """,
                source_url,
                meeting_id,
                banana,
                job_type,
                payload,
                priority,
                processing_metadata,
                work_version,
                desired_generation,
            )

        accepted = row is not None
        logger.debug(
            "queue desired work evaluated",
            source_url=source_url,
            job_type=job_type,
            accepted=accepted,
            desired_generation=desired_generation,
        )
        return accepted

    async def invalidate_desired_work(
        self,
        source_url: str,
        job_type: str,
        payload: Dict[str, Any],
        *,
        work_version: str,
        meeting_id: Optional[str] = None,
        banana: Optional[str] = None,
        desired_generation: Optional[int] = None,
        conn: Connection,
    ) -> bool:
        """Publish an authoritative terminal descriptor for no desired work.

        This is a desired-state tombstone, not an executable queue job.  It
        participates in the same per-source advisory lock and global
        generation order as :meth:`enqueue_job`, so it can atomically fence a
        claimed older version and supersede older queue/outbox publications.
        A later real-work enqueue can reopen the source only with a newer
        generation and a distinct work version.

        The caller supplies the exact version of the authoritative empty (or
        otherwise non-executable) work snapshot and an existing transaction.
        Policy-driven tombstones must use a dedicated no-work descriptor that
        is distinct from the executable work version; otherwise a later policy
        change cannot reopen identical content under version deduplication.
        Repeating an already-completed version is a no-op unless an
        intervening outbox generation expresses different desired work.  In
        that recurrence case the tombstone advances its generation so the
        older publication cannot resurrect work after this transaction.
        """
        if not isinstance(work_version, str) or not work_version:
            raise ValueError("work_version must be non-empty text")
        _validate_desired_generation(desired_generation)

        row = await conn.fetchrow(
            """
            WITH serialized AS MATERIALIZED (
                SELECT pg_advisory_xact_lock(
                    hashtextextended('queue-intent:' || $1, 0)
                )
            ), desired AS MATERIALIZED (
                SELECT COALESCE(
                    $7::bigint,
                    nextval('pipeline_work_generation_seq')
                ) AS desired_generation
                FROM serialized
            )
            INSERT INTO queue (
                source_url, meeting_id, banana, job_type, payload,
                status, priority, retry_count, completed_at, work_version,
                desired_generation
            )
            SELECT
                $1, $2, $3, $4, $5, 'completed', 0, 0,
                CURRENT_TIMESTAMP, $6, desired.desired_generation
            FROM desired
            ON CONFLICT (source_url) DO UPDATE SET
                meeting_id = EXCLUDED.meeting_id,
                banana = EXCLUDED.banana,
                job_type = EXCLUDED.job_type,
                payload = EXCLUDED.payload,
                status = 'completed',
                priority = 0,
                retry_count = 0,
                started_at = NULL,
                completed_at = CURRENT_TIMESTAMP,
                failed_at = NULL,
                error_message = NULL,
                work_version = EXCLUDED.work_version,
                desired_generation = EXCLUDED.desired_generation,
                retry_at = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                heartbeat_at = NULL,
                ready_at = CURRENT_TIMESTAMP,
                last_enqueued_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE EXCLUDED.desired_generation > queue.desired_generation
              AND (
                    NOT (
                        queue.status = 'completed'
                        AND queue.work_version IS NOT DISTINCT FROM
                            EXCLUDED.work_version
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM pipeline_outbox intervening
                        WHERE intervening.event_type = 'queue.enqueue'
                          AND intervening.payload->>'source_url' =
                              queue.source_url
                          AND intervening.payload->>'work_version'
                              IS DISTINCT FROM EXCLUDED.work_version
                          AND intervening.work_generation >
                              queue.desired_generation
                          AND intervening.work_generation <
                              EXCLUDED.desired_generation
                    )
                  )
            RETURNING id
            """,
            source_url,
            meeting_id,
            banana,
            job_type,
            payload,
            work_version,
            desired_generation,
        )
        accepted = row is not None
        logger.debug(
            "queue desired work invalidated",
            source_url=source_url,
            job_type=job_type,
            accepted=accepted,
            work_version=work_version,
            desired_generation=desired_generation,
        )
        return accepted

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
                q.last_enqueued_at DESC NULLS LAST,
                q.updated_at DESC NULLS LAST,
                q.id DESC
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
            SET status = CASE WHEN retry_count >= 2
                              THEN 'dead_letter' ELSE 'pending' END,
                retry_count = retry_count + 1,
                started_at = NULL,
                claim_token = NULL,
                claimed_at = NULL,
                heartbeat_at = NULL,
                retry_at = CASE WHEN retry_count >= 2 THEN NULL ELSE NOW() END,
                ready_at = CASE WHEN retry_count >= 2 THEN ready_at ELSE NOW() END,
                failed_at = NOW(),
                completed_at = CASE WHEN retry_count >= 2 THEN NOW() ELSE NULL END,
                error_message = CASE WHEN retry_count >= 2
                    THEN 'claim repeatedly abandoned; moved to dead letter'
                    ELSE 'stale processing claim reclaimed' END,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'processing'
              AND (
                  COALESCE(heartbeat_at, claimed_at, started_at)
                      < NOW() - INTERVAL '1 minute' * $1
                  OR COALESCE(heartbeat_at, claimed_at, started_at) IS NULL
              )
            RETURNING id
            """,
            stale_minutes,
        )
        count = len(result) if result else 0
        if count:
            logger.info("reset stale processing jobs", count=count, stale_minutes=stale_minutes)
        return count

    # Meeting jobs whose date falls outside the urgent window are batch-lane
    # eligible: nobody is waiting on them minute-to-minute, so they can take
    # the Gemini Batch API's multi-hour turnaround for the 50% discount.
    # Matter jobs, undated meetings, and orphaned meeting_ids stay streaming.
    _BATCH_ELIGIBLE_SQL = """q.job_type = 'meeting' AND m.date IS NOT NULL
                  AND (m.date < NOW() - make_interval(days => $__PAST__)
                       OR m.date > NOW() + make_interval(days => $__FUTURE__))"""

    async def preview_jobs(
        self, banana: Optional[str] = None, limit: int = 10
    ) -> List[QueueJob]:
        """Return ready pending jobs without claiming or mutating them."""
        if limit <= 0:
            return []
        rows = await self._fetch(
            """
            SELECT id, source_url, meeting_id, banana, job_type, payload,
                   priority, retry_count, status, error_message, created_at,
                   started_at, completed_at, work_version, last_enqueued_at,
                   claim_token, claimed_at, heartbeat_at, ready_at
            FROM queue
            WHERE status = 'pending'
              AND (retry_at IS NULL OR retry_at <= NOW())
              AND ($1::text IS NULL OR banana = $1)
            ORDER BY priority DESC, last_enqueued_at ASC, id ASC
            LIMIT $2
            """,
            banana,
            limit,
        )
        return [QueueJob.from_db_row(dict(row)) for row in (rows or [])]

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
        if bananas is not None and not bananas:
            return None

        conditions = [
            "q.status = 'pending'",
            "(q.retry_at IS NULL OR q.retry_at <= NOW())",
        ]
        params: list = []
        if banana:
            params.append(banana)
            conditions.append(f"q.banana = ${len(params)}")
        if bananas is not None:
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
                       q.created_at, q.started_at, q.work_version,
                       q.last_enqueued_at, q.ready_at
                FROM queue q
                {join}
                WHERE {" AND ".join(conditions)}
                ORDER BY q.priority DESC, q.last_enqueued_at ASC, q.id ASC
                LIMIT 1
                FOR UPDATE OF q SKIP LOCKED
                """,
                *params,
            )

            if not row:
                return None

            claim_token = str(uuid4())
            claim = await conn.fetchrow(
                """
                UPDATE queue
                SET status = 'processing',
                    claim_token = $2::uuid,
                    claimed_at = NOW(),
                    heartbeat_at = NOW(),
                    started_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1
                RETURNING claimed_at, heartbeat_at, started_at
                """,
                row["id"],
                claim_token,
            )
            if claim is None:  # pragma: no cover - row is locked above
                return None

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
                started_at=claim["started_at"].isoformat(),
                work_version=row.get("work_version"),
                last_enqueued_at=(
                    row.get("last_enqueued_at").isoformat()
                    if row.get("last_enqueued_at")
                    else None
                ),
                claim_token=claim_token,
                claimed_at=claim["claimed_at"].isoformat(),
                heartbeat_at=claim["heartbeat_at"].isoformat(),
                ready_at=(
                    row.get("ready_at").isoformat()
                    if row.get("ready_at")
                    else None
                ),
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
            SELECT processing_metadata->'chunk'->'quality'
                   || jsonb_build_object(
                        'item_count',
                        COALESCE(
                            (processing_metadata->'chunk'->>'item_count')::int,
                            0
                        )
                      ) AS quality
            FROM queue
            WHERE meeting_id = $1
              AND processing_metadata->'chunk'->'quality' IS NOT NULL
            ORDER BY last_enqueued_at DESC NULLS LAST,
                     updated_at DESC NULLS LAST,
                     id DESC
            LIMIT 1
            """,
            meeting_id,
        )
        return row["quality"] if row else None

    async def get_chunk_profiles(
        self,
        meeting_id: str,
        work_version: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Measured PDF morphology for the meeting's exact work version.

        One profile per cascade run (agenda ladder, then packet ladder), each
        carrying page_count/external_links/text_chars as the chunker measured
        them. The processor reads these to recognize a document with nothing
        behind it -- a single page of item labels linking to no staff reports
        -- and decline to summarize rather than invent. Chunk metadata is
        sticky routing history, so an unstamped or stale audit is diagnostics,
        never evidence for a semantic decision.
        """
        if not work_version:
            return []
        rows = await self._fetch(
            """
            WITH latest AS (
                SELECT processing_metadata->'chunk'->'runs' AS runs
                FROM queue
                WHERE meeting_id = $1
                  AND job_type = 'meeting'
                  AND work_version IS NOT DISTINCT FROM $2
                  AND processing_metadata->'chunk'->>'work_version' = $2
                  AND jsonb_typeof(processing_metadata->'chunk'->'runs') = 'array'
                ORDER BY last_enqueued_at DESC NULLS LAST,
                         updated_at DESC NULLS LAST,
                         id DESC
                LIMIT 1
            )
            SELECT run->'profile' AS profile
            FROM latest, jsonb_array_elements(latest.runs) AS run
            WHERE jsonb_typeof(run->'profile') = 'object'
            """,
            meeting_id,
            work_version,
        )
        return [row["profile"] for row in rows]

    async def heartbeat_job(
        self,
        queue_id: int,
        claim_token: str,
        work_version: Optional[str],
    ) -> bool:
        """Renew only the caller's active claim without changing its start time."""
        row = await self._fetchrow(
            """
            UPDATE queue
            SET heartbeat_at = NOW(), updated_at = NOW()
            WHERE id = $1
              AND status = 'processing'
              AND claim_token = $2::uuid
              AND work_version IS NOT DISTINCT FROM $3
            RETURNING id
            """,
            queue_id,
            claim_token,
            work_version,
        )
        return row is not None

    async def get_scope_activity(
        self, bananas: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Return finite-runtime termination state for a jurisdiction scope."""
        row = await self._fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'processing') AS processing,
                COUNT(*) FILTER (
                    WHERE status = 'pending'
                      AND (retry_at IS NULL OR retry_at <= NOW())
                ) AS ready,
                MIN(retry_at) FILTER (
                    WHERE status = 'pending' AND retry_at > NOW()
                ) AS next_retry_at
            FROM queue
            WHERE ($1::text[] IS NULL OR banana = ANY($1))
              AND status IN ('pending', 'processing')
            """,
            list(bananas) if bananas is not None else None,
        )
        if not row:
            return {
                "pending": 0,
                "processing": 0,
                "ready": 0,
                "next_retry_at": None,
            }
        return {
            "pending": int(row["pending"] or 0),
            "processing": int(row["processing"] or 0),
            "ready": int(row["ready"] or 0),
            "next_retry_at": row["next_retry_at"],
        }

    async def reactivate_job_version(
        self,
        *,
        source_url: str,
        work_version: str,
        priority: Optional[int] = None,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Explicitly schedule one exact descriptor without weakening enqueue dedup.

        Ordinary sync enqueue cannot revive an unchanged failed/completed
        version. Provider reconciliation is an explicit retry authority and may
        do so only while the row still carries exactly that version. An active
        same-version claim is invalidated: a later batch sibling may have exposed
        additional missing work that requires a fresh pass.
        """
        async with self._ensure_conn(conn) as connection:
            row = await connection.fetchrow(
                """
                UPDATE queue
                SET status = 'pending',
                    priority = COALESCE($3, priority),
                    retry_count = 0,
                    started_at = NULL,
                    claim_token = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    completed_at = NULL,
                    failed_at = NULL,
                    error_message = NULL,
                    retry_at = NULL,
                    last_enqueued_at = NOW(),
                    ready_at = NOW(),
                    updated_at = NOW()
                WHERE source_url = $1
                  AND work_version IS NOT DISTINCT FROM $2
                  AND status IN ('completed', 'failed', 'dead_letter', 'processing')
                RETURNING id
                """,
                source_url,
                work_version,
                priority,
            )
            if row is not None:
                return True
            active = await connection.fetchrow(
                """
                SELECT id
                FROM queue
                WHERE source_url = $1
                  AND work_version IS NOT DISTINCT FROM $2
                  AND status IN ('pending', 'processing')
                """,
                source_url,
                work_version,
            )
            return active is not None

    async def retry_job_version(
        self,
        *,
        source_url: str,
        work_version: str,
        error_message: str,
        priority: Optional[int] = None,
        conn: Optional[Connection] = None,
    ) -> Optional[str]:
        """Record a provider failure without resetting the attempt budget.

        A completed batch submission has already released its queue claim, so
        the ordinary claim-scoped failure transition cannot be used by the
        collector. This exact-version transition gives it the same three-try
        budget. Concurrent failed sibling chunks observe the already-pending
        retry instead of charging the meeting multiple attempts.
        """
        async with self._ensure_conn(conn) as connection:
            row = await connection.fetchrow(
                """
                UPDATE queue
                SET status = CASE WHEN retry_count >= 2
                                  THEN 'dead_letter' ELSE 'pending' END,
                    priority = COALESCE($3, priority),
                    retry_count = retry_count + 1,
                    started_at = NULL,
                    claim_token = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    completed_at = CASE WHEN retry_count >= 2
                                        THEN NOW() ELSE NULL END,
                    failed_at = NOW(),
                    error_message = $4,
                    retry_at = CASE WHEN retry_count >= 2 THEN NULL ELSE
                        NOW() + make_interval(
                            secs => $5 * CAST(power(2, retry_count) AS INTEGER)
                        ) END,
                    ready_at = CASE WHEN retry_count >= 2 THEN ready_at ELSE
                        NOW() + make_interval(
                            secs => $5 * CAST(power(2, retry_count) AS INTEGER)
                        ) END,
                    last_enqueued_at = NOW(),
                    updated_at = NOW()
                WHERE source_url = $1
                  AND work_version IS NOT DISTINCT FROM $2
                  AND status IN ('completed', 'processing')
                RETURNING status
                """,
                source_url,
                work_version,
                priority,
                error_message[:2000],
                self._RETRY_BASE_SECONDS,
            )
            if row is not None:
                status = str(row["status"])
                logger.warning(
                    "recorded provider retry",
                    source_url=source_url,
                    status=status,
                    error=error_message,
                )
                return status
            current = await connection.fetchrow(
                """
                SELECT status
                FROM queue
                WHERE source_url = $1
                  AND work_version IS NOT DISTINCT FROM $2
                  AND status IN ('pending', 'processing', 'failed', 'dead_letter')
                """,
                source_url,
                work_version,
            )
            return str(current["status"]) if current is not None else None

    async def fail_job_version(
        self,
        *,
        source_url: str,
        work_version: str,
        error_message: str,
        conn: Optional[Connection] = None,
    ) -> Optional[str]:
        """Terminally fail one unchanged descriptor after provider rejection."""
        async with self._ensure_conn(conn) as connection:
            row = await connection.fetchrow(
                """
                UPDATE queue
                SET status = 'failed',
                    started_at = NULL,
                    claim_token = NULL,
                    claimed_at = NULL,
                    heartbeat_at = NULL,
                    completed_at = NOW(),
                    failed_at = NOW(),
                    error_message = $3,
                    retry_at = NULL,
                    updated_at = NOW()
                WHERE source_url = $1
                  AND work_version IS NOT DISTINCT FROM $2
                  AND status IN ('pending', 'processing', 'completed')
                RETURNING status
                """,
                source_url,
                work_version,
                error_message[:2000],
            )
            if row is not None:
                logger.warning(
                    "terminally failed provider work",
                    source_url=source_url,
                    error=error_message,
                )
                return str(row["status"])
            current = await connection.fetchrow(
                """
                SELECT status
                FROM queue
                WHERE source_url = $1
                  AND work_version IS NOT DISTINCT FROM $2
                  AND status IN ('failed', 'dead_letter')
                """,
                source_url,
                work_version,
            )
            return str(current["status"]) if current is not None else None

    async def release_processing_claim(
        self,
        queue_id: int,
        claim_token: str,
        work_version: Optional[str],
        *,
        error_message: str = "worker cancelled before completion",
    ) -> bool:
        """Immediately release an unsettled claim without reviving newer work."""
        row = await self._fetchrow(
            """
            UPDATE queue
            SET status = 'pending', started_at = NULL,
                claim_token = NULL, claimed_at = NULL, heartbeat_at = NULL,
                retry_at = NOW(),
                ready_at = NOW(),
                error_message = $4,
                updated_at = NOW()
            WHERE id = $1
              AND status = 'processing'
              AND claim_token = $2::uuid
              AND work_version IS NOT DISTINCT FROM $3
            RETURNING id
            """,
            queue_id,
            claim_token,
            work_version,
            error_message,
        )
        return row is not None

    async def mark_processing_complete(
        self,
        queue_id: int,
        claim_token: str,
        work_version: Optional[str],
    ) -> bool:
        """Mark job as completed

        Args:
            queue_id: Queue job ID
        """
        row = await self._fetchrow(
            """
            UPDATE queue
            SET status = 'completed', completed_at = NOW(), updated_at = NOW(),
                claim_token = NULL
            WHERE id = $1
              AND status = 'processing'
              AND claim_token = $2::uuid
              AND work_version IS NOT DISTINCT FROM $3
            RETURNING id
            """,
            queue_id,
            claim_token,
            work_version,
        )

        if row is not None:
            logger.info("job completed", queue_id=queue_id)
        return row is not None

    async def mark_job_failed(
        self,
        queue_id: int,
        error_message: str,
        *,
        claim_token: str,
        work_version: Optional[str],
    ) -> bool:
        """Backward-compatible alias for the single failure state machine."""
        return await self.mark_processing_failed(
            queue_id,
            error_message,
            claim_token=claim_token,
            work_version=work_version,
        )

    async def mark_processing_failed(
        self,
        queue_id: int,
        error_message: str,
        *,
        claim_token: str,
        work_version: Optional[str],
        increment_retry: bool = True,
    ) -> bool:
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
                row = await conn.fetchrow(
                    """
                    UPDATE queue
                    SET status = 'failed',
                        error_message = $2,
                        failed_at = NOW(),
                        completed_at = NOW(),
                        claim_token = NULL,
                        updated_at = NOW()
                    WHERE id = $1
                      AND status = 'processing'
                      AND claim_token = $3::uuid
                      AND work_version IS NOT DISTINCT FROM $4
                    RETURNING id
                    """,
                    queue_id,
                    error_message,
                    claim_token,
                    work_version,
                )
                if row is not None:
                    logger.warning("marked queue item as failed (non-retryable)", queue_id=queue_id, error=error_message)
                return row is not None

            # Get current retry_count and priority with row lock to prevent race
            row = await conn.fetchrow(
                """SELECT retry_count, priority
                   FROM queue
                   WHERE id = $1
                     AND status = 'processing'
                     AND claim_token = $2::uuid
                     AND work_version IS NOT DISTINCT FROM $3
                   FOR UPDATE""",
                queue_id,
                claim_token,
                work_version,
            )

            if not row:
                logger.info("queue item no longer actively claimed", queue_id=queue_id)
                return False

            current_retry_count = row["retry_count"]
            current_priority = row["priority"]

            if current_retry_count < 2:  # Will be 3 after increment (0 -> 1 -> 2)
                # Retry with exponential backoff priority
                new_priority = current_priority - (20 * (current_retry_count + 1))
                retry_delay_seconds = self._RETRY_BASE_SECONDS * (2 ** current_retry_count)

                await conn.execute(
                    """
                    UPDATE queue
                    SET status = 'pending',
                        priority = $2,
                        retry_count = retry_count + 1,
                        error_message = $3,
                        failed_at = NOW(),
                        completed_at = NULL,
                        started_at = NULL,
                        claim_token = NULL,
                        claimed_at = NULL,
                        heartbeat_at = NULL,
                        retry_at = NOW() + make_interval(secs => $4),
                        ready_at = NOW() + make_interval(secs => $4),
                        updated_at = NOW()
                    WHERE id = $1
                      AND status = 'processing'
                      AND claim_token = $5::uuid
                      AND work_version IS NOT DISTINCT FROM $6
                    """,
                    queue_id,
                    new_priority,
                    error_message,
                    retry_delay_seconds,
                    claim_token,
                    work_version,
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
                        completed_at = NOW(),
                        retry_at = NULL,
                        claim_token = NULL,
                        updated_at = NOW()
                    WHERE id = $1
                      AND status = 'processing'
                      AND claim_token = $3::uuid
                      AND work_version IS NOT DISTINCT FROM $4
                    """,
                    queue_id,
                    error_message,
                    claim_token,
                    work_version,
                )
                logger.error(
                    "job moved to dead letter queue",
                    queue_id=queue_id,
                    total_failures=current_retry_count + 1,
                    error=error_message
                )
            return True

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

            # Average processing time (completed jobs only). claimed_at is a
            # stable claim start; heartbeat_at moves while the lease is alive.
            avg_row = await conn.fetchrow("""
                SELECT AVG(EXTRACT(EPOCH FROM (
                    completed_at - COALESCE(claimed_at, started_at)
                ))) as avg_seconds
                FROM queue
                WHERE status = 'completed'
                AND completed_at IS NOT NULL
                AND COALESCE(claimed_at, started_at) IS NOT NULL
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
