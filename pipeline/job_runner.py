"""One queue-job execution policy for CLI and daemon runtimes."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

from exceptions import ExtractionError, LLMError, ProcessingError
from pipeline.models import QueueJob
from pipeline.outcomes import JobOutcome, OutcomeStatus


class TerminalJobError(RuntimeError):
    """The claimed descriptor cannot succeed without different desired work."""


class SupersededWorkError(TerminalJobError):
    """The attempt is obsolete because newer desired work owns the aggregate."""


class _QueueClaimLost(RuntimeError):
    """The worker no longer owns the queue descriptor it was executing."""


JobHandler = Callable[[QueueJob], Awaitable[JobOutcome | Mapping[str, Any] | None]]


@dataclass(frozen=True, slots=True)
class JobExecutionPolicy:
    timeout_seconds: float
    heartbeat_seconds: float = 300
    lane: str = "streaming"


class JobRunner:
    """Execute, heartbeat, classify, and transition one claimed queue job.

    Queue claiming and finite/continuous stop behavior belong to the runtime.
    This class deliberately owns everything that must not vary by invocation
    surface once a job has been claimed.
    """

    def __init__(self, db: Any, handler: JobHandler):
        self.db = db
        self.handler = handler

    async def run(
        self,
        job: QueueJob,
        *,
        policy: JobExecutionPolicy,
        run_id: int | None = None,
    ) -> JobOutcome:
        if not job.claim_token:
            return JobOutcome.abandoned(
                f"queue job {job.id} has no claim token; handler was not started"
            )

        attempt_id: int | None = None
        stage_id: int | None = None
        heartbeat: asyncio.Task[None] | None = None
        claim_lost = asyncio.Event()
        outcome: JobOutcome | None = None
        claim_settled = False
        ownership_lost_during_execution = False
        cancelled = False
        service_started = time.monotonic()
        queue_wait_ms = _queue_wait_ms(
            job.ready_at or job.last_enqueued_at or job.created_at
        )
        desired_age_ms = _queue_wait_ms(job.last_enqueued_at or job.created_at)
        try:
            try:
                attempt = await self.db.pipeline_lifecycle.start_attempt(
                    queue_id=job.id,
                    run_id=run_id,
                    job_type=job.job_type,
                    lane=policy.lane,
                    banana=job.banana,
                    meeting_id=getattr(job.payload, "meeting_id", None),
                    matter_id=getattr(job.payload, "matter_id", None),
                    work_version=job.work_version,
                )
                attempt_id = int(attempt["id"])
                try:
                    stage_id = await self.db.pipeline_lifecycle.start_stage(
                        attempt_id=attempt_id,
                        run_id=run_id,
                        stage=f"{policy.lane}.execute",
                        metrics={
                            "queue_wait_ms": queue_wait_ms,
                            "desired_age_ms": desired_age_ms,
                        },
                    )
                except Exception:
                    stage_id = None
            except Exception as exc:
                outcome = JobOutcome.retryable_failure(
                    f"attempt journal unavailable: {type(exc).__name__}: {exc}"
                )
                retained = await self._transition(job, outcome)
                claim_settled = True
                if not retained:
                    outcome = JobOutcome.abandoned(
                        "queue claim was superseded before final transition",
                        outcome.stats,
                    )
                return outcome

            heartbeat = asyncio.create_task(
                self._heartbeat(
                    job,
                    attempt_id,
                    policy.heartbeat_seconds,
                    claim_lost,
                )
            )
            try:
                raw = await self._execute_until_claim_loss(
                    job,
                    claim_lost,
                    timeout=policy.timeout_seconds,
                )
                outcome = (
                    raw
                    if isinstance(raw, JobOutcome)
                    else JobOutcome.from_stats(raw)
                )
            except _QueueClaimLost as exc:
                ownership_lost_during_execution = True
                claim_settled = True
                outcome = JobOutcome.abandoned(str(exc))
            except asyncio.TimeoutError:
                outcome = JobOutcome.retryable_failure(
                    f"job exceeded {policy.timeout_seconds:g}s wall-clock timeout"
                )
            except SupersededWorkError as exc:
                outcome = JobOutcome.abandoned(exc)
            except TerminalJobError as exc:
                outcome = JobOutcome.terminal_failure(exc)
            except (ProcessingError, LLMError, ExtractionError) as exc:
                outcome = JobOutcome.retryable_failure(exc)
            except Exception as exc:
                outcome = JobOutcome.retryable_failure(exc)

            metrics = dict(outcome.stats)
            metrics["queue_wait_ms"] = queue_wait_ms
            metrics["desired_age_ms"] = desired_age_ms
            metrics["service_ms"] = int((time.monotonic() - service_started) * 1000)
            outcome = JobOutcome(
                status=outcome.status,
                stats=metrics,
                error=outcome.error,
                error_type=outcome.error_type,
            )

            if ownership_lost_during_execution:
                return outcome

            try:
                retained = await self._transition(job, outcome)
                claim_settled = True
                if not retained:
                    outcome = JobOutcome.abandoned(
                        "queue claim was superseded before final transition",
                        outcome.stats,
                    )
            except Exception as exc:
                outcome = JobOutcome.retryable_failure(
                    f"queue transition failed: {type(exc).__name__}: {exc}",
                    outcome.stats,
                )
            return outcome
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if not claim_settled and job.claim_token is not None:
                with suppress(Exception):
                    await self.db.queue.release_processing_claim(
                        job.id,
                        job.claim_token,
                        job.work_version,
                        error_message=(
                            "worker cancelled before completion"
                            if cancelled
                            else "worker exited before final queue transition"
                        ),
                    )
            if attempt_id is not None:
                if not cancelled and outcome is not None:
                    if stage_id is not None:
                        with suppress(Exception):
                            await self.db.pipeline_lifecycle.finish_stage(
                                stage_id,
                                status="succeeded" if outcome.is_success else "failed",
                                error_type=outcome.error_type,
                                error_message=outcome.error,
                                metrics=outcome.stats,
                            )
                    await self.db.pipeline_lifecycle.finish_attempt(
                        attempt_id,
                        status=outcome.status.value,
                        error_type=outcome.error_type,
                        error_message=outcome.error,
                        metrics=outcome.stats,
                    )
                else:
                    if stage_id is not None:
                        with suppress(Exception):
                            await self.db.pipeline_lifecycle.finish_stage(
                                stage_id,
                                status="failed",
                                error_type="CancelledError",
                                error_message="job execution cancelled",
                            )
                    await self.db.pipeline_lifecycle.finish_attempt(
                        attempt_id,
                        status="abandoned",
                        error_type="CancelledError",
                        error_message="job execution cancelled",
                    )

    async def _transition(self, job: QueueJob, outcome: JobOutcome) -> bool:
        if not job.claim_token:  # pragma: no cover - rejected at run boundary
            raise RuntimeError(f"queue job {job.id} has no claim token")
        if outcome.status is OutcomeStatus.SUCCEEDED:
            return await self.db.queue.mark_processing_complete(
                job.id,
                job.claim_token,
                job.work_version,
            )
        return await self.db.queue.mark_processing_failed(
            job.id,
            outcome.error or outcome.status.value,
            claim_token=job.claim_token,
            work_version=job.work_version,
            increment_retry=outcome.should_retry,
        )

    async def _heartbeat(
        self,
        job: QueueJob,
        attempt_id: int,
        interval: float,
        claim_lost: asyncio.Event,
    ) -> None:
        if job.claim_token is None:
            return
        while True:
            try:
                await asyncio.sleep(interval)
                retained = await self.db.queue.heartbeat_job(
                    job.id,
                    job.claim_token,
                    job.work_version,
                )
                if not retained:
                    claim_lost.set()
                    return
                await self.db.pipeline_lifecycle.heartbeat_attempt(attempt_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient telemetry write must not kill a healthy worker;
                # the next beat retries and stale reclamation remains the
                # ultimate safety net if the database stays unavailable.
                continue

    async def _execute_until_claim_loss(
        self,
        job: QueueJob,
        claim_lost: asyncio.Event,
        *,
        timeout: float,
    ) -> JobOutcome | Mapping[str, Any] | None:
        """Race useful work against timeout and definitive ownership loss."""
        handler = asyncio.ensure_future(self.handler(job))
        ownership = asyncio.create_task(claim_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {handler, ownership},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Prefer a concurrently completed handler. Its final transition is
            # still claim-token fenced, so this preserves completed work without
            # weakening ownership correctness.
            if handler in done:
                return await handler
            if ownership in done and claim_lost.is_set():
                raise _QueueClaimLost(
                    "queue claim ownership was lost during execution"
                )
            raise asyncio.TimeoutError
        finally:
            pending = [task for task in (handler, ownership) if not task.done()]
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError, Exception):
                    await task


def _queue_wait_ms(created_at: str | None) -> int:
    if not created_at:
        return 0
    try:
        created = datetime.fromisoformat(created_at)
        return max(0, int((datetime.now(created.tzinfo) - created).total_seconds() * 1000))
    except (TypeError, ValueError):
        return 0
