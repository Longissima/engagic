"""Focused contracts for queue re-enqueue and matter work identity."""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from database.repositories_async.queue import QueueRepository
from pipeline.models import MatterJob, QueueJob, create_matter_job
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.utils import matter_no_work_version

CLAIM_TOKEN = "00000000-0000-0000-0000-000000000017"


class _CaptureQueueRepository(QueueRepository):
    def __init__(self):
        self.calls = []

    async def _execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"

    async def _fetchrow(self, query, *args):
        self.calls.append((query, args))
        return {"id": 1}

    @asynccontextmanager
    async def transaction(self):
        repository = self

        class Connection:
            async def execute(self, query, *args):
                return await repository._execute(query, *args)

            async def fetchrow(self, query, *args):
                return await repository._fetchrow(query, *args)

        yield cast(Any, Connection())


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


class _PreviewQueueRepository(QueueRepository):
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def _fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.rows


class _QualityQueueRepository(QueueRepository):
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def _fetchrow(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.row


class _ClaimConnection(_FailureConnection):
    def __init__(self, selected, claimed):
        super().__init__()
        self.responses = [selected, claimed]

    async def fetchrow(self, query, *args):
        self.fetches.append((" ".join(query.split()), args))
        return self.responses.pop(0)


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

    first_accepted = await repository.enqueue_job(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload=first_payload,
        meeting_id="meeting-1",
        banana="alphaCA",
        priority=100,
        processing_metadata={"old": True},
        work_version="sv1:old",
    )
    accepted = await repository.enqueue_job(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload=changed_payload,
        meeting_id="meeting-2",
        banana="alphaCA",
        priority=180,
        processing_metadata=None,
        work_version="sv1:new",
    )

    assert first_accepted is True
    assert accepted is True

    query, args = repository.calls[-1]
    normalized = " ".join(query.split())
    assert "pg_advisory_xact_lock" in normalized
    assert "'queue-intent:' || $1" in normalized
    assert args == (
        "matter://alphaCA_ord-1",
        "meeting-2",
        "alphaCA",
        "matter",
        changed_payload,
        180,
        None,
        "sv1:new",
        None,
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
        "desired_generation = EXCLUDED.desired_generation",
        "retry_at = NULL",
        "claim_token = NULL",
        "claimed_at = NULL",
        "heartbeat_at = NULL",
        "last_enqueued_at = CURRENT_TIMESTAMP",
        "ready_at = CURRENT_TIMESTAMP",
        "updated_at = CURRENT_TIMESTAMP",
    ):
        assert assignment in normalized


@pytest.mark.asyncio
async def test_desired_state_lock_acquires_source_before_queue_row() -> None:
    connection = _FailureConnection(
        row={
            "status": "completed",
            "work_version": "mw1:current",
            "desired_generation": 41,
            "claim_token": None,
        }
    )
    repository = QueueRepository(cast(Any, _Pool(connection)))

    state = await repository.lock_desired_state(
        "matter://alphaCA_ord-1",
        conn=cast(Any, connection),
    )

    assert state == {
        "status": "completed",
        "work_version": "mw1:current",
        "desired_generation": 41,
        "claim_token": None,
    }
    assert len(connection.executions) == 1
    advisory_query, advisory_args = connection.executions[0]
    row_query, row_args = connection.fetches[0]
    assert "pg_advisory_xact_lock" in advisory_query
    assert "'queue-intent:' || $1" in advisory_query
    assert "FOR UPDATE" in row_query
    assert "claim_token" in row_query
    assert advisory_args == row_args == ("matter://alphaCA_ord-1",)


@pytest.mark.asyncio
async def test_no_work_tombstone_is_terminal_generation_fenced_and_claim_fencing() -> None:
    connection = _FailureConnection(row={"id": 17})
    repository = QueueRepository(cast(Any, _Pool(connection)))
    tombstone = matter_no_work_version("mw1:empty", "no_appearances")

    accepted = await repository.invalidate_desired_work(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload={
            "matter_id": "alphaCA_ord-1",
            "no_work_reason": "no_appearances",
        },
        work_version=tombstone,
        banana="alphaCA",
        desired_generation=42,
        conn=cast(Any, connection),
    )

    assert accepted is True
    query, args = connection.fetches[0]
    for contract in (
        "pg_advisory_xact_lock",
        "'queue-intent:' || $1",
        "status = 'completed'",
        "EXCLUDED.desired_generation > queue.desired_generation",
        "queue.status = 'completed'",
        "queue.work_version IS NOT DISTINCT FROM EXCLUDED.work_version",
        "FROM pipeline_outbox intervening",
        "intervening.payload->>'work_version' IS DISTINCT FROM EXCLUDED.work_version",
        "intervening.work_generation > queue.desired_generation",
        "intervening.work_generation < EXCLUDED.desired_generation",
        "claim_token = NULL",
        "claimed_at = NULL",
        "heartbeat_at = NULL",
        "retry_count = 0",
        "retry_at = NULL",
        "last_enqueued_at = CURRENT_TIMESTAMP",
        "RETURNING id",
    ):
        assert contract in query
    assert "processing_metadata = NULL" not in query
    assert args == (
        "matter://alphaCA_ord-1",
        None,
        "alphaCA",
        "matter",
        {
            "matter_id": "alphaCA_ord-1",
            "no_work_reason": "no_appearances",
        },
        tombstone,
        42,
    )


@pytest.mark.asyncio
async def test_no_work_tombstone_reports_same_terminal_version_as_noop() -> None:
    connection = _FailureConnection(row=None)
    repository = QueueRepository(cast(Any, _Pool(connection)))
    tombstone = matter_no_work_version("mw1:empty", "no_appearances")

    accepted = await repository.invalidate_desired_work(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload={
            "matter_id": "alphaCA_ord-1",
            "no_work_reason": "no_appearances",
        },
        work_version=tombstone,
        conn=cast(Any, connection),
    )

    assert accepted is False


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [0, -1, True, 1.5, "2"])
async def test_no_work_tombstone_rejects_invalid_generation(generation) -> None:
    connection = _FailureConnection(row={"id": 17})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    with pytest.raises(ValueError, match="desired_generation"):
        await repository.invalidate_desired_work(
            source_url="matter://alphaCA_ord-1",
            job_type="matter",
            payload={"matter_id": "alphaCA_ord-1"},
            work_version="mw1:empty",
            desired_generation=generation,
            conn=cast(Any, connection),
        )


