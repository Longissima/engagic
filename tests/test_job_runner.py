"""State-machine contracts for the shared CLI/daemon job runner."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline.job_runner import JobExecutionPolicy, JobRunner, TerminalJobError
from pipeline.models import MeetingJob, QueueJob
from pipeline.outcomes import JobOutcome, OutcomeStatus

CLAIM_TOKEN = "00000000-0000-0000-0000-000000000007"


class Lifecycle:
    def __init__(self):
        self.finished = []

    async def start_attempt(self, **kwargs):
        self.started = kwargs
        return {"id": 91}

    async def heartbeat_attempt(self, attempt_id):
        pass

    async def start_stage(self, **kwargs):
        self.stage_started = kwargs
        return 92

    async def finish_stage(self, stage_id, **kwargs):
        self.stage_finished = (stage_id, kwargs)

    async def finish_attempt(self, attempt_id, **kwargs):
        self.finished.append((attempt_id, kwargs))


class Queue:
    def __init__(self, retain_transition=True):
        self.transitions = []
        self.retain_transition = retain_transition

    async def mark_processing_complete(self, queue_id, claim_token, work_version):
        self.transitions.append(("complete", queue_id, claim_token, work_version))
        return self.retain_transition

    async def mark_processing_failed(
        self,
        queue_id,
        error,
        *,
        claim_token,
        work_version,
        increment_retry=True,
    ):
        self.transitions.append(
            ("failed", queue_id, error, increment_retry, claim_token, work_version)
        )
        return self.retain_transition

    async def heartbeat_job(self, queue_id, claim_token, work_version):
        pass

    async def release_processing_claim(
        self,
        queue_id,
        claim_token,
        work_version,
        *,
        error_message="worker cancelled before completion",
    ):
        self.transitions.append(
            ("release", queue_id, claim_token, work_version, error_message)
        )


def job() -> QueueJob:
    return QueueJob(
        id=7,
        job_type="meeting",
        payload=MeetingJob("meeting-1"),
        banana="exampleCA",
        priority=1,
        status="processing",
        work_version="mv1:abc",
        claim_token=CLAIM_TOKEN,
        last_enqueued_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        ready_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
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
    assert db.queue.transitions == [("complete", 7, CLAIM_TOKEN, "mv1:abc")]
    assert db.pipeline_lifecycle.started["work_version"] == "mv1:abc"
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "succeeded"
    assert db.pipeline_lifecycle.stage_started["stage"] == "streaming.execute"
    assert 0 <= db.pipeline_lifecycle.stage_started["metrics"]["queue_wait_ms"] < 5_000
    assert db.pipeline_lifecycle.stage_started["metrics"]["desired_age_ms"] >= 4_000
    assert db.pipeline_lifecycle.stage_finished[1]["metrics"]["service_ms"] >= 0


@pytest.mark.asyncio
async def test_tokenless_queue_job_never_starts_handler_or_transition():
    db = database()
    unowned = job()
    unowned.claim_token = None
    handler_entered = False

    async def handler(_job):
        nonlocal handler_entered
        handler_entered = True
        return {"items_new": 1}

    outcome = await JobRunner(db, handler).run(unowned, policy=POLICY)

    assert outcome.status is OutcomeStatus.ABANDONED
    assert "no claim token" in (outcome.error or "")
    assert handler_entered is False
    assert db.queue.transitions == []
    assert db.pipeline_lifecycle.finished == []


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
async def test_cancellation_releases_only_the_owned_claim():
    db = database()
    entered = asyncio.Event()

    async def handler(_job):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(JobRunner(db, handler).run(job(), policy=POLICY))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert db.queue.transitions == [
        (
            "release",
            7,
            CLAIM_TOKEN,
            "mv1:abc",
            "worker cancelled before completion",
        )
    ]
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "abandoned"


@pytest.mark.asyncio
async def test_cancellation_while_opening_attempt_releases_claim():
    db = database()
    entered = asyncio.Event()

    async def start_attempt(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    db.pipeline_lifecycle.start_attempt = start_attempt

    async def handler(_job):  # pragma: no cover - cancellation precedes handler
        return {}

    task = asyncio.create_task(JobRunner(db, handler).run(job(), policy=POLICY))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert db.queue.transitions[-1][-1] == "worker cancelled before completion"


@pytest.mark.asyncio
async def test_cancellation_during_final_transition_is_not_journaled_as_success():
    db = database()
    entered = asyncio.Event()

    async def blocked_complete(queue_id, claim_token, work_version):
        entered.set()
        await asyncio.Event().wait()

    db.queue.mark_processing_complete = blocked_complete

    async def handler(_job):
        return {"items_new": 1}

    task = asyncio.create_task(JobRunner(db, handler).run(job(), policy=POLICY))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert db.queue.transitions[-1][-1] == "worker cancelled before completion"
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "abandoned"


@pytest.mark.asyncio
async def test_transition_error_releases_claim_for_immediate_retry():
    db = database()

    async def failed_complete(queue_id, claim_token, work_version):
        raise RuntimeError("database unavailable")

    db.queue.mark_processing_complete = failed_complete

    async def handler(_job):
        return {"items_new": 1}

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY)

    assert outcome.status is OutcomeStatus.RETRYABLE_FAILURE
    assert db.queue.transitions[-1][-1] == (
        "worker exited before final queue transition"
    )
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "retryable_failure"


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


@pytest.mark.asyncio
async def test_superseded_claim_is_abandoned_not_journaled_as_success():
    db = database()
    db.queue = Queue(retain_transition=False)

    async def handler(_job):
        return {"items_new": 2, "items_failed": 0}

    outcome = await JobRunner(db, handler).run(job(), policy=POLICY)

    assert outcome.status is OutcomeStatus.ABANDONED
    assert outcome.is_success is False
    assert outcome.should_retry is False
    assert outcome.stats["items_new"] == 2
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "abandoned"
    assert db.pipeline_lifecycle.stage_finished[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_heartbeat_ownership_loss_cancels_handler_without_transition():
    db = database()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def lost_heartbeat(queue_id, claim_token, work_version):
        return False

    db.queue.heartbeat_job = lost_heartbeat

    async def handler(_job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    outcome = await JobRunner(db, handler).run(
        job(),
        policy=JobExecutionPolicy(timeout_seconds=1, heartbeat_seconds=0.001),
    )

    assert entered.is_set()
    assert cancelled.is_set()
    assert outcome.status is OutcomeStatus.ABANDONED
    assert "ownership was lost" in (outcome.error or "")
    assert db.queue.transitions == []
    assert db.pipeline_lifecycle.finished[0][1]["status"] == "abandoned"
    assert db.pipeline_lifecycle.stage_finished[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_transient_heartbeat_error_does_not_cancel_handler():
    db = database()
    heartbeat_recovered = asyncio.Event()
    heartbeat_calls = 0

    async def transient_heartbeat(queue_id, claim_token, work_version):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls == 1:
            raise RuntimeError("temporary database failure")
        heartbeat_recovered.set()
        return True

    db.queue.heartbeat_job = transient_heartbeat

    async def handler(_job):
        await heartbeat_recovered.wait()
        return {"items_new": 1}

    outcome = await JobRunner(db, handler).run(
        job(),
        policy=JobExecutionPolicy(timeout_seconds=1, heartbeat_seconds=0.001),
    )

    assert heartbeat_calls >= 2
    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert db.queue.transitions == [("complete", 7, CLAIM_TOKEN, "mv1:abc")]
