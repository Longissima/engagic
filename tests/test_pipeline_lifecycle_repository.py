from pathlib import Path

import pytest

from database.repositories_async.pipeline_lifecycle import PipelineLifecycleRepository


class FakeLifecycleRepository(PipelineLifecycleRepository):
    def __init__(self):
        self.fetchrow_calls = []
        self.execute_calls = []
        self.next_row = {"id": 7, "run_key": "run-1", "started_at": "now"}

    async def _fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.next_row

    async def _execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_start_run_records_scope_and_process_identity():
    repo = FakeLifecycleRepository()

    run = await repo.start_run(
        "process", targets=["alphaCA", "betaWA"], run_key="run-1",
        metadata={"surface": "cli"},
    )

    assert run["id"] == 7
    _, args = repo.fetchrow_calls[0]
    assert args[0] == "run-1"
    assert args[1] == "process"
    assert args[2] == ["alphaCA", "betaWA"]
    assert args[5] == {"surface": "cli"}


@pytest.mark.asyncio
async def test_attempt_history_is_appended_for_queue_identity():
    repo = FakeLifecycleRepository()
    repo.next_row = {"id": 12, "attempt_number": 3, "started_at": "now"}

    attempt = await repo.start_attempt(
        queue_id=44,
        run_id=7,
        job_type="matter",
        lane="streaming",
        banana="alphaCA",
        meeting_id="meeting-1",
        matter_id="alphaCA_matter",
        work_version="sv1:abc",
    )

    assert attempt["attempt_number"] == 3
    query, args = repo.fetchrow_calls[0]
    assert "MAX(attempt_number)" in query
    assert args == (
        44, 7, "matter", "streaming", "alphaCA", "meeting-1",
        "alphaCA_matter", "sv1:abc",
    )


@pytest.mark.asyncio
async def test_finish_attempt_persists_semantic_outcome_and_metrics():
    repo = FakeLifecycleRepository()

    await repo.finish_attempt(
        12,
        status="partial",
        error_type="ExtractionError",
        error_message="one attachment failed",
        metrics={"items_failed": 1, "items_new": 2},
    )

    query, args = repo.execute_calls[0]
    assert "completed_at = NOW()" in query
    assert "status = 'running'" in query
    assert args == (
        12, "partial", "ExtractionError", "one attachment failed",
        {"items_failed": 1, "items_new": 2},
    )


@pytest.mark.asyncio
async def test_failed_outbox_publication_gets_time_based_retry():
    repo = FakeLifecycleRepository()

    await repo.finish_outbox(
        91,
        lease_owner="publisher-1",
        claim_token="00000000-0000-0000-0000-000000000091",
        succeeded=False,
        error_message="database unavailable",
        retry_seconds=120,
    )

    query, args = repo.fetchrow_calls[0]
    assert "make_interval" in query
    assert "lease_owner = $4" in query
    assert "claim_token = $5::uuid" in query
    assert args == (
        91,
        "database unavailable",
        120,
        "publisher-1",
        "00000000-0000-0000-0000-000000000091",
    )


@pytest.mark.asyncio
async def test_outbox_completion_requires_the_current_lease_owner():
    repo = FakeLifecycleRepository()

    await repo.finish_outbox(
        91,
        lease_owner="publisher-1",
        claim_token="00000000-0000-0000-0000-000000000091",
        succeeded=True,
    )

    query, args = repo.fetchrow_calls[0]
    assert "lease_owner = $2" in query
    assert "claim_token = $3::uuid" in query
    assert args == (
        91,
        "publisher-1",
        "00000000-0000-0000-0000-000000000091",
    )


@pytest.mark.asyncio
async def test_dead_letter_replay_requires_explicit_stable_event_identity():
    repo = FakeLifecycleRepository()

    assert await repo.reactivate_outbox("queue.enqueue:meeting://m-1:mv1:a")

    query, args = repo.fetchrow_calls[0]
    normalized = " ".join(query.split())
    assert "status IN ('failed', 'dead_letter')" in normalized
    assert "attempt_count = 0" in normalized
    assert "lease_owner = NULL" in normalized
    assert "event_type <> 'queue.enqueue'" in normalized
    assert "fulfilled.source_url" in normalized
    assert "newer_queue.desired_generation > candidate.work_generation" in normalized
    assert "newer_event.work_generation > candidate.work_generation" in normalized
    assert args == ("queue.enqueue:meeting://m-1:mv1:a",)


