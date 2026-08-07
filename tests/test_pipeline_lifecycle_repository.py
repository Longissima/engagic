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
    assert args == (
        12, "partial", "ExtractionError", "one attachment failed",
        {"items_failed": 1, "items_new": 2},
    )


@pytest.mark.asyncio
async def test_failed_outbox_publication_gets_time_based_retry():
    repo = FakeLifecycleRepository()

    await repo.finish_outbox(
        91, succeeded=False, error_message="database unavailable", retry_seconds=120
    )

    query, args = repo.execute_calls[0]
    assert "make_interval" in query
    assert args == (91, "database unavailable", 120)
