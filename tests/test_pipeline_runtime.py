"""CLI and daemon adapters share one scoped pipeline runtime."""

import asyncio
import json
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import database.db_postgres as database_module
import pipeline.conductor as conductor_module
from exceptions import ProcessingError
from pipeline.conductor import Conductor, _await_daemon_tasks
from pipeline.fetcher import SyncResult, SyncStatus
from pipeline.models import MatterJob, MeetingJob, QueueJob
from pipeline.outcomes import JobOutcome
from pipeline.processor import Processor
from pipeline.protocols import NullMetrics


class Lifecycle:
    def __init__(self):
        self.finished = []
        self.dead_letters = 0

    async def start_run(self, command, **kwargs):
        self.started = (command, kwargs)
        return {"id": 41}

    async def heartbeat_run(self, run_id):
        pass

    async def finish_run(self, run_id, status, error_message=None):
        self.finished.append((run_id, status, error_message))

    async def claim_outbox(self, **kwargs):
        return None

    async def count_active_outbox(self, **kwargs):
        return 0

    async def count_dead_letter_outbox(self, **kwargs):
        return self.dead_letters

    async def get_outbox_activity(self, **kwargs):
        return {
            "active": 0,
            "dead_letter": self.dead_letters,
            "ready": 0,
            "next_attempt_at": None,
        }


class Queue:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.scopes = []
        self.recovery_calls = 0

    async def reset_stale_processing_jobs(self):
        self.recovery_calls += 1
        return 2

    async def get_next_for_processing(self, **kwargs):
        self.scopes.append(kwargs.get("bananas"))
        return self.jobs.pop(0) if self.jobs else None

    async def get_scope_activity(self, bananas):
        return {"pending": 0, "processing": 0, "ready": 0, "next_retry_at": None}


class Runner:
    async def run(self, job, **kwargs):
        return JobOutcome.succeeded({"items_processed": 1, "items_new": 1})


def queue_job(banana="alphaCA"):
    return QueueJob(
        id=1,
        job_type="meeting",
        payload=MeetingJob("meeting-1"),
        banana=banana,
        priority=1,
        status="processing",
    )


@pytest.mark.asyncio
async def test_conductor_status_exposes_durable_operational_health_and_throughput():
    pipeline = {
        "unresolved_queue_outbox_dead_letters": 2,
        "oldest_outbox_ready_seconds": 31.5,
        "tokenless_processing_claims": 1,
        "stale_processing_claims": 3,
        "hourly_performance": [
            {
                "hour": "2026-08-07T12:00:00",
                "job_type": "matter",
                "lane": "streaming",
                "outcome": "succeeded",
                "attempts": 4,
                "items_new": 7,
            }
        ],
    }

    class StatusLifecycle:
        async def get_operational_snapshot(self):
            return pipeline

    class StatusDatabase:
        pipeline_lifecycle = StatusLifecycle()

        async def get_stats(self):
            return {"active_cities": 2, "total_meetings": 10}

    conductor = Conductor.__new__(Conductor)
    conductor.db = StatusDatabase()
    conductor.fetcher = SimpleNamespace(failed_cities=set())
    conductor._running = False
    conductor._shutdown_event = asyncio.Event()

    status = await conductor.get_sync_status()

    assert status["pipeline"] is pipeline
    assert status["pipeline"]["unresolved_queue_outbox_dead_letters"] == 2
    assert status["pipeline"]["hourly_performance"][0]["items_new"] == 7