@pytest.mark.asyncio
async def test_explicit_retry_reactivates_only_the_exact_terminal_version():
    repository = _CaptureQueueRepository()

    assert await repository.reactivate_job_version(
        source_url="meeting://meeting-1",
        work_version="mv1:current",
        priority=100,
    )

    query, args = repository.calls[0]
    normalized = " ".join(query.split())
    assert "work_version IS NOT DISTINCT FROM $2" in normalized
    assert "status IN ('completed', 'failed', 'dead_letter', 'processing')" in normalized
    assert args == ("meeting://meeting-1", "mv1:current", 100)


@pytest.mark.asyncio
async def test_reactivation_invalidates_an_active_claim_before_follow_up():
    repository = _CaptureQueueRepository()

    assert await repository.reactivate_job_version(
        source_url="meeting://meeting-1",
        work_version="mv1:current",
        priority=200,
    )
    await repository.mark_processing_complete(17, CLAIM_TOKEN, "mv1:current")

    reactivate_query = " ".join(repository.calls[0][0].split())
    completion_query = " ".join(repository.calls[1][0].split())
    assert "status = 'pending'" in reactivate_query
    assert "claim_token = NULL" in reactivate_query
    assert "status IN ('completed', 'failed', 'dead_letter', 'processing')" in reactivate_query
    assert "claim_token = $2::uuid" in completion_query
    assert repository.calls[1][1] == (17, CLAIM_TOKEN, "mv1:current")


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
    assert "EXCLUDED.desired_generation > queue.desired_generation" in normalized
    assert "EXCLUDED.work_version IS NOT NULL" in normalized
    assert "queue.work_version IS DISTINCT FROM EXCLUDED.work_version" in normalized
    assert "RETURNING id" in normalized


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
async def test_enqueue_forwards_outbox_generation_without_allocating_a_new_one():
    repository = _CaptureQueueRepository()

    await repository.enqueue_job(
        source_url="meeting://meeting-1",
        job_type="meeting",
        payload={"meeting_id": "meeting-1"},
        work_version="mv1:current",
        desired_generation=91,
    )

    query, args = repository.calls[0]
    normalized = " ".join(query.split())
    assert "COALESCE($9, nextval('pipeline_work_generation_seq'))" in normalized
    assert "EXCLUDED.desired_generation > queue.desired_generation" in normalized
    assert args[-1] == 91


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [0, -1, True, 1.5, "2"])
async def test_enqueue_rejects_invalid_explicit_generation(generation):
    repository = _CaptureQueueRepository()

    with pytest.raises(ValueError, match="desired_generation"):
        await repository.enqueue_job(
            source_url="meeting://meeting-1",
            job_type="meeting",
            payload={"meeting_id": "meeting-1"},
            desired_generation=generation,
        )

    assert repository.calls == []


