"""Focused failure-injection tests for the Gemini Batch lifecycle."""

import asyncio
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from analysis.llm.summarizer import (
    BATCH_INPUT_TOKEN_LIMIT,
    BATCH_SDK_CONCURRENCY,
    BATCH_SUBMIT_CONCURRENCY,
    GeminiSummarizer,
)
from database.repositories_async.batch_jobs import BatchJobRepository
from exceptions import ProcessingError
from pipeline.processor import BatchCollectorLeaseLost, Processor
from pipeline.protocols import NullMetrics
from pipeline.utils import meeting_work_version


def make_summarizer() -> GeminiSummarizer:
    """Build the narrow unit under test without constructing a real SDK client."""
    summarizer = GeminiSummarizer.__new__(GeminiSummarizer)
    summarizer.primary_model = "gemini-test"
    summarizer.prompts_version = "v-test"
    summarizer._batch_sdk_semaphore = asyncio.Semaphore(BATCH_SDK_CONCURRENCY)
    summarizer._batch_submit_semaphore = asyncio.Semaphore(BATCH_SUBMIT_CONCURRENCY)
    summarizer._get_prompt = lambda *args, **kwargs: kwargs.get("text", "")
    return summarizer


@pytest.mark.asyncio
async def test_batch_activation_starts_a_distinct_provider_wait_clock() -> None:
    class Repository(BatchJobRepository):
        def __init__(self):
            self.call = None

        async def _fetchrow(self, query, *args):
            self.call = (" ".join(query.split()), args)
            return {"id": 1}

    repository = Repository()

    await repository.activate_submission(
        "submission-key", "jobs/1", 2, "submitter-1"
    )

    assert repository.call is not None
    query, args = repository.call
    assert "submitted_at = NOW()" in query
    assert args == ("submission-key", "jobs/1", 2, "submitter-1")


def test_batch_submission_clock_migration_matches_schema() -> None:
    root = Path(__file__).parents[1]
    migration = (
        root / "database/migrations/035_batch_submission_clock.sql"
    ).read_text()
    rollback = (
        root / "database/migrations/035_batch_submission_clock.down.sql"
    ).read_text()
    schema = (root / "database/schema_postgres.sql").read_text()

    assert "submitted_at TIMESTAMP" in migration
    assert "submitted_at TIMESTAMP" in schema
    assert "gemini_job_name NOT LIKE 'intent:%'" in migration
    assert "DROP COLUMN IF EXISTS submitted_at" in rollback