def test_status_cli_uses_db_only_snapshot_and_closes_database(
    monkeypatch, capsys
):
    closed = []
    create_options = []

    class StatusLifecycle:
        async def get_operational_snapshot(self):
            return {"stale_processing_claims": 3}

    class StatusDatabase:
        pipeline_lifecycle = StatusLifecycle()

        async def get_stats(self):
            return {"active_cities": 2, "total_meetings": 10}

        async def close(self):
            closed.append(True)

    database = StatusDatabase()

    class DatabaseFactory:
        @staticmethod
        async def create(**kwargs):
            create_options.append(kwargs)
            return database

    class ForbiddenConductor:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("status must not initialize processing runtimes")

    monkeypatch.setattr(conductor_module, "Database", DatabaseFactory)
    monkeypatch.setattr(conductor_module, "Conductor", ForbiddenConductor)
    monkeypatch.setattr(
        conductor_module,
        "_adjust_worker_oom_score",
        lambda: (_ for _ in ()).throw(
            AssertionError("status must not adjust worker OOM score")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["engagic-conductor", "status"])

    with pytest.raises(SystemExit) as exited:
        conductor_module.main()

    assert exited.value.code == 0
    assert create_options == [{"initialize_corpus": False}]
    assert closed == [True]
    output = json.loads(capsys.readouterr().out)
    assert output["is_running"] is False
    assert output["failed_cities"] == []
    assert output["active_cities"] == 2
    assert output["pipeline"] == {"stale_processing_claims": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initialize_corpus", "expected_corpus_events"),
    [(False, []), (True, ["initialize", "close"])],
)
async def test_database_factory_can_skip_corpus_lifecycle_without_changing_default(
    monkeypatch, initialize_corpus, expected_corpus_events
):
    corpus_events = []

    class Pool:
        _minsize = 1
        _maxsize = 1

        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

        def terminate(self):
            raise AssertionError("clean close should not terminate the pool")

    pool = Pool()

    async def create_pool(*_args, **_kwargs):
        return pool

    def init_corpus(_repository):
        corpus_events.append("initialize")

    async def close_corpus():
        corpus_events.append("close")

    monkeypatch.setattr(database_module.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(database_module, "init_corpus", init_corpus)
    monkeypatch.setattr(database_module, "close_corpus", close_corpus)

    db = await database_module.Database.create(
        "postgresql://test.invalid/engagic",
        min_size=1,
        max_size=1,
        require_current_schema=False,
        initialize_corpus=initialize_corpus,
    )
    await db.close()

    assert pool.closed is True
    assert corpus_events == expected_corpus_events


def test_help_and_inspection_commands_do_not_adjust_worker_oom_score(
    monkeypatch, capsys
):
    adjusted = []
    monkeypatch.setattr(
        conductor_module,
        "_adjust_worker_oom_score",
        lambda: adjusted.append(True),
    )

    monkeypatch.setattr(sys, "argv", ["engagic-conductor", "--help"])
    with pytest.raises(SystemExit) as exited:
        conductor_module.main()

    assert exited.value.code == 0
    assert "Usage:" in capsys.readouterr().out
    assert adjusted == []

    conductor_module._configure_worker_oom_score(None)
    conductor_module._configure_worker_oom_score("status")
    conductor_module._configure_worker_oom_score("preview-queue")
    conductor_module._configure_worker_oom_score("processor")

    assert adjusted == [True]


def finite_processor(monkeypatch):
    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(
        queue=Queue([queue_job()]), pipeline_lifecycle=Lifecycle()
    )
    processor.analyzer = SimpleNamespace(summarizer=SimpleNamespace())
    processor.metrics = NullMetrics()
    processor._running = True
    import asyncio

    processor._shutdown_event = asyncio.Event()
    processor._active_run_id = None
    processor._streaming_job_runner = Runner()
    processor._outbox_worker_id = "test"
    processor._ensure_stale_sweep_running = lambda: None
    monkeypatch.setattr("pipeline.processor.config.BATCH_API_ENABLED", False)
    return processor


@pytest.mark.asyncio
async def test_finite_runtime_claims_one_scope_and_records_one_run(monkeypatch):
    processor = finite_processor(monkeypatch)

    result = await processor.run_pipeline_runtime(
        bananas=["alphaCA", "betaCA"],
        continuous=False,
        command="process-cli",
    )

    assert result["processed_count"] == 1
    assert result["by_banana"]["alphaCA"]["items_new"] == 1
    assert processor.db.queue.scopes
    assert all(
        scope == ["alphaCA", "betaCA"] for scope in processor.db.queue.scopes
    )
    assert processor.db.pipeline_lifecycle.started[0] == "process-cli"
    assert processor.db.pipeline_lifecycle.finished == [(41, "completed", None)]
    assert processor.db.queue.recovery_calls == 1


@pytest.mark.asyncio
async def test_finite_runtime_surfaces_scoped_outbox_dead_letters(monkeypatch):
    processor = finite_processor(monkeypatch)
    processor.db.pipeline_lifecycle.dead_letters = 2

    result = await processor.run_pipeline_runtime(
        bananas=["alphaCA"],
        continuous=False,
        command="process-cli",
    )

    assert result["outbox_dead_letter_count"] == 2
    assert processor.db.pipeline_lifecycle.finished == [
        (41, "failed", "2 scoped queue publication intent(s) are dead-lettered")
    ]


@pytest.mark.asyncio
async def test_outbox_completion_uses_the_individual_claim_token():
    class OutboxLifecycle:
        def __init__(self):
            self.claimed = False
            self.finished = []

        async def claim_outbox(self, **_kwargs):
            if self.claimed:
                return None
            self.claimed = True
            return {
                "id": 9,
                "event_type": "queue.enqueue",
                "event_key": "queue.enqueue:meeting://m1:mv1:a",
                "payload": {
                    "source_url": "meeting://m1",
                    "job_type": "meeting",
                    "payload": {"meeting_id": "m1"},
                },
                "attempt_count": 0,
                "work_generation": 27,
                "claim_token": "00000000-0000-0000-0000-000000000009",
            }

        async def finish_outbox(self, outbox_id, **kwargs):
            self.finished.append((outbox_id, kwargs))
            return True

    class QueuePublisher:
        async def enqueue_job(self, **_kwargs):
            return None

    processor = Processor.__new__(Processor)
    lifecycle = OutboxLifecycle()
    processor.db = SimpleNamespace(
        pipeline_lifecycle=lifecycle,
        queue=QueuePublisher(),
    )
    processor._outbox_worker_id = "publisher"

    attempted, published = await processor._publish_outbox_tick(None)

    assert (attempted, published) == (1, 1)
    assert lifecycle.finished[0][1]["claim_token"] == (
        "00000000-0000-0000-0000-000000000009"
    )


def test_finite_retry_delay_waits_for_earliest_delayed_source():
    now = datetime.now()

    delay = Processor._finite_retry_delay(
        {
            "pending": 1,
            "processing": 0,
            "ready": 0,
            "next_retry_at": now + timedelta(seconds=180),
        },
        {
            "active": 1,
            "dead_letter": 0,
            "ready": 0,
            "next_attempt_at": now + timedelta(seconds=75),
        },
    )

    assert 70 < delay < 80


def test_finite_retry_delay_keeps_short_poll_for_in_flight_work():
    delay = Processor._finite_retry_delay(
        {
            "pending": 0,
            "processing": 1,
            "ready": 0,
            "next_retry_at": None,
        },
        {
            "active": 0,
            "dead_letter": 0,
            "ready": 0,
            "next_attempt_at": None,
        },
    )

    assert delay == 5


@pytest.mark.asyncio
async def test_stale_sweep_recovers_lifecycle_even_when_queue_sweep_fails():
    processor = Processor.__new__(Processor)
    processor._running = True
    import asyncio

    processor._shutdown_event = asyncio.Event()

    class BrokenQueue:
        async def reset_stale_processing_jobs(self, **kwargs):
            raise RuntimeError("queue unavailable")

    class RecoveringLifecycle:
        def __init__(self):
            self.calls = []

        async def recover_stale_lifecycle(self, **kwargs):
            self.calls.append(kwargs)
            processor.is_running = False
            return {"attempts": 1, "runs": 1, "stages": 2}

    lifecycle = RecoveringLifecycle()
    processor.db = SimpleNamespace(queue=BrokenQueue(), pipeline_lifecycle=lifecycle)

    async def no_initial_delay(seconds: float) -> bool:
        del seconds
        return False

    processor._wait_with_shutdown_check = no_initial_delay

    await processor._periodic_stale_reset()

    assert lifecycle.calls == [{"stale_minutes": 15}]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, []])