@pytest.mark.asyncio
async def test_superseded_worker_cannot_complete_new_pending_version():
    repository = _CaptureQueueRepository()

    await repository.mark_processing_complete(17, CLAIM_TOKEN, "sv1:old")

    normalized = " ".join(repository.calls[0][0].split())
    assert "claim_token = $2::uuid" in normalized
    assert "work_version IS NOT DISTINCT FROM $3" in normalized
    assert repository.calls[0][1] == (17, CLAIM_TOKEN, "sv1:old")


@pytest.mark.asyncio
async def test_retryable_failure_returns_to_pending_with_lower_priority():
    connection = _FailureConnection(row={"retry_count": 0, "priority": 100})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(
        17,
        "temporary failure",
        claim_token=CLAIM_TOKEN,
        work_version="sv1:current",
    )

    query, args = connection.executions[-1]
    assert "status = 'pending'" in query
    assert "retry_count = retry_count + 1" in query
    assert "retry_at = NOW() + make_interval(secs => $4)" in query
    assert "ready_at = NOW() + make_interval(secs => $4)" in query
    assert "claim_token = $5::uuid" in query
    assert "work_version IS NOT DISTINCT FROM $6" in query
    assert args == (
        17,
        80,
        "temporary failure",
        30,
        CLAIM_TOKEN,
        "sv1:current",
    )


@pytest.mark.asyncio
async def test_second_retry_uses_sixty_second_delay():
    connection = _FailureConnection(row={"retry_count": 1, "priority": 80})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(
        18,
        "temporary failure again",
        claim_token=CLAIM_TOKEN,
        work_version="sv1:current",
    )

    _query, args = connection.executions[-1]
    assert args == (
        18,
        40,
        "temporary failure again",
        60,
        CLAIM_TOKEN,
        "sv1:current",
    )


@pytest.mark.asyncio
async def test_exhausted_failure_moves_to_dead_letter():
    connection = _FailureConnection(row={"retry_count": 2, "priority": 60})
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(
        19,
        "persistent failure",
        claim_token=CLAIM_TOKEN,
        work_version="sv1:current",
    )

    query, args = connection.executions[-1]
    assert "status = 'dead_letter'" in query
    assert "failed_at = NOW()" in query
    assert "claim_token = $3::uuid" in query
    assert "work_version IS NOT DISTINCT FROM $4" in query
    assert args == (19, "persistent failure", CLAIM_TOKEN, "sv1:current")


@pytest.mark.asyncio
async def test_claim_paths_exclude_jobs_until_retry_at():
    connection = _FailureConnection(row=None)
    repository = QueueRepository(cast(Any, _Pool(connection)))

    assert await repository.get_next_for_processing() is None

    assert len(connection.fetches) == 1
    for query, _args in connection.fetches:
        assert "retry_at IS NULL OR" in query
        assert "retry_at <= NOW()" in query


@pytest.mark.asyncio
async def test_superseded_worker_failure_cannot_mutate_new_pending_version():
    connection = _FailureConnection(row=None)
    repository = QueueRepository(cast(Any, _Pool(connection)))

    await repository.mark_processing_failed(
        20,
        "old worker failed",
        claim_token=CLAIM_TOKEN,
        work_version="sv1:old",
    )

    query, args = connection.fetches[-1]
    assert "claim_token = $2::uuid" in query
    assert "work_version IS NOT DISTINCT FROM $3" in query
    assert args == (20, CLAIM_TOKEN, "sv1:old")
    assert connection.executions == []