@pytest.mark.asyncio
async def test_scoped_outbox_activity_is_one_retry_aware_snapshot():
    repo = FakeLifecycleRepository()
    repo.next_row = {
        "active": 3,
        "dead_letter": 2,
        "ready": 1,
        "next_attempt_at": "later",
    }

    activity = await repo.get_outbox_activity(
        event_type="queue.enqueue",
        bananas=["alphaCA", "alphaCA"],
    )

    assert activity == {
        "active": 3,
        "dead_letter": 2,
        "ready": 1,
        "next_attempt_at": "later",
    }
    query, args = repo.fetchrow_calls[0]
    normalized = " ".join(query.split())
    assert "AS next_attempt_at" in normalized
    assert "lease_expires_at" in normalized
    assert "earlier.aggregate_id = po.aggregate_id" in normalized
    assert "earlier.work_generation < po.work_generation" in normalized
    assert "fulfilled.source_url" in normalized
    assert "newer_queue.desired_generation > candidate.work_generation" in normalized
    assert "newer_event.work_generation > candidate.work_generation" in normalized
    assert args == ("queue.enqueue", ["alphaCA"])


@pytest.mark.asyncio
async def test_outbox_count_adapters_share_the_activity_read():
    repo = FakeLifecycleRepository()
    repo.next_row = {
        "active": 3,
        "dead_letter": 2,
        "ready": 0,
        "next_attempt_at": None,
    }

    assert await repo.count_active_outbox() == 3
    assert await repo.count_dead_letter_outbox() == 2


@pytest.mark.asyncio
async def test_aggregate_outbox_gate_uses_canonical_current_intent_predicate():
    class Connection:
        def __init__(self):
            self.call = None

        async def fetchval(self, query, *args):
            self.call = (" ".join(query.split()), args)
            return True

    connection = Connection()
    repo = FakeLifecycleRepository()

    active = await repo.has_unresolved_outbox_for_aggregate(
        event_type="queue.enqueue",
        aggregate_type="matter",
        aggregate_id="alphaCA_ord-1",
        conn=connection,
    )

    assert active is True
    assert connection.call is not None
    query, args = connection.call
    assert "newer_queue.desired_generation > candidate.work_generation" in query
    assert "newer_event.work_generation > candidate.work_generation" in query
    assert "candidate.status IN" in query
    assert args == ("queue.enqueue", "matter", "alphaCA_ord-1")


def test_outbox_and_queue_migrations_share_claim_and_generation_fences():
    root = Path(__file__).parents[1]
    migration = (
        root / "database/migrations/032_outbox_delivery.sql"
    ).read_text()
    rollback = (
        root / "database/migrations/032_outbox_delivery.down.sql"
    ).read_text()
    queue_migration = (
        root / "database/migrations/033_queue_claim_ownership.sql"
    ).read_text()
    queue_rollback = (
        root / "database/migrations/033_queue_claim_ownership.down.sql"
    ).read_text()
    schema = (root / "database/schema_postgres.sql").read_text()

    assert "claim_token UUID" in migration
    assert "claim_token UUID" in schema
    assert "DROP COLUMN IF EXISTS claim_token" in rollback
    assert "WHERE status IN ('dead_letter', 'publishing')" in rollback
    assert "CREATE SEQUENCE IF NOT EXISTS pipeline_work_generation_seq" in migration
    assert "work_generation BIGINT" in migration
    assert "work_generation BIGINT NOT NULL" in schema
    assert "desired_generation BIGINT" in queue_migration
    assert "ambiguous legacy queue/outbox versions" in queue_migration
    assert "desired_generation = COALESCE" in queue_migration
    assert "nextval('pipeline_work_generation_seq')" in queue_migration
    assert "desired_generation BIGINT NOT NULL" in schema
    assert "DROP COLUMN IF EXISTS desired_generation" in queue_rollback


@pytest.mark.asyncio
async def test_claimed_outbox_event_carries_its_work_generation():
    class Connection:
        def __init__(self):
            self.calls = []

        async def fetchrow(self, query, *_args):
            self.calls.append(" ".join(query.split()))
            return {
                "id": 7,
                "event_key": "queue.enqueue:meeting://m1:mv1:a",
                "event_type": "queue.enqueue",
                "aggregate_type": "meeting",
                "aggregate_id": "m1",
                "payload": {},
                "attempt_count": 0,
                "work_generation": 29,
            }

        async def execute(self, *_args):
            return "UPDATE 1"

    class ClaimRepository(PipelineLifecycleRepository):
        def __init__(self):
            self.connection = Connection()

        class _Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        def transaction(self):
            return self._Transaction(self.connection)

    repo = ClaimRepository()
    event = await repo.claim_outbox(lease_owner="publisher")

    assert event is not None
    assert event["work_generation"] == 29
    assert "work_generation" in repo.connection.calls[0]