async def test_finite_runtime_rejects_empty_scope_before_claiming(monkeypatch, scope):
    processor = finite_processor(monkeypatch)

    with pytest.raises(ProcessingError, match="non-empty scope"):
        await processor.run_pipeline_runtime(
            bananas=scope,
            continuous=False,
            command="process-cli",
        )

    assert processor.db.queue.scopes == []
    assert not hasattr(processor.db.pipeline_lifecycle, "started")


@pytest.mark.asyncio
async def test_daemon_adapter_only_selects_continuous_policy():
    processor = Processor.__new__(Processor)
    captured = []

    async def runtime(**kwargs):
        captured.append(kwargs)
        return {}

    processor.run_pipeline_runtime = runtime

    await processor.process_queue()

    assert captured == [
        {"bananas": None, "continuous": True, "command": "processor-daemon"}
    ]


@pytest.mark.asyncio
async def test_processing_daemon_restarts_failed_runtime_while_service_is_live():
    conductor = Conductor.__new__(Conductor)
    conductor._running = True
    import asyncio

    conductor._shutdown_event = asyncio.Event()
    events = []

    class RuntimeProcessor:
        analyzer = object()
        is_running = True

        def __init__(self):
            self.calls = 0

        async def process_queue(self):
            self.calls += 1
            events.append("process")
            if self.calls == 1:
                raise RuntimeError("transient database outage")
            conductor.is_running = False

    conductor.processor = RuntimeProcessor()

    await conductor.run_processing_daemon(restart_delay_seconds=0.001)

    assert conductor.processor.calls == 2
    assert events == ["process", "process"]


