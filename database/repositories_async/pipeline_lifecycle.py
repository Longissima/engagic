"""Durable pipeline run, attempt, stage, and outbox state."""

from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Dict, Iterable, Optional

from asyncpg import Connection

from database.repositories_async.base import BaseRepository


def _newer_desired_work_sql(alias: str) -> str:
    """Whether a queue row or outbox intent supersedes ``alias`` by generation."""
    return f"""
        (
            {alias}.event_type = 'queue.enqueue'
            AND (
                EXISTS (
                    SELECT 1
                    FROM queue newer_queue
                    WHERE newer_queue.source_url =
                          {alias}.payload->>'source_url'
                      AND newer_queue.desired_generation >
                          {alias}.work_generation
                )
                OR EXISTS (
                    SELECT 1
                    FROM pipeline_outbox newer_event
                    WHERE newer_event.event_type = 'queue.enqueue'
                      AND newer_event.payload->>'source_url' =
                          {alias}.payload->>'source_url'
                      AND newer_event.work_generation >
                          {alias}.work_generation
                )
            )
        )
    """


def _unresolved_outbox_event_sql(alias: str) -> str:
    """One SQL predicate for current, unfulfilled publication intent.

    Queue source URLs are unique, but versions are opaque hashes. A shared
    monotonic generation orders direct queue writes and outbox intents without
    relying on transaction-start clocks: an exact queue version is fulfilled;
    a queue row at/after the event generation or a newer event for the same
    source supersedes it. Anything else remains replayable.
    """
    newer_work = _newer_desired_work_sql(alias)
    return f"""
        (
            {alias}.event_type <> 'queue.enqueue'
            OR (
                NOT EXISTS (
                    SELECT 1
                    FROM queue fulfilled
                    WHERE fulfilled.source_url =
                          {alias}.payload->>'source_url'
                      AND fulfilled.work_version IS NOT DISTINCT FROM
                          {alias}.payload->>'work_version'
                )
                AND NOT {newer_work}
            )
        )
    """