@pytest.mark.asyncio
async def test_typed_claim_gets_a_fresh_token_and_stable_claim_timestamps():
    claim_time = datetime(2026, 8, 7, 12, 0)
    selected = {
        "id": 22,
        "source_url": "meeting://meeting-1",
        "meeting_id": "meeting-1",
        "banana": "alphaCA",
        "job_type": "meeting",
        "payload": {"meeting_id": "meeting-1"},
        "priority": 100,
        "retry_count": 0,
        "status": "pending",
        "created_at": datetime(2026, 8, 1),
        "started_at": None,
        "work_version": None,
        "last_enqueued_at": datetime(2026, 8, 7, 11, 59),
        "ready_at": datetime(2026, 8, 7, 11, 59, 30),
    }
    connection = _ClaimConnection(
        selected,
        {"claimed_at": claim_time, "heartbeat_at": claim_time, "started_at": claim_time},
    )
    repository = QueueRepository(cast(Any, _Pool(connection)))

    job = await repository.get_next_for_processing()

    assert job is not None
    assert UUID(job.claim_token or "").version == 4
    assert job.work_version is None
    assert job.claimed_at == claim_time.isoformat()
    assert job.last_enqueued_at == datetime(2026, 8, 7, 11, 59).isoformat()
    assert job.ready_at == datetime(2026, 8, 7, 11, 59, 30).isoformat()
    select_query, _ = connection.fetches[0]
    claim_query, claim_args = connection.fetches[1]
    assert "ORDER BY q.priority DESC, q.last_enqueued_at ASC, q.id ASC" in select_query
    assert "claim_token = $2::uuid" in claim_query
    assert "claimed_at = NOW()" in claim_query
    assert "heartbeat_at = NOW()" in claim_query
    assert claim_args[0] == 22
    assert claim_args[1] == job.claim_token


@pytest.mark.asyncio
async def test_heartbeat_renews_only_owned_version_without_moving_claim_start():
    repository = _CaptureQueueRepository()

    assert await repository.heartbeat_job(22, CLAIM_TOKEN, None)

    query, args = repository.calls[0]
    normalized = " ".join(query.split())
    assert "SET heartbeat_at = NOW()" in normalized
    assert "claimed_at = NOW()" not in normalized
    assert "started_at = NOW()" not in normalized
    assert "claim_token = $2::uuid" in normalized
    assert "work_version IS NOT DISTINCT FROM $3" in normalized
    assert args == (22, CLAIM_TOKEN, None)


@pytest.mark.asyncio
async def test_preview_jobs_is_a_pure_ordered_select_returning_typed_jobs():
    repository = _PreviewQueueRepository(
        [
            {
                "id": 31,
                "source_url": "matter://alphaCA_ord-1",
                "meeting_id": None,
                "banana": "alphaCA",
                "job_type": "matter",
                "payload": {"matter_id": "alphaCA_ord-1"},
                "priority": 180,
                "retry_count": 0,
                "status": "pending",
                "error_message": None,
                "created_at": datetime(2026, 8, 1),
                "started_at": None,
                "completed_at": None,
                "work_version": "sv1:current",
                "last_enqueued_at": datetime(2026, 8, 7),
                "ready_at": datetime(2026, 8, 7, 0, 1),
                "claim_token": None,
                "claimed_at": None,
                "heartbeat_at": None,
            }
        ]
    )

    jobs = await repository.preview_jobs(banana="alphaCA", limit=5)

    assert len(jobs) == 1
    assert isinstance(jobs[0], QueueJob)
    assert isinstance(jobs[0].payload, MatterJob)
    assert jobs[0].payload.meeting_id is None
    assert jobs[0].ready_at == datetime(2026, 8, 7, 0, 1).isoformat()
    query, args = repository.calls[0]
    assert query.lstrip().startswith("SELECT")
    assert "UPDATE" not in query
    assert "FOR UPDATE" not in query
    assert "ORDER BY priority DESC, last_enqueued_at ASC, id ASC" in query
    assert args == ("alphaCA", 5)