@pytest.mark.asyncio
async def test_combined_daemon_fails_fast_without_analyzer():
    conductor = Conductor.__new__(Conductor)
    conductor.processor = SimpleNamespace(analyzer=None)

    with pytest.raises(ProcessingError, match="fetcher command"):
        await conductor.run_processing_daemon()


@pytest.mark.asyncio
async def test_combined_daemon_failure_cancels_and_awaits_sibling():
    sibling_started = asyncio.Event()
    sibling_cleaned = asyncio.Event()

    async def sibling():
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            sibling_cleaned.set()

    async def failing():
        await sibling_started.wait()
        raise RuntimeError("processing startup failed")

    sibling_task = asyncio.create_task(sibling())
    failing_task = asyncio.create_task(failing())

    with pytest.raises(RuntimeError, match="processing startup failed"):
        await _await_daemon_tasks(sibling_task, failing_task)

    assert sibling_task.cancelled()
    assert failing_task.done()
    assert sibling_cleaned.is_set()


@pytest.mark.asyncio
async def test_conductor_processes_all_cities_with_one_runtime_call():
    calls = []

    class RuntimeProcessor:
        analyzer = object()

        async def run_pipeline_runtime(self, **kwargs):
            calls.append(kwargs)
            return {
                "by_banana": {
                    "alphaCA": Counter(processed=2, items_new=3),
                    "betaCA": Counter(failed=1),
                },
                "batch_queue_completed": 4,
                "batch_chunks_collected": 2,
            }

    conductor = Conductor.__new__(Conductor)
    conductor.processor = RuntimeProcessor()

    @contextmanager
    def enabled():
        yield

    conductor.enable_processing = enabled
    results = [
        result
        async for result in conductor.process_cities(["alphaCA", "betaCA"])
    ]

    assert calls == [
        {
            "bananas": ["alphaCA", "betaCA"],
            "continuous": False,
            "command": "process-cli",
        }
    ]
    assert results[0]["processed"] == 2
    assert results[1]["failed"] == 1
    assert results[2]["batch_queue_completed"] == 4