class PipelineLifecycleRepository(BaseRepository):
    async def get_operational_snapshot(self) -> Dict[str, Any]:
        """One read model for actionable health and durable throughput.

        Claim health uses the queue reclaimer's ten-minute stale threshold.
        ``hourly_performance`` groups terminal attempts by completion hour and
        running attempts by start hour for the trailing 24 hours. Item totals
        come from the durable attempt metrics journal rather than process-local
        counters.
        """
        unresolved = _unresolved_outbox_event_sql("candidate")
        row = await self._fetchrow(
            f"""
            WITH
            queue_counts AS (
                SELECT status, COUNT(*) AS count FROM queue GROUP BY status
            ),
            batch_counts AS (
                SELECT status, COUNT(*) AS count FROM batch_jobs GROUP BY status
            ),
            outbox_counts AS (
                SELECT status, COUNT(*) AS count
                FROM pipeline_outbox GROUP BY status
            ),
            outbox_ready AS (
                SELECT CASE
                           WHEN po.status = 'publishing'
                               THEN po.lease_expires_at
                           ELSE po.next_attempt_at
                       END AS ready_since
                FROM pipeline_outbox po
                WHERE (
                    (po.status IN ('pending', 'failed')
                     AND po.next_attempt_at <= NOW())
                    OR
                    (po.status = 'publishing'
                     AND po.lease_expires_at <= NOW())
                )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pipeline_outbox earlier
                      WHERE earlier.event_type = po.event_type
                        AND earlier.aggregate_type = po.aggregate_type
                        AND earlier.aggregate_id = po.aggregate_id
                        AND earlier.work_generation < po.work_generation
                        AND earlier.status NOT IN ('published', 'dead_letter')
                  )
            ),
            recent AS (
                SELECT
                    COUNT(*) AS attempts,
                    COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded,
                    COUNT(*) FILTER (
                        WHERE status IN (
                            'partial', 'retryable_failure', 'terminal_failure',
                            'abandoned'
                        )
                    ) AS non_success,
                    percentile_cont(0.50) WITHIN GROUP (
                        ORDER BY (metrics->>'queue_wait_ms')::numeric
                    ) FILTER (WHERE metrics ? 'queue_wait_ms') AS queue_wait_p50_ms,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY (metrics->>'queue_wait_ms')::numeric
                    ) FILTER (WHERE metrics ? 'queue_wait_ms') AS queue_wait_p95_ms,
                    percentile_cont(0.50) WITHIN GROUP (
                        ORDER BY (metrics->>'desired_age_ms')::numeric
                    ) FILTER (WHERE metrics ? 'desired_age_ms') AS desired_age_p50_ms,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY (metrics->>'desired_age_ms')::numeric
                    ) FILTER (WHERE metrics ? 'desired_age_ms') AS desired_age_p95_ms,
                    percentile_cont(0.50) WITHIN GROUP (
                        ORDER BY (metrics->>'service_ms')::numeric
                    ) FILTER (WHERE metrics ? 'service_ms') AS service_p50_ms,
                    percentile_cont(0.95) WITHIN GROUP (
                        ORDER BY (metrics->>'service_ms')::numeric
                    ) FILTER (WHERE metrics ? 'service_ms') AS service_p95_ms
                FROM job_attempts
                WHERE started_at >= NOW() - INTERVAL '1 hour'
            ),
            hourly AS (
                SELECT
                    date_trunc(
                        'hour', COALESCE(completed_at, started_at)
                    ) AS hour,
                    job_type,
                    COALESCE(lane, 'unknown') AS lane,
                    status AS outcome,
                    COUNT(*) AS attempts,
                    COALESCE(SUM(
                        (metrics->>'items_processed')::bigint
                    ) FILTER (
                        WHERE jsonb_typeof(metrics->'items_processed') = 'number'
                    ), 0) AS items_processed,
                    COALESCE(SUM(
                        (metrics->>'items_new')::bigint
                    ) FILTER (
                        WHERE jsonb_typeof(metrics->'items_new') = 'number'
                    ), 0) AS items_new,
                    COALESCE(SUM(
                        (metrics->>'items_failed')::bigint
                    ) FILTER (
                        WHERE jsonb_typeof(metrics->'items_failed') = 'number'
                    ), 0) AS items_failed,
                    AVG((metrics->>'queue_wait_ms')::numeric) FILTER (
                        WHERE jsonb_typeof(metrics->'queue_wait_ms') = 'number'
                    ) AS queue_wait_avg_ms,
                    AVG((metrics->>'desired_age_ms')::numeric) FILTER (
                        WHERE jsonb_typeof(metrics->'desired_age_ms') = 'number'
                    ) AS desired_age_avg_ms,
                    AVG((metrics->>'service_ms')::numeric) FILTER (
                        WHERE jsonb_typeof(metrics->'service_ms') = 'number'
                    ) AS service_avg_ms
                FROM job_attempts
                WHERE COALESCE(completed_at, started_at) >=
                      NOW() - INTERVAL '24 hours'
                GROUP BY 1, 2, 3, 4
            ),
            batch_hourly AS (
                SELECT
                    hour,
                    phase,
                    outcome,
                    COUNT(*) AS chunks,
                    COALESCE(SUM(items), 0) AS items,
                    AVG(provider_elapsed_ms) AS provider_elapsed_avg_ms
                FROM (
                    SELECT
                        date_trunc('hour', submitted_at) AS hour,
                        'submitted'::text AS phase,
                        'submitted'::text AS outcome,
                        jsonb_array_length(item_ids) AS items,
                        NULL::numeric AS provider_elapsed_ms
                    FROM batch_jobs
                    WHERE submitted_at >= NOW() - INTERVAL '24 hours'
                    UNION ALL
                    SELECT
                        date_trunc('hour', collected_at) AS hour,
                        'terminal'::text AS phase,
                        status AS outcome,
                        jsonb_array_length(item_ids) AS items,
                        CASE WHEN submitted_at IS NOT NULL THEN
                            EXTRACT(EPOCH FROM (collected_at - submitted_at))
                            * 1000
                        END AS provider_elapsed_ms
                    FROM batch_jobs
                    WHERE collected_at >= NOW() - INTERVAL '24 hours'
                ) events
                GROUP BY 1, 2, 3
            )
            SELECT
                COALESCE((SELECT jsonb_object_agg(status, count) FROM queue_counts), '{{}}') AS queue,
                COALESCE((SELECT jsonb_object_agg(status, count) FROM batch_counts), '{{}}') AS batch,
                COALESCE((SELECT jsonb_object_agg(status, count) FROM outbox_counts), '{{}}') AS outbox,
                (SELECT COUNT(*) FROM pipeline_runs WHERE status = 'running') AS active_runs,
                24 AS performance_window_hours,
                600 AS stale_claim_threshold_seconds,
                (SELECT EXTRACT(EPOCH FROM (NOW() - MIN(ready_at)))
                   FROM queue
                  WHERE status = 'pending'
                    AND (retry_at IS NULL OR retry_at <= NOW())) AS oldest_ready_seconds,
                (SELECT EXTRACT(EPOCH FROM (NOW() - MIN(last_enqueued_at)))
                   FROM queue
                  WHERE status = 'pending'
                    AND (retry_at IS NULL OR retry_at <= NOW())) AS oldest_desired_seconds,
                (SELECT EXTRACT(EPOCH FROM (NOW() - MIN(ready_since)))
                   FROM outbox_ready) AS oldest_outbox_ready_seconds,
                (SELECT COUNT(*)
                   FROM pipeline_outbox candidate
                  WHERE candidate.event_type = 'queue.enqueue'
                    AND candidate.status = 'dead_letter'
                    AND {unresolved}
                ) AS unresolved_queue_outbox_dead_letters,
                (SELECT COUNT(*)
                   FROM queue
                  WHERE status = 'processing'
                    AND claim_token IS NULL) AS tokenless_processing_claims,
                (SELECT COUNT(*)
                   FROM queue
                  WHERE status = 'processing'
                    AND (
                        COALESCE(heartbeat_at, claimed_at, started_at) IS NULL
                        OR COALESCE(heartbeat_at, claimed_at, started_at) <
                           NOW() - INTERVAL '10 minutes'
                    )) AS stale_processing_claims,
                (SELECT COUNT(*) FROM batch_jobs
                  WHERE status = 'submitted'
                    AND gemini_job_name LIKE 'intent:%') AS submission_intents,
                (SELECT COUNT(*) FROM batch_jobs
                  WHERE status = 'submitted' AND next_poll_at <= NOW()
                    AND gemini_job_name NOT LIKE 'intent:%') AS provider_jobs_due,
                COALESCE((
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'hour', hour,
                            'job_type', job_type,
                            'lane', lane,
                            'outcome', outcome,
                            'attempts', attempts,
                            'items_processed', items_processed,
                            'items_new', items_new,
                            'items_failed', items_failed,
                            'queue_wait_avg_ms', queue_wait_avg_ms,
                            'desired_age_avg_ms', desired_age_avg_ms,
                            'service_avg_ms', service_avg_ms
                        ) ORDER BY hour, job_type, lane, outcome
                    )
                    FROM hourly
                ), '[]'::jsonb) AS hourly_performance,
                COALESCE((
                    SELECT jsonb_agg(
                        jsonb_build_object(
                            'hour', hour,
                            'phase', phase,
                            'outcome', outcome,
                            'chunks', chunks,
                            'items', items,
                            'provider_elapsed_avg_ms', provider_elapsed_avg_ms
                        ) ORDER BY hour, phase, outcome
                    )
                    FROM batch_hourly
                ), '[]'::jsonb) AS hourly_batch_performance,
                recent.*
            FROM recent
            """
        )
        if row is None:  # pragma: no cover - aggregate query always yields
            return {}
        snapshot = dict(row)
        for key in (
            "oldest_ready_seconds",
            "oldest_desired_seconds",
            "oldest_outbox_ready_seconds",
            "queue_wait_p50_ms",
            "queue_wait_p95_ms",
            "desired_age_p50_ms",
            "desired_age_p95_ms",
            "service_p50_ms",
            "service_p95_ms",
        ):
            if snapshot.get(key) is not None:
                snapshot[key] = float(snapshot[key])
        snapshot["hourly_performance"] = snapshot.get("hourly_performance") or []
        snapshot["hourly_batch_performance"] = (
            snapshot.get("hourly_batch_performance") or []
        )
        return snapshot

    async def start_run(
        self,
        command: str,
        *,
        targets: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        run_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = await self._fetchrow(
            """
            INSERT INTO pipeline_runs (
                run_key, command, targets, host, process_id, metadata
            ) VALUES ($1, $2, $3::jsonb, $4, $5, $6::jsonb)
            RETURNING id, run_key, started_at
            """,
            run_key or str(uuid.uuid4()), command,
            list(targets) if targets is not None else None,
            socket.gethostname(), os.getpid(), metadata or {},
        )
        if row is None:  # pragma: no cover - INSERT RETURNING always yields
            raise RuntimeError("pipeline run insert returned no row")
        return dict(row)

    async def heartbeat_run(self, run_id: int) -> None:
        await self._execute(
            "UPDATE pipeline_runs SET heartbeat_at = NOW() WHERE id = $1 AND status = 'running'",
            run_id,
        )

    async def finish_run(
        self, run_id: int, status: str, error_message: Optional[str] = None
    ) -> None:
        await self._execute(
            """
            UPDATE pipeline_runs
            SET status = $2, completed_at = NOW(), heartbeat_at = NOW(),
                error_message = $3
            WHERE id = $1 AND status = 'running'
            """,
            run_id, status, error_message,
        )

    async def start_attempt(
        self,
        *,
        queue_id: int,
        run_id: Optional[int],
        job_type: str,
        lane: Optional[str],
        banana: Optional[str],
        meeting_id: Optional[str],
        matter_id: Optional[str],
        work_version: Optional[str],
    ) -> Dict[str, Any]:
        row = await self._fetchrow(
            """
            INSERT INTO job_attempts (
                queue_id, run_id, attempt_number, job_type, lane, banana,
                meeting_id, matter_id, work_version
            )
            SELECT $1, $2, COALESCE(MAX(attempt_number), 0) + 1,
                   $3, $4, $5, $6, $7, $8
            FROM job_attempts WHERE queue_id = $1
            RETURNING id, attempt_number, started_at
            """,
            queue_id, run_id, job_type, lane, banana, meeting_id, matter_id,
            work_version,
        )
        if row is None:  # pragma: no cover
            raise RuntimeError("job attempt insert returned no row")
        return dict(row)

    async def heartbeat_attempt(self, attempt_id: int) -> None:
        await self._execute(
            """UPDATE job_attempts SET heartbeat_at = NOW()
               WHERE id = $1 AND status = 'running'""",
            attempt_id,
        )

    async def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: str,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._execute(
            """
            UPDATE job_attempts
            SET status = $2, completed_at = NOW(), heartbeat_at = NOW(),
                error_type = $3, error_message = $4, metrics = $5::jsonb
            WHERE id = $1 AND status = 'running'
            """,
            attempt_id, status, error_type, error_message, metrics or {},
        )

    async def start_stage(
        self,
        *,
        attempt_id: Optional[int],
        run_id: Optional[int],
        stage: str,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> int:
        row = await self._fetchrow(
            """
            INSERT INTO pipeline_stage_events (attempt_id, run_id, stage, metrics)
            VALUES ($1, $2, $3, $4::jsonb) RETURNING id
            """,
            attempt_id, run_id, stage, metrics or {},
        )
        if row is None:  # pragma: no cover
            raise RuntimeError("stage event insert returned no row")
        return int(row["id"])

    async def finish_stage(
        self,
        stage_id: int,
        *,
        status: str,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._execute(
            """
            UPDATE pipeline_stage_events
            SET status = $2, completed_at = NOW(), error_type = $3,
                error_message = $4, metrics = metrics || $5::jsonb
            WHERE id = $1 AND status = 'running'
            """,
            stage_id, status, error_type, error_message, metrics or {},
        )

    async def enqueue_outbox(
        self,
        *,
        event_key: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Dict[str, Any],
        serialization_key: Optional[str] = None,
        conn: Optional[Connection] = None,
    ) -> None:
        recurrence = _newer_desired_work_sql("pipeline_outbox")
        async with self._ensure_conn(conn) as connection:
            await connection.execute(
                f"""
                WITH serialized AS MATERIALIZED (
                    SELECT pg_advisory_xact_lock(hashtextextended($6, 0))
                )
                INSERT INTO pipeline_outbox (
                    event_key, event_type, aggregate_type, aggregate_id, payload
                )
                SELECT $1, $2, $3, $4, $5::jsonb
                FROM serialized
                ON CONFLICT (event_key) DO UPDATE SET
                    payload = CASE WHEN {recurrence}
                                   THEN EXCLUDED.payload
                                   WHEN pipeline_outbox.status IN (
                                        'publishing', 'published', 'dead_letter'
                                   )
                                   THEN pipeline_outbox.payload ELSE EXCLUDED.payload END,
                    status = CASE WHEN {recurrence} THEN 'pending'
                                  WHEN pipeline_outbox.status IN (
                                       'publishing', 'published', 'dead_letter'
                                  )
                                  THEN pipeline_outbox.status ELSE 'pending' END,
                    attempt_count = CASE WHEN {recurrence} THEN 0
                        ELSE pipeline_outbox.attempt_count END,
                    next_attempt_at = CASE WHEN {recurrence} THEN NOW()
                        WHEN pipeline_outbox.status IN (
                                                'publishing', 'published', 'dead_letter'
                                           )
                        THEN pipeline_outbox.next_attempt_at ELSE NOW() END,
                    last_error = CASE WHEN {recurrence} THEN NULL
                        WHEN pipeline_outbox.status IN (
                                           'publishing', 'published', 'dead_letter'
                                      )
                        THEN pipeline_outbox.last_error ELSE NULL END,
                    claimed_at = CASE WHEN NOT ({recurrence})
                                           AND pipeline_outbox.status = 'publishing'
                        THEN pipeline_outbox.claimed_at ELSE NULL END,
                    lease_owner = CASE WHEN NOT ({recurrence})
                                           AND pipeline_outbox.status = 'publishing'
                        THEN pipeline_outbox.lease_owner ELSE NULL END,
                    lease_expires_at = CASE WHEN NOT ({recurrence})
                                           AND pipeline_outbox.status = 'publishing'
                        THEN pipeline_outbox.lease_expires_at ELSE NULL END,
                    claim_token = CASE WHEN NOT ({recurrence})
                                           AND pipeline_outbox.status = 'publishing'
                        THEN pipeline_outbox.claim_token ELSE NULL END,
                    work_generation = CASE WHEN {recurrence}
                        THEN EXCLUDED.work_generation
                        ELSE pipeline_outbox.work_generation END,
                    published_at = CASE WHEN {recurrence} THEN NULL
                        ELSE pipeline_outbox.published_at END
                """,
                event_key,
                event_type,
                aggregate_type,
                aggregate_id,
                payload,
                serialization_key or event_key,
            )

    async def enqueue_queue_job(
        self,
        *,
        source_url: str,
        job_type: str,
        payload: Dict[str, Any],
        aggregate_id: str,
        meeting_id: Optional[str],
        banana: Optional[str],
        priority: int,
        work_version: Optional[str],
        processing_metadata: Optional[Dict[str, Any]] = None,
        conn: Optional[Connection] = None,
    ) -> None:
        """Write a version-keyed queue publication intent in the caller's UoW."""
        await self.enqueue_outbox(
            event_key=f"queue.enqueue:{source_url}:{work_version or 'legacy'}",
            event_type="queue.enqueue",
            aggregate_type=job_type,
            aggregate_id=aggregate_id,
            payload={
                "source_url": source_url,
                "job_type": job_type,
                "payload": payload,
                "meeting_id": meeting_id,
                "banana": banana,
                "priority": priority,
                "processing_metadata": processing_metadata,
                "work_version": work_version,
            },
            serialization_key=f"queue-intent:{source_url}",
            conn=conn,
        )

    async def claim_outbox(
        self,
        *,
        event_type: Optional[str] = None,
        bananas: Optional[Iterable[str]] = None,
        lease_owner: Optional[str] = None,
        lease_seconds: int = 300,
    ) -> Optional[Dict[str, Any]]:
        scope = list(dict.fromkeys(bananas)) if bananas is not None else None
        async with self.transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, event_key, event_type, aggregate_type, aggregate_id,
                       payload, attempt_count, work_generation
                FROM pipeline_outbox po
                WHERE ($1::text IS NULL OR po.event_type = $1)
                  AND ($2::text[] IS NULL OR po.payload->>'banana' = ANY($2))
                  AND (
                      (po.status IN ('pending', 'failed')
                       AND po.next_attempt_at <= NOW())
                      OR
                      (po.status = 'publishing' AND po.lease_expires_at <= NOW())
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pipeline_outbox earlier
                      WHERE earlier.event_type = po.event_type
                        AND earlier.aggregate_type = po.aggregate_type
                        AND earlier.aggregate_id = po.aggregate_id
                        AND earlier.work_generation < po.work_generation
                        AND earlier.status NOT IN ('published', 'dead_letter')
                  )
                ORDER BY po.next_attempt_at, po.work_generation
                LIMIT 1 FOR UPDATE OF po SKIP LOCKED
                """,
                event_type,
                scope,
            )
            if row is None:
                return None
            claim_token = str(uuid.uuid4())
            await conn.execute(
                """UPDATE pipeline_outbox
                   SET status = 'publishing', attempt_count = attempt_count + 1,
                       claimed_at = NOW(), lease_owner = $2,
                       lease_expires_at = NOW() + make_interval(secs => $3),
                       claim_token = $4::uuid
                   WHERE id = $1""",
                row["id"], lease_owner or f"{socket.gethostname()}:{os.getpid()}",
                lease_seconds, claim_token,
            )
            event = dict(row)
            event["claim_token"] = claim_token
            return event

    async def get_outbox_activity(
        self,
        *,
        event_type: Optional[str] = None,
        bananas: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Return one scoped snapshot of outbox completion and retry state.

        ``next_attempt_at`` is the next time a currently claimable event can
        make progress. For an in-flight publication that is its lease expiry;
        later events for the same aggregate remain ordered behind their
        predecessor and do not make the scope appear immediately runnable.
        """
        scope = list(dict.fromkeys(bananas)) if bananas is not None else None
        unresolved = _unresolved_outbox_event_sql("candidate")
        row = await self._fetchrow(
            f"""
            WITH scoped AS (
                SELECT po.*
                FROM pipeline_outbox po
                WHERE ($1::text IS NULL OR po.event_type = $1)
                  AND ($2::text[] IS NULL OR po.payload->>'banana' = ANY($2))
            ), actionable AS (
                SELECT po.*
                FROM scoped po
                WHERE po.status IN ('pending', 'failed', 'publishing')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pipeline_outbox earlier
                      WHERE earlier.event_type = po.event_type
                        AND earlier.aggregate_type = po.aggregate_type
                        AND earlier.aggregate_id = po.aggregate_id
                        AND earlier.work_generation < po.work_generation
                        AND earlier.status NOT IN ('published', 'dead_letter')
                  )
            )
            SELECT
                COUNT(*) FILTER (
                    WHERE status IN ('pending', 'failed', 'publishing')
                ) AS active,
                COUNT(*) FILTER (
                    WHERE candidate.status = 'dead_letter'
                      AND {unresolved}
                ) AS dead_letter,
                (SELECT COUNT(*)
                   FROM actionable
                  WHERE (status IN ('pending', 'failed')
                         AND next_attempt_at <= NOW())
                     OR (status = 'publishing' AND lease_expires_at <= NOW())
                ) AS ready,
                (SELECT MIN(
                    CASE WHEN status = 'publishing' THEN lease_expires_at
                         ELSE next_attempt_at END
                ) FROM actionable) AS next_attempt_at
            FROM scoped candidate
            """,
            event_type,
            scope,
        )
        if row is None:  # pragma: no cover - aggregate query always yields
            return {
                "active": 0,
                "dead_letter": 0,
                "ready": 0,
                "next_attempt_at": None,
            }
        return {
            "active": int(row["active"] or 0),
            "dead_letter": int(row["dead_letter"] or 0),
            "ready": int(row["ready"] or 0),
            "next_attempt_at": row["next_attempt_at"],
        }

    async def count_active_outbox(
        self,
        *,
        event_type: Optional[str] = None,
        bananas: Optional[Iterable[str]] = None,
    ) -> int:
        """Compatibility adapter over the canonical scoped activity read."""
        activity = await self.get_outbox_activity(
            event_type=event_type, bananas=bananas
        )
        return int(activity["active"])

    async def count_dead_letter_outbox(
        self,
        *,
        event_type: Optional[str] = None,
        bananas: Optional[Iterable[str]] = None,
    ) -> int:
        """Compatibility adapter over the canonical scoped activity read."""
        activity = await self.get_outbox_activity(
            event_type=event_type, bananas=bananas
        )
        return int(activity["dead_letter"])

    async def has_unresolved_outbox_for_aggregate(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Whether an aggregate still has current, unfulfilled publication work."""
        unresolved = _unresolved_outbox_event_sql("candidate")
        async with self._ensure_conn(conn) as connection:
            return bool(
                await connection.fetchval(
                    f"""
                    SELECT EXISTS (
                        SELECT 1
                        FROM pipeline_outbox candidate
                        WHERE candidate.event_type = $1
                          AND candidate.aggregate_type = $2
                          AND candidate.aggregate_id = $3
                          AND candidate.status IN (
                              'pending', 'publishing', 'failed', 'dead_letter'
                          )
                          AND {unresolved}
                    )
                    """,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                )
            )

    async def recover_stale_lifecycle(
        self, *, stale_minutes: int = 15
    ) -> Dict[str, int]:
        """Close lifecycle rows whose owning process stopped heartbeating.

        One atomic statement selects stale attempts and runs, closes their
        running stages, and records terminal outcomes. A run is retained when
        it still owns a fresh running attempt even if the run heartbeat itself
        is late. Terminal rows are fenced from subsequent stale-worker writes
        by the status predicates in the finish methods above.
        """
        if stale_minutes <= 0:
            raise ValueError("stale_minutes must be positive")
        row = await self._fetchrow(
            """
            WITH stale_attempts AS MATERIALIZED (
                SELECT id
                FROM job_attempts
                WHERE status = 'running'
                  AND heartbeat_at < NOW() - make_interval(mins => $1)
                FOR UPDATE SKIP LOCKED
            ), stale_runs AS MATERIALIZED (
                SELECT pr.id
                FROM pipeline_runs pr
                WHERE pr.status = 'running'
                  AND pr.heartbeat_at < NOW() - make_interval(mins => $1)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM job_attempts active_attempt
                      WHERE active_attempt.run_id = pr.id
                        AND active_attempt.status = 'running'
                        AND active_attempt.heartbeat_at >=
                            NOW() - make_interval(mins => $1)
                  )
                FOR UPDATE OF pr SKIP LOCKED
            ), recovered_stages AS (
                UPDATE pipeline_stage_events stage
                SET status = 'failed', completed_at = NOW(),
                    error_type = COALESCE(stage.error_type, 'StaleLifecycle'),
                    error_message = COALESCE(
                        stage.error_message,
                        'owning lifecycle heartbeat expired'
                    )
                WHERE stage.status = 'running'
                  AND (
                      stage.attempt_id IN (SELECT id FROM stale_attempts)
                      OR stage.run_id IN (SELECT id FROM stale_runs)
                  )
                RETURNING stage.id
            ), recovered_attempts AS (
                UPDATE job_attempts attempt
                SET status = 'abandoned', completed_at = NOW(),
                    heartbeat_at = NOW(),
                    error_type = COALESCE(attempt.error_type, 'StaleLifecycle'),
                    error_message = COALESCE(
                        attempt.error_message,
                        'attempt heartbeat expired'
                    )
                WHERE attempt.id IN (SELECT id FROM stale_attempts)
                  AND attempt.status = 'running'
                RETURNING attempt.id
            ), recovered_runs AS (
                UPDATE pipeline_runs run
                SET status = 'failed', completed_at = NOW(),
                    heartbeat_at = NOW(),
                    error_message = COALESCE(
                        run.error_message,
                        'pipeline run heartbeat expired'
                    )
                WHERE run.id IN (SELECT id FROM stale_runs)
                  AND run.status = 'running'
                RETURNING run.id
            )
            SELECT
                (SELECT COUNT(*) FROM recovered_attempts) AS attempts,
                (SELECT COUNT(*) FROM recovered_runs) AS runs,
                (SELECT COUNT(*) FROM recovered_stages) AS stages
            """,
            stale_minutes,
        )
        if row is None:  # pragma: no cover - aggregate query always yields
            return {"attempts": 0, "runs": 0, "stages": 0}
        return {
            "attempts": int(row["attempts"] or 0),
            "runs": int(row["runs"] or 0),
            "stages": int(row["stages"] or 0),
        }

    async def finish_outbox(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        claim_token: str,
        succeeded: bool,
        error_message: Optional[str] = None,
        retry_seconds: int = 60,
    ) -> bool:
        """Finish a delivery only while this worker still owns its lease.

        A slow publisher may outlive its lease and overlap a replacement
        attempt. The owner predicate prevents that stale worker from
        overwriting the replacement's result.
        """
        if succeeded:
            row = await self._fetchrow(
                """UPDATE pipeline_outbox
                   SET status = 'published', published_at = NOW(), last_error = NULL,
                       lease_owner = NULL, lease_expires_at = NULL,
                       claim_token = NULL
                   WHERE id = $1 AND status = 'publishing' AND lease_owner = $2
                     AND claim_token = $3::uuid
                   RETURNING id""",
                outbox_id, lease_owner, claim_token,
            )
            return row is not None
        row = await self._fetchrow(
            """
            UPDATE pipeline_outbox
            SET status = CASE WHEN attempt_count >= 8
                              THEN 'dead_letter' ELSE 'failed' END,
                last_error = $2,
                next_attempt_at = NOW() + make_interval(secs => $3),
                lease_owner = NULL,
                lease_expires_at = NULL,
                claim_token = NULL
            WHERE id = $1 AND status = 'publishing' AND lease_owner = $4
              AND claim_token = $5::uuid
            RETURNING id
            """,
            outbox_id, error_message, retry_seconds, lease_owner, claim_token,
        )
        return row is not None

    async def reactivate_outbox(self, event_key: str) -> bool:
        """Replay one still-unfulfilled event by stable identity.

        A queue publication is fulfilled by its exact queue version and
        superseded by a later queue write or later outbox intent. Replaying an
        older version could otherwise replace newer desired work because queue
        enqueue intentionally accepts version changes.
        """
        unresolved = _unresolved_outbox_event_sql("candidate")
        row = await self._fetchrow(
            f"""
            UPDATE pipeline_outbox AS candidate
            SET status = 'pending', attempt_count = 0, next_attempt_at = NOW(),
                last_error = NULL, claimed_at = NULL, lease_owner = NULL,
                lease_expires_at = NULL, claim_token = NULL, published_at = NULL
            WHERE candidate.event_key = $1
              AND candidate.status IN ('failed', 'dead_letter')
              AND {unresolved}
            RETURNING id
            """,
            event_key,
        )
        return row is not None
