"""Durable pipeline run, attempt, stage, and outbox state."""

from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Dict, Iterable, Optional

from asyncpg import Connection

from database.repositories_async.base import BaseRepository


class PipelineLifecycleRepository(BaseRepository):
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
            WHERE id = $1
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
            WHERE id = $1
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
            WHERE id = $1
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
        conn: Optional[Connection] = None,
    ) -> None:
        async with self._ensure_conn(conn) as connection:
            await connection.execute(
                """
                INSERT INTO pipeline_outbox (
                    event_key, event_type, aggregate_type, aggregate_id, payload
                ) VALUES ($1, $2, $3, $4, $5::jsonb)
                ON CONFLICT (event_key) DO UPDATE SET
                    payload = CASE WHEN pipeline_outbox.status = 'published'
                                   THEN pipeline_outbox.payload ELSE EXCLUDED.payload END,
                    status = CASE WHEN pipeline_outbox.status = 'published'
                                  THEN 'published' ELSE 'pending' END,
                    next_attempt_at = CASE WHEN pipeline_outbox.status = 'published'
                        THEN pipeline_outbox.next_attempt_at ELSE NOW() END,
                    last_error = CASE WHEN pipeline_outbox.status = 'published'
                        THEN pipeline_outbox.last_error ELSE NULL END
                """,
                event_key, event_type, aggregate_type, aggregate_id, payload,
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
            conn=conn,
        )

    async def claim_outbox(self) -> Optional[Dict[str, Any]]:
        async with self.transaction() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, event_key, event_type, aggregate_type, aggregate_id,
                       payload, attempt_count
                FROM pipeline_outbox po
                WHERE po.status IN ('pending', 'failed')
                  AND po.next_attempt_at <= NOW()
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pipeline_outbox earlier
                      WHERE earlier.event_type = po.event_type
                        AND earlier.aggregate_type = po.aggregate_type
                        AND earlier.aggregate_id = po.aggregate_id
                        AND earlier.id < po.id
                        AND earlier.status <> 'published'
                  )
                ORDER BY po.next_attempt_at, po.id
                LIMIT 1 FOR UPDATE OF po SKIP LOCKED
                """
            )
            if row is None:
                return None
            await conn.execute(
                """UPDATE pipeline_outbox
                   SET status = 'publishing', attempt_count = attempt_count + 1
                   WHERE id = $1""",
                row["id"],
            )
            return dict(row)

    async def finish_outbox(
        self,
        outbox_id: int,
        *,
        succeeded: bool,
        error_message: Optional[str] = None,
        retry_seconds: int = 60,
    ) -> None:
        if succeeded:
            await self._execute(
                """UPDATE pipeline_outbox
                   SET status = 'published', published_at = NOW(), last_error = NULL
                   WHERE id = $1""",
                outbox_id,
            )
            return
        await self._execute(
            """
            UPDATE pipeline_outbox
            SET status = 'failed', last_error = $2,
                next_attempt_at = NOW() + make_interval(secs => $3)
            WHERE id = $1
            """,
            outbox_id, error_message, retry_seconds,
        )