@pytest.mark.asyncio
async def test_conductor_queue_preview_is_read_only_and_identity_aware():
    calls = []
    jobs = [
        QueueJob(
            id=1,
            job_type="meeting",
            payload=MeetingJob("meeting-1"),
            banana="alphaCA",
            priority=10,
            status="pending",
        ),
        QueueJob(
            id=2,
            job_type="matter",
            payload=MatterJob("alphaCA_ord-1"),
            banana="alphaCA",
            priority=9,
            status="pending",
        ),
    ]

    class PreviewQueue:
        async def preview_jobs(self, **kwargs):
            calls.append(kwargs)
            return jobs

        async def get_next_for_processing(self, **kwargs):
            raise AssertionError("preview must never claim")

    conductor = Conductor.__new__(Conductor)
    conductor.db = SimpleNamespace(
        queue=PreviewQueue(),
        meetings=SimpleNamespace(
            get_meeting=lambda meeting_id: None,
        ),
    )

    async def get_meeting(meeting_id):
        return SimpleNamespace(
            id=meeting_id,
            title="Council",
            date=None,
        )

    async def get_matter(matter_id):
        return SimpleNamespace(
            id=matter_id,
            title="Ordinance 1",
            last_seen=None,
        )

    conductor.db.meetings.get_meeting = get_meeting
    conductor.db.matters = SimpleNamespace(get_matter=get_matter)

    result = await conductor.preview_queue(city_banana="alphaCA", limit=2)

    assert calls == [{"banana": "alphaCA", "limit": 2}]
    assert result["total_queued"] == 2
    assert result["previews"][0]["meeting_id"] == "meeting-1"
    assert result["previews"][1]["matter_id"] == "alphaCA_ord-1"
    assert result["previews"][1]["meeting_id"] is None


@pytest.mark.asyncio
async def test_sync_cycle_records_the_same_durable_lifecycle_for_cli_or_daemon():
    lifecycle = Lifecycle()

    async def start_stage(**kwargs):
        lifecycle.stage_started = kwargs
        return 51

    async def finish_stage(stage_id, **kwargs):
        lifecycle.stage_finished = (stage_id, kwargs)

    lifecycle.start_stage = start_stage
    lifecycle.finish_stage = finish_stage

    class Fetcher:
        async def sync_cities(self, bananas):
            assert bananas == ["alphaCA"]
            return [
                SyncResult(
                    city_banana="alphaCA",
                    status=SyncStatus.COMPLETED,
                    meetings_found=2,
                    items_stored=3,
                )
            ]

    conductor = Conductor.__new__(Conductor)
    conductor.db = SimpleNamespace(pipeline_lifecycle=lifecycle)
    conductor.fetcher = Fetcher()

    class Processor:
        async def publish_due_outbox(self, bananas):
            assert bananas == ["alphaCA"]
            return 4

    conductor.processor = Processor()

    results = await conductor.run_sync_cycle(["alphaCA"], command="sync-cli")

    assert results[0].status is SyncStatus.COMPLETED
    assert lifecycle.started[0] == "sync-cli"
    assert lifecycle.stage_started["stage"] == "sync.cycle"
    assert lifecycle.stage_finished[1]["metrics"]["items_stored"] == 3
    assert lifecycle.stage_finished[1]["metrics"]["outbox_published"] == 4
    assert lifecycle.finished == [(41, "completed", None)]


