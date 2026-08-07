"""One queue-job execution policy for CLI and daemon runtimes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from exceptions import ExtractionError, LLMError, ProcessingError
from pipeline.models import QueueJob
from pipeline.outcomes import JobOutcome, OutcomeStatus


class TerminalJobError(RuntimeError):
    """The claimed descriptor cannot succeed without different desired work."""


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
        attempt_id: int | None = None
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
        except Exception as exc:
            outcome = JobOutcome.retryable_failure(
                f"attempt journal unavailable: {type(exc).__name__}: {exc}"
            )
            await self._transition(job.id, outcome)
            return outcome

        heartbeat = asyncio.create_task(
            self._heartbeat(job.id, attempt_id, policy.heartbeat_seconds)
        )
        try:
            try:
                raw = await asyncio.wait_for(
                    self.handler(job), timeout=policy.timeout_seconds
                )
                outcome = (
                    raw
                    if isinstance(raw, JobOutcome)
                    else JobOutcome.from_stats(raw)
                )
            except asyncio.TimeoutError:
                outcome = JobOutcome.retryable_failure(
                    f"job exceeded {policy.timeout_seconds:g}s wall-clock timeout"
                )
            except TerminalJobError as exc:
                outcome = JobOutcome.terminal_failure(exc)
            except (ProcessingError, LLMError, ExtractionError) as exc:
                outcome = JobOutcome.retryable_failure(exc)
            except Exception as exc:
                outcome = JobOutcome.retryable_failure(exc)

            try:
                await self._transition(job.id, outcome)
            except Exception as exc:
                outcome = JobOutcome.retryable_failure(
                    f"queue transition failed: {type(exc).__name__}: {exc}",
                    outcome.stats,
                )
            return outcome
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            if attempt_id is not None:
                # The outcome is always assigned before this point unless the
                # task itself was cancelled. Cancellation is append-only too.
                if "outcome" in locals():
                    await self.db.pipeline_lifecycle.finish_attempt(
                        attempt_id,
                        status=outcome.status.value,
                        error_type=outcome.error_type,
                        error_message=outcome.error,
                        metrics=outcome.stats,
                    )
                else:
                    await self.db.pipeline_lifecycle.finish_attempt(
                        attempt_id,
                        status="abandoned",
                        error_type="CancelledError",
                        error_message="job execution cancelled",
                    )

    async def _transition(self, queue_id: int, outcome: JobOutcome) -> None:
        if outcome.status is OutcomeStatus.SUCCEEDED:
            await self.db.queue.mark_processing_complete(queue_id)
            return
        await self.db.queue.mark_processing_failed(
            queue_id,
            outcome.error or outcome.status.value,
            increment_retry=outcome.should_retry,
        )

    async def _heartbeat(
        self, queue_id: int, attempt_id: int, interval: float
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.db.queue.heartbeat_job(queue_id)
            await self.db.pipeline_lifecycle.heartbeat_attempt(attempt_id)
