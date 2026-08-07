"""Focused failure-injection tests for the Gemini Batch lifecycle."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from analysis.llm.summarizer import (
    BATCH_SDK_CONCURRENCY,
    BATCH_SUBMIT_CONCURRENCY,
    GeminiSummarizer,
)
from pipeline.processor import Processor
from pipeline.protocols import NullMetrics


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
async def test_sync_sdk_call_runs_off_event_loop() -> None:
    summarizer = make_summarizer()
    thread_started = asyncio.Event()

    def blocking_call() -> str:
        thread_started.set()
        time.sleep(0.1)
        return "done"

    call = asyncio.create_task(summarizer._run_batch_sdk(blocking_call))
    await thread_started.wait()
    # If blocking_call ran on the event loop, this timer could not fire until
    # after the 100ms sleep and call would already be done.
    await asyncio.sleep(0.01)
    assert not call.done()
    assert await call == "done"


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

    async def submit_one(chunk, chunk_num, *args, **kwargs):
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
    assert descriptors[0]["gemini_job_name"] == "jobs/1"
    assert descriptors[1]["error"] == "503 unavailable"


@pytest.mark.asyncio
async def test_collect_reports_provider_omissions_as_item_failures() -> None:
    summarizer = make_summarizer()
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
        },
    ]


@pytest.mark.asyncio
async def test_poll_failure_is_persisted_and_lease_released_for_retry() -> None:
    marked: list[tuple[int, str]] = []

    class BatchJobs:
        async def mark_transient_failure(self, job_id, error):
            marked.append((job_id, error))
            return 2

    class Summarizer:
        async def collect_item_batch(self, *args):
            raise ConnectionError("provider unavailable")

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(batch_jobs=BatchJobs())
    processor.analyzer = SimpleNamespace(summarizer=Summarizer())

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
async def test_partial_collect_requeues_before_chunk_is_closed() -> None:
    events: list[str] = []

    class BatchJobs:
        async def count_open_for_meeting(self, *args):
            return 1

        async def mark_collected(self, *args):
            events.append("collected")

    class Summarizer:
        async def collect_item_batch(self, *args):
            return "succeeded", [{"item_id": "item-1", "success": False}]

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(batch_jobs=BatchJobs())
    processor.analyzer = SimpleNamespace(summarizer=Summarizer())

    async def ingest(*args):
        return 0, 1

    async def requeue(*args):
        events.append("requeued")
        return True

    processor._ingest_batch_results = ingest
    processor._requeue_batch_meeting = requeue

    outcome = await processor._collect_one_job(
        {
            "id": 8,
            "meeting_id": "meeting-1",
            "gemini_job_name": "jobs/1",
            "item_ids": ["item-1"],
            "meeting_meta": {},
        }
    )

    assert outcome == "collected"
    assert events == ["requeued", "collected"]


@pytest.mark.asyncio
async def test_batch_ingest_never_writes_canonical_matter_projection() -> None:
    updates: list[str] = []

    class Items:
        async def get_agenda_item(self, item_id):
            return SimpleNamespace(
                id=item_id, meeting_id="meeting-1", matter_id="matter-1"
            )

        async def update_agenda_item(self, item_id, **kwargs):
            updates.append(item_id)

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(items=Items())
    processor.analyzer = SimpleNamespace(
        summarizer=SimpleNamespace(prompts_version="v-test")
    )
    processor.metrics = NullMetrics()

    async def forbidden_canonical_write(*args, **kwargs):
        raise AssertionError("batch ingestion must not own matter projection")

    processor._store_canonical_summary = forbidden_canonical_write
    processed, failed = await processor._ingest_batch_results(
        "meeting-1",
        [
            {
                "item_id": "item-1",
                "success": True,
                "summary": "summary",
                "topics": [],
            }
        ],
    )

    assert (processed, failed) == (1, 0)
    assert updates == ["item-1"]


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
        "batch_processed": 0,
        "batch_failed": 0,
        "batch_collected": 0,
    }