@pytest.mark.asyncio
async def test_sync_cycle_marks_parent_run_failed_for_failed_jurisdiction():
    lifecycle = Lifecycle()

    async def start_stage(**kwargs):
        lifecycle.stage_started = kwargs
        return 51

    async def finish_stage(stage_id, **kwargs):
        lifecycle.stage_finished = (stage_id, kwargs)

    lifecycle.start_stage = start_stage
    lifecycle.finish_stage = finish_stage

    class Fetcher:
        async def sync_cities(self, bananas):
            return [
                SyncResult(
                    city_banana=bananas[0],
                    status=SyncStatus.FAILED,
                    error_message="vendor unavailable",
                )
            ]

    class RuntimeProcessor:
        async def publish_due_outbox(self, bananas):
            return 0

    conductor = Conductor.__new__(Conductor)
    conductor.db = SimpleNamespace(pipeline_lifecycle=lifecycle)
    conductor.fetcher = Fetcher()
    conductor.processor = RuntimeProcessor()

    await conductor.run_sync_cycle(["alphaCA"], command="sync-cli")

    assert lifecycle.stage_finished[1]["status"] == "failed"
    assert lifecycle.finished == [
        (41, "failed", "1 jurisdiction sync(s) failed")
    ]


@pytest.mark.asyncio
async def test_sync_cycle_marks_parent_run_cancelled_for_interrupted_stream():
    lifecycle = Lifecycle()

    async def start_stage(**kwargs):
        lifecycle.stage_started = kwargs
        return 51

    async def finish_stage(stage_id, **kwargs):
        lifecycle.stage_finished = (stage_id, kwargs)

    lifecycle.start_stage = start_stage
    lifecycle.finish_stage = finish_stage

    class Fetcher:
        async def sync_cities(self, bananas):
            return [
                SyncResult(
                    city_banana=bananas[0],
                    status=SyncStatus.CANCELLED,
                    error_message="shutdown between vendor streams",
                )
            ]

    class RuntimeProcessor:
        async def publish_due_outbox(self, bananas):
            return 0

    conductor = Conductor.__new__(Conductor)
    conductor.db = SimpleNamespace(pipeline_lifecycle=lifecycle)
    conductor.fetcher = Fetcher()
    conductor.processor = RuntimeProcessor()

    await conductor.run_sync_cycle(["alphaCA"], command="sync-cli")

    assert lifecycle.stage_finished[1]["status"] == "failed"
    assert lifecycle.stage_finished[1]["metrics"]["cancelled"] == 1
    assert lifecycle.finished == [
        (41, "cancelled", "1 jurisdiction sync(s) cancelled")
    ]


@pytest.mark.asyncio
async def test_sync_cycle_finishes_run_failed_when_stage_startup_raises():
    lifecycle = Lifecycle()

    async def start_stage(**kwargs):
        raise RuntimeError("stage insert lost connection")

    async def finish_stage(stage_id, **kwargs):
        lifecycle.stage_finished = (stage_id, kwargs)

    lifecycle.start_stage = start_stage
    lifecycle.finish_stage = finish_stage

    conductor = Conductor.__new__(Conductor)
    conductor.db = SimpleNamespace(pipeline_lifecycle=lifecycle)

    heartbeats = []

    async def fake_heartbeat(run_id):
        heartbeats.append(run_id)

    conductor._heartbeat_run = fake_heartbeat

    with pytest.raises(RuntimeError, match="stage insert lost connection"):
        await conductor.run_sync_cycle(["alphaCA"], command="sync-cli")

    assert not hasattr(lifecycle, "stage_finished")
    assert lifecycle.finished == [
        (41, "failed", "RuntimeError: stage insert lost connection")
    ]
    assert heartbeats == []


@pytest.mark.asyncio
async def test_sync_cycle_finishes_run_cancelled_when_cancelled_before_stage_exists():
    lifecycle = Lifecycle()

    async def start_stage(**kwargs):
        raise asyncio.CancelledError()

    lifecycle.start_stage = start_stage

    conductor = Conductor.__new__(Conductor)
    conductor.db = SimpleNamespace(pipeline_lifecycle=lifecycle)

    with pytest.raises(asyncio.CancelledError):
        await conductor.run_sync_cycle(["alphaCA"], command="sync-cli")

    assert lifecycle.finished == [(41, "cancelled", None)]