@pytest.mark.asyncio
async def test_sync_sdk_call_runs_off_event_loop(monkeypatch) -> None:
    summarizer = make_summarizer()
    offloaded = []

    def blocking_call() -> str:
        return "done"

    # Keep executor ownership out of this unit test. The former version called
    # asyncio.Event.set() from a real worker thread (not thread-safe) and could
    # strand pytest in epoll with a default-executor thread after the assertion
    # output. This seam verifies delegation without creating process-lifetime
    # thread state; SDK timeout behavior is an integration concern.
    async def to_thread(call, *args, **kwargs):
        offloaded.append(call)
        await asyncio.sleep(0)
        return call(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    assert await summarizer._run_batch_sdk(blocking_call) == "done"
    assert offloaded == [blocking_call]


@pytest.mark.asyncio
async def test_chunks_submit_concurrently_but_return_in_deterministic_order() -> None:
    summarizer = make_summarizer()
    active = 0
    peak = 0

    async def submit_one(chunk, chunk_num, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        # Complete out of order; gather must still return chunk order.
        await asyncio.sleep(0.01 * (4 - chunk_num))
        active -= 1
        return {
            "gemini_job_name": f"jobs/{chunk_num}",
            "item_ids": [req["item_id"] for req in chunk],
            "chunk_num": chunk_num,
            "attempts": 1,
        }

    summarizer._submit_one_chunk = submit_one
    requests = [
        {"item_id": f"item-{idx}", "title": "title", "text": "text"}
        for idx in range(61)
    ]
    first = await summarizer.submit_item_batches(
        requests, submission_scope="meeting-1", include_failures=True
    )
    second = await summarizer.submit_item_batches(
        requests, submission_scope="meeting-1", include_failures=True
    )

    assert peak == BATCH_SUBMIT_CONCURRENCY
    assert [entry["chunk_num"] for entry in first] == [1, 2, 3]
    assert [entry["submission_key"] for entry in first] == [
        entry["submission_key"] for entry in second
    ]


@pytest.mark.asyncio
async def test_partial_submit_activates_success_and_releases_failed_intent() -> None:
    summarizer = make_summarizer()
    reserved: list[int] = []
    activated: list[int] = []
    failed: list[int] = []
    lifecycle: list[tuple[str, int]] = []

    async def submit_one(chunk, chunk_num, *args, **kwargs):
        lifecycle.append(("provider", chunk_num))
        base = {
            "item_ids": [req["item_id"] for req in chunk],
            "chunk_num": chunk_num,
            "attempts": 2,
        }
        if chunk_num == 2:
            return {**base, "error": "503 unavailable"}
        return {**base, "gemini_job_name": f"jobs/{chunk_num}"}

    async def reserve(descriptor):
        reserved.append(descriptor["chunk_num"])
        lifecycle.append(("reserve", descriptor["chunk_num"]))
        return True

    async def activate(descriptor):
        activated.append(descriptor["chunk_num"])

    async def fail(descriptor):
        failed.append(descriptor["chunk_num"])

    summarizer._submit_one_chunk = submit_one
    requests = [
        {"item_id": f"item-{idx}", "title": "title", "text": "text"}
        for idx in range(31)
    ]
    descriptors = await summarizer.submit_item_batches(
        requests,
        submission_scope="meeting-1",
        reserve_submission=reserve,
        record_submission=activate,
        fail_submission=fail,
        include_failures=True,
    )

    assert reserved == [1, 2]
    assert activated == [1]
    assert failed == [2]
    assert lifecycle[:2] == [("reserve", 1), ("reserve", 2)]
    assert descriptors[0]["gemini_job_name"] == "jobs/1"
    assert descriptors[1]["error"] == "503 unavailable"


@pytest.mark.asyncio
async def test_reservation_exception_returns_descriptor_without_provider_create() -> None:
    summarizer = make_summarizer()

    async def reserve(_descriptor):
        raise RuntimeError("database unavailable")

    async def forbidden_submit(*_args, **_kwargs):
        raise AssertionError("provider create requires a durable reservation")

    summarizer._submit_one_chunk = forbidden_submit
    descriptors = await summarizer.submit_item_batches(
        [{"item_id": "item-1", "title": "title", "text": "text"}],
        submission_scope="meeting-1",
        reserve_submission=reserve,
        include_failures=True,
    )

    assert len(descriptors) == 1
    assert descriptors[0]["chunk_num"] == 1
    assert descriptors[0]["stage"] == "reserve"
    assert descriptors[0]["error"] == "database unavailable"


@pytest.mark.asyncio
async def test_collect_reports_provider_omissions_as_item_failures(monkeypatch) -> None:
    summarizer = make_summarizer()

    async def to_thread(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)
    summarizer.client = SimpleNamespace(
        batches=SimpleNamespace(
            get=lambda **kwargs: SimpleNamespace(
                state=SimpleNamespace(name="JOB_STATE_SUCCEEDED"),
                dest=SimpleNamespace(file_name="files/result"),
            )
        ),
        files=SimpleNamespace(download=lambda **kwargs: b"one-result-line\n"),
    )
    summarizer._parse_batch_response_line = lambda *args: {
        "item_id": "item-a",
        "success": True,
        "summary": "summary",
        "topics": [],
    }

    state, results = await summarizer.collect_item_batch(
        "jobs/1", ["item-a", "item-b"]
    )

    assert state == "succeeded"
    assert results == [
        {
            "item_id": "item-a",
            "success": True,
            "summary": "summary",
            "topics": [],
        },
        {
            "item_id": "item-b",
            "success": False,
            "error": "Batch response omitted item",
            "error_type": "omitted_batch_result",
            "retryable": True,
        },
    ]


def test_batch_invalid_argument_is_terminal_for_unchanged_request() -> None:
    summarizer = make_summarizer()
    result = summarizer._parse_batch_response_line(
        json.dumps(
            {
                "key": "item-1",
                "error": {
                    "code": 3,
                    "message": "Request contains an invalid argument.",
                },
            }
        ),
        1,
        {"item-1": {"item_id": "item-1"}},
    )

    assert result is not None
    assert result["success"] is False
    assert result["error_code"] == 3
    assert result["retryable"] is False


def test_batch_empty_safety_response_preserves_diagnostics() -> None:
    summarizer = make_summarizer()
    result = summarizer._parse_batch_response_line(
        json.dumps(
            {
                "key": "item-1",
                "response": {
                    "candidates": [
                        {
                            "finishReason": "SAFETY",
                            "safetyRatings": [
                                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT"}
                            ],
                        }
                    ],
                    "promptFeedback": {"blockReason": "SAFETY"},
                    "usageMetadata": {"promptTokenCount": 123},
                },
            }
        ),
        1,
        {"item-1": {"item_id": "item-1"}},
    )

    assert result is not None
    assert result["success"] is False
    assert result["retryable"] is False
    assert result["diagnostics"] == {
        "finish_reason": "SAFETY",
        "prompt_feedback": {"blockReason": "SAFETY"},
        "safety_ratings": [
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT"}
        ],
        "usage_metadata": {"promptTokenCount": 123},
    }


@pytest.mark.asyncio
async def test_large_batch_request_is_counted_and_rejected_before_upload(
    monkeypatch,
) -> None:
    summarizer = make_summarizer()
    uploaded = False
    failed_descriptors = []

    async def to_thread(call, *args, **kwargs):
        return call(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    def forbidden_upload(**_kwargs):
        nonlocal uploaded
        uploaded = True
        raise AssertionError("oversized input must not be uploaded")

    summarizer.client = SimpleNamespace(
        models=SimpleNamespace(
            count_tokens=lambda **_kwargs: SimpleNamespace(
                total_tokens=BATCH_INPUT_TOKEN_LIMIT + 1
            )
        ),
        files=SimpleNamespace(upload=forbidden_upload),
    )

    async def reserve(_descriptor):
        return True

    async def fail(descriptor):
        failed_descriptors.append(descriptor)

    descriptors = await summarizer.submit_item_batches(
        [{"item_id": "item-huge", "title": "Huge", "text": "x" * 500_000}],
        submission_scope="meeting-1",
        reserve_submission=reserve,
        fail_submission=fail,
        include_failures=True,
    )

    assert uploaded is False
    assert len(descriptors) == 1
    assert descriptors[0]["error_type"] == "representation_required"
    assert descriptors[0]["retryable"] is False
    assert descriptors[0]["attempts"] == 0
    assert failed_descriptors == descriptors


@pytest.mark.asyncio
async def test_poll_failure_is_persisted_and_lease_released_for_retry() -> None:
    marked: list[tuple[int, str]] = []

    class BatchJobs:
        async def mark_transient_failure(self, job_id, error, **kwargs):
            marked.append((job_id, error))
            return 2

    class Summarizer:
        async def collect_item_batch(self, *args):
            raise ConnectionError("provider unavailable")

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(batch_jobs=BatchJobs())
    processor.analyzer = SimpleNamespace(summarizer=Summarizer())
    processor._batch_collector_id = "collector-1"

    outcome = await processor._collect_one_job(
        {
            "id": 7,
            "meeting_id": "meeting-1",
            "gemini_job_name": "jobs/1",
            "item_ids": ["item-1"],
        }
    )

    assert outcome == "transient_failure"
    assert marked == [(7, "poll/download: ConnectionError: provider unavailable")]


@pytest.mark.asyncio
async def test_partial_collect_closes_and_requeues_atomically() -> None:
    events: list[str] = []

    class BatchJobs:
        async def count_open_for_meeting(self, *args):
            return 1

    class Summarizer:
        async def collect_item_batch(self, *args):
            return "succeeded", [{"item_id": "item-1", "success": False}]

        async def delete_shared_context_cache(self, *_args):
            events.append("delete cache")

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(batch_jobs=BatchJobs())
    processor.analyzer = SimpleNamespace(summarizer=Summarizer())
    processor._batch_collector_id = "collector-1"
    processor.metrics = NullMetrics()

    def prepare(*args, **kwargs):
        return [], 1

    async def commit(*args, **kwargs):
        events.append("write+requeue+close")
        return [], False, 1

    processor._prepare_batch_item_results = prepare
    processor._commit_batch_item_results = commit

    outcome = await processor._collect_one_job(
        {
            "id": 8,
            "meeting_id": "meeting-1",
            "gemini_job_name": "jobs/1",
            "item_ids": ["item-1"],
            "prompts_version": "v-test",
            "meeting_meta": {},
        }
    )

    assert outcome == "collected"
    assert events == ["write+requeue+close"]


@pytest.mark.asyncio
async def test_expired_submission_intent_is_terminalized_and_requeued() -> None:
    intent = {
        "id": 9,
        "meeting_id": "meeting-1",
        "banana": "exampleCA",
        "gemini_job_name": "intent:submission-key:nonce",
    }
    calls: list[tuple] = []

    class BatchJobs:
        async def claim_expired_submission_intents(
            self, collector_id, *, bananas, limit
        ):
            calls.append(("claim", collector_id, bananas, limit))
            return [intent]

        async def defer_submission_intent_recovery(self, *args):
            raise AssertionError("successful recovery must not be deferred")

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(batch_jobs=BatchJobs())
    processor._batch_collector_id = "collector-1"

    async def terminalize(job, *, terminal_status, error_message):
        calls.append(("terminalize", job, terminal_status, error_message))
        return True

    processor._terminalize_batch_and_requeue = terminalize
    stats = Counter()

    await processor._recover_expired_submission_intents(["exampleCA"], stats)

    assert calls[0][:3] == ("claim", "collector-1", ["exampleCA"])
    assert calls[1] == (
        "terminalize",
        intent,
        "failed",
        "Submission intent expired before provider activation",
    )
    assert stats == Counter(expired_intents_recovered=1)


@pytest.mark.asyncio
async def test_batch_cache_retires_only_after_exact_last_reference_closes() -> None:
    counts = iter([1, 0])
    checked: list[str] = []
    deleted: list[str] = []

    class BatchJobs:
        async def count_open_for_cache(self, cache_name):
            checked.append(cache_name)
            return next(counts)

    class Summarizer:
        async def delete_shared_context_cache(self, cache_name):
            deleted.append(cache_name)

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(batch_jobs=BatchJobs())
    processor.analyzer = SimpleNamespace(summarizer=Summarizer())

    await processor._retire_batch_cache_if_unused(
        {"id": 1, "cache_name": "caches/shared"}
    )
    await processor._retire_batch_cache_if_unused(
        {"id": 2, "cache_name": "caches/shared"}
    )

    assert checked == ["caches/shared", "caches/shared"]
    assert deleted == ["caches/shared"]


def atomic_handoff_processor(*, reactivate_error: bool = False):
    state = {"batch_status": "submitted", "queue_active": False}
    events: list[str] = []
    publication: dict = {}
    connection = object()

    class Transaction:
        async def __aenter__(self):
            self.snapshot = state.copy()
            events.append("begin")
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            del exc, traceback
            if exc_type is not None:
                state.clear()
                state.update(self.snapshot)
                events.append("rollback")
            else:
                events.append("commit")
            return False

    class BatchJobs:
        def transaction(self):
            return Transaction()

        async def mark_collected(self, job_id, *, lease_owner, conn):
            assert (job_id, lease_owner, conn) == (9, "collector-1", connection)
            state["batch_status"] = "collected"
            events.append("close")
            return True

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            return SimpleNamespace(id=meeting_id, banana="currentCA")

        async def update_processing_status(self, meeting_id, status, *, conn):
            assert (meeting_id, status, conn) == (
                "meeting-1",
                "pending",
                connection,
            )
            events.append("pending")

    class Items:
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            return []

    class Queue:
        async def enqueue_job(self, *, conn, **kwargs):
            assert conn is connection
            assert kwargs["source_url"] == "meeting://meeting-1"
            assert state["batch_status"] == "submitted"
            publication.update(kwargs)
            state["queue_active"] = True
            events.append("enqueue")

        async def retry_job_version(self, *, conn, **kwargs):
            assert conn is connection
            assert kwargs["source_url"] == "meeting://meeting-1"
            events.append("retry")
            if reactivate_error:
                raise RuntimeError("injected retry failure")
            return "pending"

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(
        batch_jobs=BatchJobs(), meetings=Meetings(), items=Items(), queue=Queue()
    )
    processor._batch_collector_id = "collector-1"
    return processor, state, events, publication


@pytest.mark.asyncio
async def test_atomic_batch_handoff_uses_global_lock_order_and_commits() -> None:
    processor, state, events, publication = atomic_handoff_processor()

    recovered = await processor._terminalize_batch_and_requeue(
        {"id": 9, "meeting_id": "meeting-1", "banana": "staleCA"},
        terminal_status="collected",
        error_message="one item failed",
    )

    assert recovered is True
    assert state == {"batch_status": "collected", "queue_active": True}
    assert publication["banana"] == "currentCA"
    assert events == [
        "begin",
        "enqueue",
        "retry",
        "pending",
        "close",
        "commit",
    ]


@pytest.mark.asyncio
async def test_atomic_batch_handoff_rolls_back_close_when_reactivation_fails() -> None:
    processor, state, events, _publication = atomic_handoff_processor(
        reactivate_error=True
    )

    recovered = await processor._terminalize_batch_and_requeue(
        {"id": 9, "meeting_id": "meeting-1", "banana": "exampleCA"},
        terminal_status="collected",
        error_message="one item failed",
    )

    assert recovered is False
    assert state == {"batch_status": "submitted", "queue_active": False}
    assert events == ["begin", "enqueue", "retry", "rollback"]


@pytest.mark.asyncio
async def test_batch_requeue_uses_locked_authoritative_snapshot() -> None:
    connection = object()
    events: list[str] = []
    stale = SimpleNamespace(id="meeting-1", banana="oldCA", title="Old title")
    current = SimpleNamespace(
        id="meeting-1", banana="currentCA", title="Current title"
    )
    stale_items = [SimpleNamespace(id="old-item", sequence=1, title="Old")]
    current_items = [
        SimpleNamespace(id="current-item", sequence=1, title="Current")
    ]
    publication: dict = {}

    class Transaction:
        async def __aenter__(self):
            events.append("begin")
            return connection

        async def __aexit__(self, exc_type, *_args):
            events.append("rollback" if exc_type else "commit")
            return False

    class Meetings:
        async def get_meeting(
            self, meeting_id, conn=None, *, lock_for_update=False
        ):
            assert meeting_id == "meeting-1"
            if conn is connection and lock_for_update:
                events.append("lock meeting")
                return current
            return stale

    class Items:
        async def get_agenda_items(
            self, meeting_id, conn=None, *, lock_for_update=False
        ):
            assert meeting_id == "meeting-1"
            if conn is connection and lock_for_update:
                events.append("lock items")
                return current_items
            return stale_items

    class Queue:
        def transaction(self):
            return Transaction()

        async def enqueue_job(self, *, conn=None, **kwargs):
            assert conn is connection
            events.append("enqueue")
            publication.update(kwargs)

        async def reactivate_job_version(self, *, conn=None, **kwargs):
            assert conn is connection
            events.append("reactivate")
            assert kwargs["work_version"] == publication["work_version"]
            return True

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(
        meetings=Meetings(), items=Items(), queue=Queue()
    )

    assert await processor._requeue_batch_meeting("meeting-1") is True
    assert publication["banana"] == "currentCA"
    assert publication["work_version"] == meeting_work_version(
        current, current_items
    )
    assert publication["work_version"] != meeting_work_version(stale, stale_items)
    assert events == [
        "begin",
        "lock meeting",
        "lock items",
        "enqueue",
        "reactivate",
        "commit",
    ]


def test_batch_result_preparation_never_writes_canonical_matter_projection() -> None:
    processor = Processor.__new__(Processor)
    processor.analyzer = SimpleNamespace(
        summarizer=SimpleNamespace(prompts_version="v-current")
    )

    async def forbidden_canonical_write(*args, **kwargs):
        raise AssertionError("batch ingestion must not own matter projection")

    processor._store_canonical_summary = forbidden_canonical_write
    writes, failed = processor._prepare_batch_item_results(
        {
            "id": 1,
            "meeting_id": "meeting-1",
            "item_ids": ["item-1"],
            "prompts_version": "v-submitted",
        },
        [
            {
                "item_id": "item-1",
                "success": True,
                "summary": "summary",
                "topics": [],
            }
        ],
    )

    assert failed == 0
    assert writes == [
        {
            "item_id": "item-1",
            "summary": "summary",
            "topics": [],
            "prompts_version": "v-submitted",
        }
    ]


def test_batch_result_preparation_rejects_duplicate_and_missing_outputs() -> None:
    processor = Processor.__new__(Processor)
    processor.analyzer = SimpleNamespace(
        summarizer=SimpleNamespace(prompts_version="v-test")
    )
    job = {
        "id": 1,
        "meeting_id": "meeting-1",
        "item_ids": ["item-1", "item-2"],
        "prompts_version": "v-test",
    }
    duplicate = {
        "item_id": "item-1",
        "success": True,
        "summary": "nondeterministic output",
        "topics": [],
    }

    writes, failed = processor._prepare_batch_item_results(
        job, [duplicate, {**duplicate, "summary": "different output"}]
    )

    assert writes == []
    assert failed == 2


def test_batch_result_preparation_rejects_empty_success_summary() -> None:
    processor = Processor.__new__(Processor)
    processor.analyzer = SimpleNamespace(
        summarizer=SimpleNamespace(prompts_version="v-test")
    )

    writes, failed = processor._prepare_batch_item_results(
        {
            "id": 1,
            "meeting_id": "meeting-1",
            "item_ids": ["item-1"],
            "prompts_version": "v-test",
        },
        [
            {
                "item_id": "item-1",
                "success": True,
                "summary": "   ",
                "topics": [],
            }
        ],
    )

    assert writes == []
    assert failed == 1


def collector_commit_processor(
    *,
    summary=None,
    lease_owned=True,
    item_ids=("item-1",),
    open_jobs=(1,),
    queue_status="pending",
):
    state = {
        "summaries": {item_id: summary for item_id in item_ids},
        "open_jobs": set(open_jobs),
        "batch_status": "submitted",
        "queue_active": False,
        "queue_status": queue_status,
        "meeting_finalized": False,
        "meeting_status": "completed",
    }
    events: list[str] = []
    connection = object()
    meeting = SimpleNamespace(id="meeting-1", banana="exampleCA", title="Meeting")

    def item(item_id):
        return SimpleNamespace(
            id=item_id,
            meeting_id="meeting-1",
            sequence=1,
            title=f"Item {item_id}",
            summary=state["summaries"][item_id],
            topics=[],
            filter_reason=None,
        )

    def items():
        return [item(item_id) for item_id in item_ids]

    class Transaction:
        async def __aenter__(self):
            self.snapshot = {
                **state,
                "summaries": state["summaries"].copy(),
                "open_jobs": state["open_jobs"].copy(),
            }
            events.append("begin")
            return connection

        async def __aexit__(self, exc_type, *_args):
            if exc_type:
                state.clear()
                state.update(self.snapshot)
                events.append("rollback")
            else:
                events.append("commit")
            return False

    class BatchJobs:
        def transaction(self):
            return Transaction()

        async def count_other_open_for_meeting(
            self, meeting_id, job_id, *, conn
        ):
            assert (meeting_id, conn) == ("meeting-1", connection)
            return len(state["open_jobs"] - {job_id})

        async def mark_collected(self, job_id, *, lease_owner, conn):
            assert (lease_owner, conn) == ("collector-1", connection)
            events.append("close" if lease_owned else "close rejected")
            if lease_owned and job_id in state["open_jobs"]:
                state["open_jobs"].remove(job_id)
                state["batch_status"] = "collected"
                return True
            return False

        async def mark_failed(
            self, job_id, error_message, *, lease_owner, conn
        ):
            assert error_message
            assert (lease_owner, conn) == ("collector-1", connection)
            events.append("close" if lease_owned else "close rejected")
            if lease_owned and job_id in state["open_jobs"]:
                state["open_jobs"].remove(job_id)
                state["batch_status"] = "failed"
                return True
            return False

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock meeting")
            return meeting

        async def update_meeting_summary(self, *, meeting_id, conn, **_kwargs):
            assert (meeting_id, conn) == ("meeting-1", connection)
            state["meeting_finalized"] = True
            events.append("finalize meeting")

        async def update_processing_status(self, meeting_id, status, *, conn):
            assert meeting_id == "meeting-1"
            assert status in {"pending", "failed"}
            assert conn is connection
            state["meeting_status"] = status
            events.append(f"meeting {status}")

    class Items:
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock items")
            return items()

        async def update_agenda_item(self, *, item_id, summary, conn, **_kwargs):
            assert conn is connection
            state["summaries"][item_id] = summary
            events.append("write item")

    class Queue:
        async def lock_desired_state(self, source_url, *, conn):
            assert (source_url, conn) == ("meeting://meeting-1", connection)
            events.append("lock queue")
            return {"status": state["queue_status"], "work_version": work_version}

        async def enqueue_job(self, *, conn, **_kwargs):
            assert conn is connection
            state["queue_active"] = True
            state["queue_status"] = "pending"
            events.append("enqueue")

        async def reactivate_job_version(self, *, conn, **_kwargs):
            assert conn is connection
            events.append("reactivate")
            return True

        async def retry_job_version(self, *, conn, **_kwargs):
            assert conn is connection
            state["queue_status"] = "pending"
            events.append("retry")
            return "pending"

        async def fail_job_version(self, *, conn, **_kwargs):
            assert conn is connection
            state["queue_status"] = "failed"
            events.append("fail")
            return "failed"

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(
        batch_jobs=BatchJobs(), meetings=Meetings(), items=Items(), queue=Queue()
    )
    processor._batch_collector_id = "collector-1"
    work_version = meeting_work_version(meeting, items())
    return processor, state, events, work_version


@pytest.mark.asyncio
async def test_collector_lease_loss_rolls_back_item_and_queue_writes() -> None:
    processor, state, events, work_version = collector_commit_processor(
        lease_owned=False
    )

    with pytest.raises(BatchCollectorLeaseLost):
        await processor._commit_batch_item_results(
            {"id": 1, "meeting_id": "meeting-1"},
            [
                {
                    "item_id": "item-1",
                    "summary": "stale collector output",
                    "topics": [],
                    "prompts_version": "v-test",
                }
            ],
            failed=1,
            expected_work_version=work_version,
        )

    assert state["summaries"] == {"item-1": None}
    assert state["open_jobs"] == {1}
    assert state["batch_status"] == "submitted"
    assert state["queue_active"] is False
    assert state["meeting_finalized"] is False
    assert state["meeting_status"] == "completed"
    assert events == [
        "begin",
        "lock meeting",
        "lock items",
        "write item",
        "lock items",
        "enqueue",
        "retry",
        "meeting pending",
        "close rejected",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_same_version_duplicate_output_cannot_overwrite_frozen_summary() -> None:
    processor, state, events, work_version = collector_commit_processor(
        summary="first committed output"
    )

    applied, superseded, failed = await processor._commit_batch_item_results(
        {"id": 1, "meeting_id": "meeting-1"},
        [
            {
                "item_id": "item-1",
                "summary": "different nondeterministic output",
                "topics": [],
                "prompts_version": "v-test",
            }
        ],
        failed=0,
        expected_work_version=work_version,
    )

    assert (applied, superseded, failed) == ([], False, 0)
    assert state["summaries"]["item-1"] == "first committed output"
    assert state["batch_status"] == "collected"
    assert "write item" not in events


@pytest.mark.asyncio
async def test_last_batch_chunk_finalizes_before_lease_checked_close() -> None:
    processor, state, events, work_version = collector_commit_processor(
        queue_status="completed"
    )

    result = await processor._commit_batch_item_results(
        {"id": 1, "meeting_id": "meeting-1", "meeting_meta": {}},
        [
            {
                "item_id": "item-1",
                "summary": "summary",
                "topics": [],
                "prompts_version": "v-test",
            }
        ],
        failed=0,
        expected_work_version=work_version,
    )

    assert result == (["item-1"], False, 0)
    assert state["meeting_finalized"] is True
    assert events == [
        "begin",
        "lock meeting",
        "lock items",
        "write item",
        "lock items",
        "lock queue",
        "finalize meeting",
        "close",
        "commit",
    ]


@pytest.mark.asyncio
async def test_fast_last_chunk_reactivates_original_processing_claim() -> None:
    processor, state, events, work_version = collector_commit_processor(
        summary="already committed",
        queue_status="processing",
    )

    result = await processor._commit_batch_item_results(
        {"id": 1, "meeting_id": "meeting-1", "meeting_meta": {}},
        [],
        failed=0,
        expected_work_version=work_version,
    )

    assert result == ([], False, 0)
    assert state["queue_status"] == "pending"
    assert state["meeting_status"] == "pending"
    assert state["meeting_finalized"] is False
    assert events == [
        "begin",
        "lock meeting",
        "lock items",
        "lock queue",
        "meeting pending",
        "enqueue",
        "reactivate",
        "close",
        "commit",
    ]


@pytest.mark.asyncio
async def test_unversioned_legacy_batch_result_is_requeued_without_domain_write() -> None:
    processor, state, events, _work_version = collector_commit_processor(
        queue_status="completed"
    )

    result = await processor._commit_batch_item_results(
        {"id": 1, "meeting_id": "meeting-1"},
        [
            {
                "item_id": "item-1",
                "summary": "unfenced legacy output",
                "topics": [],
                "prompts_version": None,
            }
        ],
        failed=0,
        expected_work_version=None,
    )

    assert result == ([], True, 0)
    assert state["summaries"]["item-1"] is None
    assert state["queue_status"] == "pending"
    assert state["meeting_finalized"] is False
    assert "write item" not in events


@pytest.mark.asyncio
async def test_terminal_item_failure_stops_unchanged_meeting_work() -> None:
    processor, state, events, work_version = collector_commit_processor(
        queue_status="completed"
    )

    result = await processor._commit_batch_item_results(
        {"id": 1, "meeting_id": "meeting-1", "meeting_meta": {}},
        [],
        failed=1,
        terminal_failed=1,
        failure_message="item-1: INVALID_ARGUMENT",
        expected_work_version=work_version,
    )

    assert result == ([], False, 1)
    assert state["queue_status"] == "failed"
    assert state["meeting_status"] == "failed"
    assert state["batch_status"] == "failed"
    assert events == [
        "begin",
        "lock meeting",
        "lock items",
        "enqueue",
        "fail",
        "meeting failed",
        "close",
        "commit",
    ]


@pytest.mark.asyncio
async def test_successful_sibling_cannot_revive_terminal_incomplete_work() -> None:
    processor, state, events, work_version = collector_commit_processor(
        item_ids=("item-1", "item-2"),
        open_jobs=(2,),
        queue_status="failed",
    )

    result = await processor._commit_batch_item_results(
        {"id": 2, "meeting_id": "meeting-1", "meeting_meta": {}},
        [
            {
                "item_id": "item-2",
                "summary": "summary for item-2",
                "topics": [],
                "prompts_version": "v-test",
            }
        ],
        failed=0,
        expected_work_version=work_version,
    )

    assert result == (["item-2"], False, 0)
    assert state["queue_status"] == "failed"
    assert state["meeting_status"] == "failed"
    assert state["meeting_finalized"] is False
    assert "enqueue" not in events
    assert "reactivate" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_chunk_first", [False, True])
async def test_mixed_chunk_outcomes_never_finalize_partial_meeting(
    failed_chunk_first: bool,
) -> None:
    processor, state, events, work_version = collector_commit_processor(
        item_ids=("item-1", "item-2"),
        open_jobs=(1, 2),
        queue_status="completed",
    )

    async def commit(job_id: int, item_id: str, succeeds: bool):
        writes = (
            [
                {
                    "item_id": item_id,
                    "summary": f"summary for {item_id}",
                    "topics": [],
                    "prompts_version": "v-test",
                }
            ]
            if succeeds
            else []
        )
        return await processor._commit_batch_item_results(
            {"id": job_id, "meeting_id": "meeting-1", "meeting_meta": {}},
            writes,
            failed=0 if succeeds else 1,
            expected_work_version=work_version,
        )

    if failed_chunk_first:
        await commit(1, "item-1", False)
        await commit(2, "item-2", True)
    else:
        await commit(1, "item-1", True)
        await commit(2, "item-2", False)

    assert state["open_jobs"] == set()
    assert state["queue_status"] == "pending"
    assert state["meeting_finalized"] is False
    assert "finalize meeting" not in events


@pytest.mark.asyncio
async def test_finite_supervisor_overlaps_shared_submit_and_collect_primitives() -> None:
    collector_started = asyncio.Event()
    submit_observed_collector = False

    processor = Processor.__new__(Processor)
    processor._running = True
    processor._shutdown_event = asyncio.Event()
    processor._ensure_stale_sweep_running = lambda: None

    async def submitters(*args, **kwargs):
        nonlocal submit_observed_collector
        await asyncio.wait_for(collector_started.wait(), timeout=0.2)
        submit_observed_collector = True

    async def collector(*args, submissions_done, **kwargs):
        collector_started.set()
        await asyncio.wait_for(submissions_done.wait(), timeout=0.2)

    processor._run_batch_submitters = submitters
    processor._run_batch_collector = collector

    result = await processor.run_batch_supervisor(["exampleCA"])

    assert submit_observed_collector is True
    assert result == {
        "batch_queue_completed": 0,
        "batch_chunks_collected": 0,
        "batch_processed": 0,
        "batch_failed": 0,
        "batch_collected": 0,
        "batch_superseded": 0,
    }


@pytest.mark.asyncio
async def test_finite_batch_supervisor_rejects_empty_scope_before_starting_tasks() -> None:
    processor = Processor.__new__(Processor)

    with pytest.raises(ProcessingError, match="non-empty scope"):
        await processor.run_batch_supervisor([])


@pytest.mark.asyncio
async def test_cancelling_finite_supervisor_cancels_and_awaits_collector() -> None:
    collector_started = asyncio.Event()
    collector_exited = asyncio.Event()
    collector_task = None

    processor = Processor.__new__(Processor)
    processor._running = True
    processor._shutdown_event = asyncio.Event()
    processor._ensure_stale_sweep_running = lambda: None

    async def submitters(*args, **kwargs):
        await asyncio.Event().wait()

    async def collector(*args, **kwargs):
        nonlocal collector_task
        collector_task = asyncio.current_task()
        collector_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            collector_exited.set()

    processor._run_batch_submitters = submitters
    processor._run_batch_collector = collector

    supervisor = asyncio.create_task(processor.run_batch_supervisor(["exampleCA"]))
    await asyncio.wait_for(collector_started.wait(), timeout=0.2)
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor

    assert collector_exited.is_set()
    assert collector_task is not None
    assert collector_task.done()
    assert collector_task.cancelled()
