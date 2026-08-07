"""Focused contracts for queue re-enqueue and matter work identity."""

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from database.repositories_async.queue import QueueRepository
from pipeline.models import MatterJob, QueueJob, create_matter_job
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator


class _CaptureQueueRepository(QueueRepository):
    def __init__(self):
        self.calls = []

    async def _execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


class _FailureConnection:
    def __init__(self, row=None):
        self.row = row
        self.fetches = []
        self.executions = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        self.fetches.append((" ".join(query.split()), args))
        return self.row

    async def execute(self, query, *args):
        self.executions.append((" ".join(query.split()), args))
        return "UPDATE 1"


class _CaptureLifecycle:
    def __init__(self):
        self.calls = []

    async def enqueue_queue_job(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_enqueue_replaces_mutable_work_and_resets_attempt_lifecycle():
    repository = _CaptureQueueRepository()
    first_payload = {
        "matter_id": "alphaCA_ord-1",
        "meeting_id": "meeting-1",
    }
    changed_payload = {
        "matter_id": "alphaCA_ord-1",
        "meeting_id": "meeting-2",
    }

    await repository.enqueue_job(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload=first_payload,
        meeting_id="meeting-1",
        banana="alphaCA",
        priority=100,
        processing_metadata={"old": True},
        work_version="sv1:old",
    )
    await repository.enqueue_job(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload=changed_payload,
        meeting_id="meeting-2",
        banana="alphaCA",
        priority=180,
        processing_metadata=None,
        work_version="sv1:new",
    )

    query, args = repository.calls[-1]
    normalized = " ".join(query.split())
    assert args == (
        "matter://alphaCA_ord-1",
        "meeting-2",
        "alphaCA",
        "matter",
        changed_payload,
        180,
        None,
        "sv1:new",
    )
    for assignment in (
        "meeting_id = EXCLUDED.meeting_id",
        "banana = EXCLUDED.banana",
        "job_type = EXCLUDED.job_type",
        "payload = EXCLUDED.payload",
        "priority = EXCLUDED.priority",
        "retry_count = 0",
        "started_at = NULL",
        "completed_at = NULL",
        "failed_at = NULL",
        "error_message = NULL",
        "processing_metadata = COALESCE( EXCLUDED.processing_metadata, queue.processing_metadata )",
        "work_version = EXCLUDED.work_version",
        "retry_at = NULL",
        "last_enqueued_at = CURRENT_TIMESTAMP",
        "updated_at = CURRENT_TIMESTAMP",
    ):
        assert assignment in normalized


@pytest.mark.asyncio
async def test_versioned_pending_or_terminal_work_requires_a_distinct_version():
    repository = _CaptureQueueRepository()

    await repository.enqueue_job(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload={"matter_id": "alphaCA_ord-1"},
        work_version="sv1:new",
    )

    normalized = " ".join(repository.calls[0][0].split())
    assert "EXCLUDED.work_version IS NOT NULL" in normalized
    assert "queue.work_version IS DISTINCT FROM EXCLUDED.work_version" in normalized


@pytest.mark.asyncio
async def test_legacy_unversioned_work_preserves_reenqueue_except_active_claims():
    repository = _CaptureQueueRepository()

    await repository.enqueue_job(
        source_url="meeting://meeting-1",
        job_type="meeting",
        payload={"meeting_id": "meeting-1"},
    )

    normalized = " ".join(repository.calls[0][0].split())
    assert "EXCLUDED.work_version IS NULL" in normalized
    assert "queue.work_version IS NULL" in normalized
    assert "queue.status <> 'processing'" in normalized


@pytest.mark.asyncio
async def test_superseded_worker_cannot_complete_new_pending_version():
    repository = _CaptureQueueRepository()

    await repository.mark_processing_complete(17)

    normalized = " ".join(repository.calls[0][0].split())
    assert "WHERE id = $1 AND status = 'processing'" in normalized


@pytest.mark.asyncio
async def test_retryable_failure_returns_to_pending_with_lower_priority():
    connection = _FailureConnection(row={"retry_count": 0, "priority": 100})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(17, "temporary failure")

    query, args = connection.executions[-1]
    assert "status = 'pending'" in query
    assert "retry_count = retry_count + 1" in query
    assert "retry_at = NOW() + make_interval(secs => $4)" in query
    assert "WHERE id = $1 AND status = 'processing'" in query
    assert args == (17, 80, "temporary failure", 30)


@pytest.mark.asyncio
async def test_second_retry_uses_sixty_second_delay():
    connection = _FailureConnection(row={"retry_count": 1, "priority": 80})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(18, "temporary failure again")

    _query, args = connection.executions[-1]
    assert args == (18, 40, "temporary failure again", 60)


@pytest.mark.asyncio
async def test_exhausted_failure_moves_to_dead_letter():
    connection = _FailureConnection(row={"retry_count": 2, "priority": 60})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(19, "persistent failure")

    query, args = connection.executions[-1]
    assert "status = 'dead_letter'" in query
    assert "failed_at = NOW()" in query
    assert "WHERE id = $1 AND status = 'processing'" in query
    assert args == (19, "persistent failure")


@pytest.mark.asyncio
async def test_claim_paths_exclude_jobs_until_retry_at():
    connection = _FailureConnection(row=None)
    repository = QueueRepository(cast(Any, _Pool(connection)))

    assert await repository.get_next_job() is None
    assert await repository.get_next_for_processing() is None

    assert len(connection.fetches) == 2
    for query, _args in connection.fetches:
        assert "retry_at IS NULL OR" in query
        assert "retry_at <= NOW()" in query


@pytest.mark.asyncio
async def test_superseded_worker_failure_cannot_mutate_new_pending_version():
    connection = _FailureConnection(row=None)
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(20, "old worker failed")

    query, args = connection.fetches[-1]
    assert "WHERE id = $1 AND status = 'processing'" in query
    assert args == (20,)
    assert connection.executions == []


def test_matter_job_accepts_but_discards_legacy_item_id_snapshots():
    legacy = MatterJob.from_dict(
        {
            "matter_id": "alphaCA_ord-1",
            "meeting_id": "meeting-1",
            "item_ids": ["old-item", "current-item"],
        }
    )

    assert legacy.item_ids == []
    assert legacy.to_dict() == {
        "matter_id": "alphaCA_ord-1",
        "meeting_id": "meeting-1",
    }


def test_new_matter_job_serializes_identity_and_version_only():
    payload = create_matter_job(
        matter_id="alphaCA_ord-1",
        meeting_id="meeting-2",
        banana="alphaCA",
        priority=180,
        work_version="sv1:new",
    )

    assert json.loads(payload["payload"]) == {
        "matter_id": "alphaCA_ord-1",
        "meeting_id": "meeting-2",
    }
    assert "item_ids" not in json.loads(payload["payload"])
    assert payload["work_version"] == "sv1:new"


def test_queue_job_deserializes_new_matter_payload_without_item_ids():
    job = QueueJob.from_db_row(
        {
            "id": 1,
            "job_type": "matter",
            "payload": {
                "matter_id": "alphaCA_ord-1",
                "meeting_id": "meeting-2",
            },
            "banana": "alphaCA",
            "priority": 180,
            "status": "pending",
            "work_version": "sv1:new",
        }
    )

    assert isinstance(job.payload, MatterJob)
    assert job.payload.item_ids == []
    assert job.work_version == "sv1:new"


@pytest.mark.asyncio
async def test_matter_reenqueue_changes_version_without_snapshotting_appearances():
    lifecycle = _CaptureLifecycle()
    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(pipeline_lifecycle=lifecycle)
    )
    connection = object()
    first_date = datetime(2026, 1, 1)
    second_date = datetime(2026, 2, 1)

    await orchestrator._enqueue_matter_job(
        matter_id="alphaCA_ord-1",
        meeting_id="meeting-1",
        attachment_hash="sv1:old",
        banana="alphaCA",
        meeting_date=first_date,
        conn=cast(Any, connection),
    )
    await orchestrator._enqueue_matter_job(
        matter_id="alphaCA_ord-1",
        meeting_id="meeting-2",
        attachment_hash="sv1:new",
        banana="alphaCA",
        meeting_date=second_date,
        conn=cast(Any, connection),
    )

    assert len(lifecycle.calls) == 2
    assert lifecycle.calls[0]["source_url"] == lifecycle.calls[1]["source_url"]
    assert lifecycle.calls[1]["meeting_id"] == "meeting-2"
    assert lifecycle.calls[1]["payload"] == {
        "matter_id": "alphaCA_ord-1",
        "meeting_id": "meeting-2",
    }
    assert "item_ids" not in lifecycle.calls[1]["payload"]
    assert lifecycle.calls[1]["work_version"] == "sv1:new"
    assert lifecycle.calls[1]["conn"] is connection