@pytest.mark.asyncio
async def test_chunker_hints_choose_the_latest_enqueued_audit():
    repository = _PreviewQueueRepository([])

    assert await repository.get_chunker_hints() == []

    query, args = repository.calls[0]
    assert "q.last_enqueued_at DESC NULLS LAST" in query
    assert "q.updated_at DESC NULLS LAST" in query
    assert "q.id DESC" in query
    assert "q.created_at DESC" not in query
    assert args == ()


@pytest.mark.asyncio
async def test_chunk_quality_chooses_the_latest_enqueued_audit():
    quality = {"seg_smell": "healthy"}
    repository = _QualityQueueRepository({"quality": quality})

    assert await repository.get_chunk_quality("meeting-1") == quality

    query, args = repository.calls[0]
    assert "last_enqueued_at DESC NULLS LAST" in query
    assert "updated_at DESC NULLS LAST" in query
    assert "id DESC" in query
    assert "created_at DESC" not in query
    assert args == ("meeting-1",)


def test_matter_job_accepts_but_discards_legacy_item_id_snapshots():
    legacy = MatterJob.from_dict(
        {
            "matter_id": "alphaCA_ord-1",
            "meeting_id": "meeting-1",
            "item_ids": ["old-item", "current-item"],
        }
    )

    assert legacy.item_ids == []
    assert legacy.meeting_id == "meeting-1"
    assert legacy.to_dict() == {"matter_id": "alphaCA_ord-1"}


def test_new_matter_job_serializes_identity_and_version_only():
    payload = create_matter_job(
        matter_id="alphaCA_ord-1",
        meeting_id="meeting-2",
        banana="alphaCA",
        priority=180,
        work_version="mw1:new",
    )

    assert payload["payload"] == {"matter_id": "alphaCA_ord-1"}
    assert "item_ids" not in payload["payload"]
    assert payload["work_version"] == "mw1:new"


def test_queue_job_deserializes_new_matter_payload_without_item_ids():
    job = QueueJob.from_db_row(
        {
            "id": 1,
            "job_type": "matter",
            "payload": {"matter_id": "alphaCA_ord-1"},
            "banana": "alphaCA",
            "priority": 180,
            "status": "pending",
            "work_version": "sv1:new",
        }
    )

    assert isinstance(job.payload, MatterJob)
    assert job.payload.item_ids == []
    assert job.payload.meeting_id is None
    assert job.work_version == "sv1:new"


def test_queue_claim_migration_and_schema_are_mirrored():
    root = Path(__file__).parents[1]
    migration = (root / "database/migrations/033_queue_claim_ownership.sql").read_text()
    rollback = (
        root / "database/migrations/033_queue_claim_ownership.down.sql"
    ).read_text()
    schema = (root / "database/schema_postgres.sql").read_text()

    for column in (
        "claim_token UUID",
        "claimed_at TIMESTAMP",
        "heartbeat_at TIMESTAMP",
        "ready_at TIMESTAMP",
    ):
        assert column in migration
        assert column in schema
    assert "WHERE status = 'processing'" in migration
    assert "DROP COLUMN IF EXISTS claim_token" in rollback
    assert "DROP COLUMN IF EXISTS ready_at" in rollback
    assert "idx_queue_claim_heartbeat" in migration
    assert "idx_queue_claim_heartbeat" in schema


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
        work_version="mw1:old",
        banana="alphaCA",
        meeting_date=first_date,
        conn=cast(Any, connection),
    )
    await orchestrator._enqueue_matter_job(
        matter_id="alphaCA_ord-1",
        work_version="mw1:new",
        banana="alphaCA",
        meeting_date=second_date,
        conn=cast(Any, connection),
    )

    assert len(lifecycle.calls) == 2
    assert lifecycle.calls[0]["source_url"] == lifecycle.calls[1]["source_url"]
    assert lifecycle.calls[1]["meeting_id"] is None
    assert lifecycle.calls[1]["payload"] == {"matter_id": "alphaCA_ord-1"}
    assert "item_ids" not in lifecycle.calls[1]["payload"]
    assert lifecycle.calls[1]["work_version"] == "mw1:new"
    assert lifecycle.calls[1]["conn"] is connection