@pytest.mark.asyncio
async def test_stale_lifecycle_recovery_closes_attempts_runs_and_stages_atomically():
    repo = FakeLifecycleRepository()
    repo.next_row = {"attempts": 2, "runs": 1, "stages": 3}

    recovered = await repo.recover_stale_lifecycle(stale_minutes=15)

    assert recovered == {"attempts": 2, "runs": 1, "stages": 3}
    query, args = repo.fetchrow_calls[0]
    normalized = " ".join(query.split())
    assert "make_interval(mins => $1)" in normalized
    assert "SET status = 'abandoned'" in normalized
    assert "SET status = 'failed'" in normalized
    assert "active_attempt.heartbeat_at >=" in normalized
    assert "FOR UPDATE" in normalized
    assert args == (15,)


@pytest.mark.asyncio
async def test_stale_lifecycle_recovery_rejects_nonpositive_threshold():
    repo = FakeLifecycleRepository()

    with pytest.raises(ValueError, match="must be positive"):
        await repo.recover_stale_lifecycle(stale_minutes=0)

    assert repo.fetchrow_calls == []


@pytest.mark.asyncio
async def test_operational_snapshot_uses_one_aggregate_read_model():
    repo = FakeLifecycleRepository()
    repo.next_row = {
        "queue": {"pending": 3},
        "batch": {"submitted": 2},
        "outbox": {},
        "active_runs": 1,
        "performance_window_hours": 24,
        "stale_claim_threshold_seconds": 600,
        "oldest_ready_seconds": 12,
        "oldest_desired_seconds": 45,
        "oldest_outbox_ready_seconds": 30,
        "unresolved_queue_outbox_dead_letters": 2,
        "tokenless_processing_claims": 1,
        "stale_processing_claims": 3,
        "submission_intents": 0,
        "provider_jobs_due": 1,
        "hourly_performance": [
            {
                "hour": "2026-08-07T12:00:00",
                "job_type": "meeting",
                "lane": "streaming",
                "outcome": "succeeded",
                "attempts": 4,
                "items_processed": 12,
                "items_new": 8,
                "items_failed": 0,
                "queue_wait_avg_ms": 50,
                "desired_age_avg_ms": 250,
                "service_avg_ms": 20,
            }
        ],
        "hourly_batch_performance": [
            {
                "hour": "2026-08-07T12:00:00",
                "phase": "terminal",
                "outcome": "collected",
                "chunks": 2,
                "items": 60,
                "provider_elapsed_avg_ms": 900000,
            }
        ],
        "attempts": 4,
        "succeeded": 3,
        "non_success": 1,
        "queue_wait_p50_ms": 50,
        "queue_wait_p95_ms": 90,
        "desired_age_p50_ms": 250,
        "desired_age_p95_ms": 900,
        "service_p50_ms": 20,
        "service_p95_ms": 40,
    }

    snapshot = await repo.get_operational_snapshot()

    assert snapshot["queue"] == {"pending": 3}
    assert snapshot["queue_wait_p95_ms"] == 90.0
    assert snapshot["desired_age_p95_ms"] == 900.0
    assert snapshot["oldest_desired_seconds"] == 45.0
    assert snapshot["oldest_outbox_ready_seconds"] == 30.0
    assert snapshot["unresolved_queue_outbox_dead_letters"] == 2
    assert snapshot["tokenless_processing_claims"] == 1
    assert snapshot["stale_processing_claims"] == 3
    assert snapshot["performance_window_hours"] == 24
    assert snapshot["stale_claim_threshold_seconds"] == 600
    assert snapshot["hourly_performance"][0]["items_new"] == 8
    assert snapshot["hourly_batch_performance"][0]["items"] == 60
    query, _ = repo.fetchrow_calls[0]
    normalized = " ".join(query.split())
    assert "INTERVAL '1 hour'" in query
    assert "INTERVAL '24 hours'" in query
    assert "INTERVAL '10 minutes'" in query
    assert "MIN(ready_at)" in query
    assert "MIN(last_enqueued_at)" in query
    assert "MIN(ready_since)" in query
    assert "AS unresolved_queue_outbox_dead_letters" in normalized
    assert "candidate.event_type = 'queue.enqueue'" in normalized
    assert "candidate.status = 'dead_letter'" in normalized
    assert "newer_queue.desired_generation > candidate.work_generation" in normalized
    assert "AS tokenless_processing_claims" in normalized
    assert "AS stale_processing_claims" in normalized
    assert "AS hourly_performance" in normalized
    assert "AS hourly_batch_performance" in normalized
    assert "collected_at - submitted_at" in normalized
    assert "COALESCE(completed_at, started_at)" in normalized
    assert "GROUP BY 1, 2, 3, 4" in normalized


@pytest.mark.asyncio
async def test_operational_snapshot_normalizes_empty_hourly_series():
    repo = FakeLifecycleRepository()
    repo.next_row = {
        "hourly_performance": None,
        "hourly_batch_performance": None,
    }

    snapshot = await repo.get_operational_snapshot()

    assert snapshot["hourly_performance"] == []
    assert snapshot["hourly_batch_performance"] == []
