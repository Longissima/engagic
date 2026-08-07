"""State-machine contracts for the shared CLI/daemon job runner."""

from types import SimpleNamespace

import pytest

from pipeline.job_runner import JobExecutionPolicy, JobRunner, TerminalJobError
from pipeline.models import MeetingJob, QueueJob
from pipeline.outcomes import JobOutcome, OutcomeStatus


class Lifecycle:
    def __init__(self):
        self.finished = []

    async def start_attempt(self, **kwargs):
        self.started = kwargs
        return {"id": 91}

    async def heartbeat_attempt(self, attempt_id):
        pass

    async def finish_attempt(self, attempt_id, **kwargs):
        self.finished.append((attempt_id, kwargs))


class Queue:
    def __init__(self):
        self.transitions = []

    async def mark_processing_complete(self, queue_id):
        self.transitions.append(("complete", queue_id))

    async def mark_processing_failed(self, queue_id, error, increment_retry=True):
        self.transitions.append(("failed", queue_id, error, increment_retry))

    async def heartbeat_job(self, queue_id):
        pass


def job() -> QueueJob:
    return QueueJob(
        id=7,
        job_type="meeting",
        payload=MeetingJob("meeting-1"),
        banana="exampleCA",
        priority=1,
        status="processing",
        work_version="mv1:abc",
    )


def database():
    return SimpleNamespace(queue=Queue(), pipeline_lifecycle=Lifecycle())


POLICY = JobExecutionPolicy(timeout_seconds=0.05, heartbeat_seconds=60)


@pytest.mark.asyncio
async def test_success_records_attempt_and_completes_queue():
    db = database()

    async def handler(_job):
        return {"items_new": 2, "items_failed": 0}

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY, run_id=5)

    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert db.queue.transitions == [("complete", 7)]
    assert db.pipeline_lifecycle.started["work_version"] == "mv1:abc"
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_partial_result_is_retryable_in_every_surface():
    db = database()

    async def handler(_job):
        return {"items_new": 1, "items_failed": 1}

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY)

    assert outcome.status is OutcomeStatus.PARTIAL
    assert db.queue.transitions[0][0] == "failed"
    assert db.queue.transitions[0][3] is True
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "partial"


@pytest.mark.asyncio
async def test_terminal_descriptor_failure_does_not_retry():
    db = database()

    async def handler(_job):
        raise TerminalJobError("meeting no longer exists")

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY)

    assert outcome.status is OutcomeStatus.TERMINAL_FAILURE
    assert db.queue.transitions[0][3] is False


@pytest.mark.asyncio
async def test_timeout_is_durable_retryable_failure():
    db = database()

    async def handler(_job):
        import asyncio

        await asyncio.sleep(1)

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY)

    assert outcome.status is OutcomeStatus.RETRYABLE_FAILURE
    assert "wall-clock timeout" in (outcome.error or "")
    assert db.queue.transitions[0][3] is True


@pytest.mark.asyncio
async def test_explicit_outcome_passes_through_without_reclassification():
    db = database()

    async def handler(_job):
        return JobOutcome.terminal_failure("invalid desired version")

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY)

    assert outcome.status is OutcomeStatus.TERMINAL_FAILURE
    assert db.queue.transitions[0][3] is False
