"""Pipeline Processor - Queue processing and item assembly"""

import asyncio
import ctypes
import ctypes.util
import time
import uuid
from collections import Counter
from typing import List, Optional, Dict, Any

from database.db_postgres import Database
from database.models import Meeting, Matter, MatterMetadata, ParticipationInfo
from database.id_generation import validate_matter_id, extract_banana_from_matter_id
from corpus.store import get_corpus
from pipeline.ground_truth import produce_ground_truth
from pipeline.job_runner import JobExecutionPolicy, JobRunner, TerminalJobError
from pipeline.models import MatterJob, MeetingJob, QueueJob
from pipeline.outcomes import JobOutcome, OutcomeStatus
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.utils import (
    filter_document_version_urls,
    hash_substantive_attachments,
    hash_substantive_attachments_legacy,
)
from pipeline.url_refresh import refresh_attachment_urls
from exceptions import ProcessingError, ExtractionError, LLMError
from analysis.analyzer_async import AsyncAnalyzer
from analysis.llm.input_budget import (
    DOCUMENT_ATTACHMENT_TYPES,
    MAX_ITEM_INPUT_CHARS,
    MAX_SHARED_CONTEXT_CHARS,
    PUBLIC_COMMENT_EXCERPT_CHARS,
    render_document_parts,
    truncate_text_to_budget,
)
from analysis.topics.normalizer import get_normalizer
from parsing.participation import parse_participation_info
from config import config, get_logger
from pipeline.protocols import MetricsCollector, NullMetrics
from pipeline.filters import get_skip_reason, is_public_comment_attachment
from vendors.session_manager_async import AsyncSessionManager

logger = get_logger(__name__).bind(component="processor")

QUEUE_POLL_INTERVAL = 5
QUEUE_FATAL_ERROR_BACKOFF = 10

# Batch collector cadence. Batch turnaround is minutes-to-hours, so a quiet
# poll keeps GET volume low while still draining jobs promptly once Gemini
# finishes them. BATCH_SIZE caps how many open jobs one tick polls.
BATCH_COLLECTOR_POLL_INTERVAL = 60
BATCH_COLLECTOR_BATCH_SIZE = 100
BATCH_COLLECTOR_CONCURRENCY = 8
BATCH_COLLECTOR_LEASE_SECONDS = 900

# Priority for a meeting re-enqueued after its batch job failed terminally on
# Gemini. Mid-band: ahead of brand-new non-urgent work, behind urgent meetings.
QUEUE_PRIORITY_BATCH_RETRY = 100

# How often to sweep zombie processing rows back to pending, and what counts
# as "stuck." Anything claimed but not heartbeating for STALE_MINUTES gets
# reset on each sweep tick. Keep STALE_MINUTES > the longest legitimate job
# duration (p99 meeting ~10min) so we never preempt healthy long-runners.
STALE_SWEEP_INTERVAL = 300
STALE_SWEEP_MINUTES = 15

PUBLIC_COMMENT_SIGNATURE_THRESHOLD = 20

# Cache libc handle for malloc_trim. glibc retains freed heap arenas by default;
# malloc_trim(0) forces release back to the kernel after large transient allocations.
# None on non-glibc platforms (macOS dev, musl) -- feature is a no-op there.
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
    _libc.malloc_trim(0)  # Probe -- raises AttributeError if unavailable
    _MALLOC_TRIM: Optional[ctypes.CDLL] = _libc
except (OSError, AttributeError):
    _MALLOC_TRIM = None


def _release_memory_to_os() -> None:
    """Return freed glibc heap arenas to the kernel. No-op on non-glibc."""
    if _MALLOC_TRIM is not None:
        try:
            _MALLOC_TRIM.malloc_trim(0)
        except OSError:
            pass

MEETING_SPECIFIC_PARTICIPATION_KEYS = (
    'virtual_url', 'meeting_id', 'streaming_urls', 'is_hybrid', 'is_virtual_only'
)


def filter_participation_for_city(
    parsed: Dict[str, Any], city_has_participation: bool
) -> Dict[str, Any]:
    """Filter participation data based on city configuration.

    When city has centralized participation, only keep meeting-specific fields
    (virtual_url, streaming) - skip email/phone which are noise from PDFs.
    """
    if not city_has_participation:
        return parsed
    return {k: v for k, v in parsed.items() if k in MEETING_SPECIFIC_PARTICIPATION_KEYS}


def is_likely_public_comment_compilation(
    extraction_result: Dict[str, Any],
    url_path: str
) -> bool:
    """Detect public comment compilations via signature patterns.

    Pre-extraction filtering by attachment name is handled by is_public_comment_attachment().
    This is a post-extraction fallback for documents that slipped past the name filter.
    Page count and OCR ratio are not reliable signals -- legitimate contracts and bid
    documents routinely have hundreds of pages and scanned attachments.
    """
    text = extraction_result.get("text", "")

    if len(text) > 5000:
        sincerely_count = text.lower().count("sincerely,")
        if sincerely_count > PUBLIC_COMMENT_SIGNATURE_THRESHOLD:
            logger.info("skipping likely comment compilation - repetitive signatures", url_path=url_path, signature_count=sincerely_count)
            return True

    return False


class Processor:
    """Queue processing and item assembly orchestrator"""

    def __init__(
        self,
        db: Database,
        analyzer: Optional[AsyncAnalyzer] = None,
        metrics: Optional[MetricsCollector] = None,
    ):
        self.db = db
        self.metrics = metrics or NullMetrics()
        # Use asyncio.Event for proper async-safe shutdown signaling
        self._shutdown_event = asyncio.Event()
        self._running = True  # Internal state, use property for access

        # Global PDF extraction semaphore — shared across all concurrent meetings.
        # Cap at 8 subprocesses total (each may run OCR on 3 vCPU / 4GB box).
        # Bumped 6 -> 8 after swap was doubled 6Gi -> 13Gi (2026-05-20); modest
        # increase to relieve the extraction bottleneck without piling on RSS.
        self._pdf_semaphore = asyncio.Semaphore(8)

        # Background sweep that reclaims rows abandoned in 'processing' by
        # earlier crashes/SIGKILLs. The startup-only reset misses jobs that
        # cross the staleness threshold *after* the new processor starts, so
        # this picks them up periodically.
        self._stale_sweep_task: Optional[asyncio.Task] = None

        # Batch API lane: a small worker pool that drains non-urgent meeting
        # jobs through Gemini's Batch endpoint (50% cost, separate quota).
        # Separate slots from JOB_CONCURRENCY -- batch jobs park on poll
        # loops for minutes-to-hours and must never starve the streaming lane.
        self._batch_lane_task: Optional[asyncio.Task] = None
        self._batch_lane_warned = False

        # Batch collector: polls submitted batch_jobs rows and ingests results
        # once Gemini reports a terminal state. Decoupled from the submit lane
        # so a slow job never pins a slot -- the submit path writes a durable
        # row and returns; this loop owns completion (and survives restarts).
        self._collector_task: Optional[asyncio.Task] = None
        self._batch_collector_id = f"processor-{uuid.uuid4()}"
        self._active_run_id: Optional[int] = None
        self._batch_collector_semaphore = asyncio.Semaphore(
            BATCH_COLLECTOR_CONCURRENCY
        )

        if analyzer is not None:
            self.analyzer = analyzer
        else:
            try:
                self.analyzer = AsyncAnalyzer(api_key=config.get_api_key(), metrics=self.metrics)
                logger.info("initialized with async llm analyzer", has_analyzer=True)
            except ValueError:
                logger.warning("llm analyzer not available, summaries will be skipped", has_analyzer=False)
                self.analyzer = None

        self._streaming_job_runner = JobRunner(db, self._execute_streaming_job)
        self._batch_job_runner = JobRunner(db, self._execute_batch_queue_job)

    @property
    def is_running(self) -> bool:
        """Thread-safe running state check"""
        return self._running and not self._shutdown_event.is_set()

    @is_running.setter
    def is_running(self, value: bool):
        """Set running state (triggers shutdown event if False)"""
        self._running = value
        if not value:
            self._shutdown_event.set()
        else:
            self._shutdown_event.clear()

    def _ensure_stale_sweep_running(self) -> None:
        """Start the periodic stale-job sweep if it isn't already running.

        Lazy-started on the first process_queue/process_city_jobs call so the
        task lives inside the same event loop and respects the same shutdown
        signal as the work it's protecting. Idempotent — safe to call from
        every entry point.
        """
        if self._stale_sweep_task is not None and not self._stale_sweep_task.done():
            return
        self._stale_sweep_task = asyncio.create_task(self._periodic_stale_reset())

    async def _periodic_stale_reset(self) -> None:
        """Reset stuck 'processing' rows on a fixed interval until shutdown."""
        logger.info("stale sweep started",
                    interval_seconds=STALE_SWEEP_INTERVAL,
                    stale_minutes=STALE_SWEEP_MINUTES)
        try:
            while self.is_running:
                # First sleep, then sweep -- gives the startup-time reset (which
                # the conductor already runs) time to act, and avoids a double-
                # sweep race when both fire within the same second.
                if await self._wait_with_shutdown_check(STALE_SWEEP_INTERVAL):
                    break
                try:
                    count = await self.db.queue.reset_stale_processing_jobs(
                        stale_minutes=STALE_SWEEP_MINUTES
                    )
                    if count:
                        logger.warning("stale sweep reclaimed jobs", count=count)
                except Exception as e:  # Intentionally broad: sweep must survive any DB hiccup
                    logger.error("stale sweep failed",
                                 error=str(e), error_type=type(e).__name__)
        finally:
            logger.info("stale sweep stopped")

    def _streaming_lane_kwargs(self) -> dict:
        """Dequeue kwargs for the streaming lane (no-op when batch lane is off)."""
        if not config.BATCH_API_ENABLED:
            return {}
        return {
            "lane": "streaming",
            "urgent_past_days": config.BATCH_URGENT_PAST_DAYS,
            "urgent_future_days": config.BATCH_URGENT_FUTURE_DAYS,
        }

    def _ensure_batch_lane_running(self) -> None:
        """(Re)start the Batch API worker lane if enabled.

        Called every main-loop iteration: a lane that died on an unexpected
        error gets logged and restarted instead of leaving batch-eligible
        jobs pending forever while streaming hums along.
        """
        if not config.BATCH_API_ENABLED:
            return
        if not self.analyzer:
            # Streaming (lane-filtered) never claims batch-eligible jobs, so
            # without an analyzer they'd sit pending invisibly. Warn once.
            if not self._batch_lane_warned:
                self._batch_lane_warned = True
                logger.warning(
                    "batch lane enabled but analyzer unavailable; "
                    "batch-eligible jobs will not be claimed"
                )
            return
        if self._batch_lane_task is not None:
            if not self._batch_lane_task.done():
                return
            if not self._batch_lane_task.cancelled():
                exc = self._batch_lane_task.exception()
                if exc is not None:
                    logger.error(
                        "batch lane died unexpectedly, restarting",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
        self._batch_lane_task = asyncio.create_task(self._batch_lane_loop())

    async def _batch_lane_loop(self) -> None:
        """Drain non-urgent meeting jobs through the Gemini Batch API.

        Claims meeting jobs whose date falls outside the urgent window and
        runs them with use_batch=True: 50% token cost, separate quota pool,
        turnaround measured in minutes-to-hours. One independent worker per
        slot — each claims its next job as soon as it finishes the last, so
        one slow meeting never idles the other slots behind a barrier.
        """
        logger.info(
            "batch lane started",
            concurrency=config.BATCH_JOB_CONCURRENCY,
            urgent_window_days=(config.BATCH_URGENT_PAST_DAYS, config.BATCH_URGENT_FUTURE_DAYS),
            timeout_seconds=config.BATCH_JOB_TIMEOUT_SECONDS,
        )
        try:
            await self._run_batch_submitters()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("batch lane stopped")

    async def _run_batch_submitters(
        self,
        bananas: Optional[List[str]] = None,
        *,
        finite: bool = False,
        stats: Optional[Counter] = None,
    ) -> None:
        """Run the shared batch submit worker pool.

        Scope and stop policy are the only daemon/CLI differences: daemon
        workers wait for future work; finite workers return when their scoped
        queue is dry.  Claiming and job execution are otherwise identical.
        """
        await asyncio.gather(
            *(
                self._batch_lane_worker(
                    slot, bananas=bananas, finite=finite, stats=stats
                )
                for slot in range(config.BATCH_JOB_CONCURRENCY)
            )
        )

    async def _batch_lane_worker(
        self,
        slot: int,
        bananas: Optional[List[str]] = None,
        *,
        finite: bool = False,
        stats: Optional[Counter] = None,
    ) -> None:
        """Claim-and-run loop for one batch lane slot.

        Mirrors the streaming loop's per-iteration error containment: a
        transient claim failure (or an escaped failure-marking error) backs
        off and continues instead of killing the worker.
        """
        consecutive_errors = 0
        while self.is_running:
            try:
                job = await self.db.queue.get_next_for_processing(
                    bananas=bananas,
                    lane="batch",
                    urgent_past_days=config.BATCH_URGENT_PAST_DAYS,
                    urgent_future_days=config.BATCH_URGENT_FUTURE_DAYS,
                )
                if not job:
                    if finite:
                        return
                    # Lazy poll cadence — nothing in this lane is time-sensitive
                    if await self._wait_with_shutdown_check(60):
                        break
                    continue

                logger.info("batch lane claimed job", slot=slot, queue_id=job.id)
                outcome = await self._run_batch_job(job)
                if stats is not None:
                    stats[outcome] += 1
                consecutive_errors = 0

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "batch lane worker error",
                    slot=slot,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                consecutive_errors += 1
                if finite and consecutive_errors >= 3:
                    if stats is not None:
                        stats["worker_errors"] += 1
                    return
                if await self._wait_with_shutdown_check(QUEUE_FATAL_ERROR_BACKOFF):
                    break

    async def _run_batch_job(self, job: QueueJob) -> str:
        """Run a batch-lane claim through the same durable job policy."""
        job_start = time.time()
        outcome = await self._batch_job_runner.run(
            job,
            policy=JobExecutionPolicy(
                timeout_seconds=config.BATCH_JOB_TIMEOUT_SECONDS,
                lane="batch",
            ),
            run_id=self._active_run_id,
        )
        status = "completed" if outcome.is_success else "failed"
        self.metrics.queue_jobs_processed.labels(
            job_type="meeting_batch", status=status
        ).inc()
        logger.info(
            "batch lane job finished",
            queue_id=job.id,
            meeting_id=getattr(job.payload, "meeting_id", None),
            outcome=outcome.status.value,
            duration_seconds=round(time.time() - job_start, 1),
        )
        return status

    def _ensure_collector_running(self) -> None:
        """(Re)start the batch collector loop if the Batch API is enabled.

        Called every main-loop iteration alongside the batch lane: a collector
        that died on an unexpected error gets logged and restarted, so submitted
        jobs never strand uncollected while the rest of the pipeline hums along.
        """
        if not config.BATCH_API_ENABLED or not self.analyzer:
            return  # the batch lane's warn-once already covers the no-analyzer case
        if self._collector_task is not None:
            if not self._collector_task.done():
                return
            if not self._collector_task.cancelled():
                exc = self._collector_task.exception()
                if exc is not None:
                    logger.error(
                        "batch collector died unexpectedly, restarting",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
        self._collector_task = asyncio.create_task(self._collector_loop())

    async def _collector_loop(self) -> None:
        """Poll submitted batch jobs until shutdown, ingesting terminal ones."""
        logger.info("batch collector started", poll_interval=BATCH_COLLECTOR_POLL_INTERVAL)
        try:
            await self._run_batch_collector()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("batch collector stopped")

    async def _run_batch_collector(
        self,
        bananas: Optional[List[str]] = None,
        *,
        finite: bool = False,
        submissions_done: Optional[asyncio.Event] = None,
        stats: Optional[Counter] = None,
    ) -> None:
        """Run the shared leased collector with continuous or finite policy."""
        while self.is_running:
            try:
                await self._collect_batch_tick(bananas=bananas, stats=stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "batch collector tick failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if stats is not None:
                    stats["collector_errors"] += 1

            if finite and submissions_done and submissions_done.is_set():
                if await self.db.batch_jobs.count_open_for_bananas(bananas or []) == 0:
                    return

            # While submitters are active, check frequently so newly created
            # provider jobs start collecting immediately.  Once submission is
            # dry, durable next_poll_at controls provider traffic; this short DB
            # check only detects the final terminal transition promptly.
            delay = (
                1
                if finite and submissions_done and not submissions_done.is_set()
                else BATCH_COLLECTOR_POLL_INTERVAL
            )
            if await self._wait_with_shutdown_check(delay):
                return

    async def _collect_batch_tick(
        self,
        bananas: Optional[List[str]] = None,
        stats: Optional[Counter] = None,
    ) -> None:
        """Claim and concurrently poll one bounded set of due batch rows."""
        if bananas is None:
            jobs = await self.db.batch_jobs.claim_open(
                collector_id=self._batch_collector_id,
                limit=BATCH_COLLECTOR_BATCH_SIZE,
                lease_seconds=BATCH_COLLECTOR_LEASE_SECONDS,
            )
        else:
            jobs = await self.db.batch_jobs.claim_open_for_bananas(
                bananas,
                collector_id=self._batch_collector_id,
                limit=BATCH_COLLECTOR_BATCH_SIZE,
                lease_seconds=BATCH_COLLECTOR_LEASE_SECONDS,
            )

        async def collect(job: Dict[str, Any]) -> str:
            async with self._batch_collector_semaphore:
                return await self._collect_one_job(job)

        outcomes = await asyncio.gather(
            *(collect(job) for job in jobs), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                logger.error(
                    "leased batch collection escaped",
                    error=str(outcome),
                    error_type=type(outcome).__name__,
                )
                if stats is not None:
                    stats["collector_errors"] += 1
            elif stats is not None:
                stats[outcome] += 1

    async def _collect_one_job(self, job: Dict[str, Any]) -> str:
        """Poll one submitted batch job; ingest, re-enqueue, or leave running.

        Never cancels: a still-running job is simply re-polled next tick. A
        transient poll/download/ingest error is swallowed (the row stays
        'submitted' and we retry next tick) -- only Gemini's own terminal
        verdict moves a job out of flight.
        """
        job_id = job["id"]
        meeting_id = job["meeting_id"]
        try:
            state, results = await self.analyzer.summarizer.collect_item_batch(
                job["gemini_job_name"], job["item_ids"]
            )
        except Exception as e:
            consecutive = await self.db.batch_jobs.mark_transient_failure(
                job_id, f"poll/download: {type(e).__name__}: {e}"
            )
            logger.warning(
                "batch collect poll failed",
                job_id=job_id,
                consecutive_errors=consecutive,
                error=str(e),
                error_type=type(e).__name__,
            )
            return "transient_failure"

        if state == "running":
            await self.db.batch_jobs.mark_polled(
                job_id, poll_after_seconds=BATCH_COLLECTOR_POLL_INTERVAL
            )
            return "running"

        if state == "failed":
            # Gemini's terminal failure. Mark the row failed and re-enqueue the
            # meeting so the enqueue gate re-runs its still-unsummarized items.
            if not await self._requeue_batch_meeting(meeting_id, job.get("banana")):
                await self.db.batch_jobs.mark_transient_failure(
                    job_id, "Gemini terminal failure; meeting requeue failed"
                )
                return "transient_failure"
            await self.db.batch_jobs.mark_failed(job_id, "Gemini terminal failure")
            return "provider_failed"

        # succeeded -- ingest, then finalize once this meeting's last chunk lands
        try:
            processed, failed = await self._ingest_batch_results(meeting_id, results or [])
        except Exception as e:
            await self.db.batch_jobs.mark_transient_failure(
                job_id, f"ingest: {type(e).__name__}: {e}"
            )
            logger.error(
                "batch ingest failed",
                job_id=job_id,
                meeting_id=meeting_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return "transient_failure"

        # Item-level parse/persist failures are durable omissions, not a
        # successful meeting. Requeue before closing this chunk so the next
        # claim submits only uncovered, still-unsummarized items.
        if failed and not await self._requeue_batch_meeting(
            meeting_id, job.get("banana")
        ):
            await self.db.batch_jobs.mark_transient_failure(
                job_id, f"{failed} item failures; meeting requeue failed"
            )
            return "transient_failure"

        await self.db.batch_jobs.mark_collected(job_id)
        logger.info(
            "batch chunk ingested",
            job_id=job_id,
            meeting_id=meeting_id,
            processed=processed,
            failed=failed,
        )
        # Check after closing this row. With several chunks collecting at once,
        # a pre-close sibling count lets every worker observe another open row
        # and skip finalization. Post-close, at least the last committer sees
        # zero; duplicate finalization is harmless and preferable to omission.
        remaining = await self.db.batch_jobs.count_open_for_meeting(meeting_id)
        if remaining == 0:
            try:
                await self._finalize_batch_meeting(
                    meeting_id, job.get("meeting_meta") or {}
                )
            except Exception as exc:
                # The provider result and item writes are already durable. A
                # meeting requeue re-runs the idempotent rollup without paying
                # for another item summary.
                logger.error(
                    "batch meeting finalization failed",
                    meeting_id=meeting_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                await self._requeue_batch_meeting(meeting_id, job.get("banana"))
            await self.analyzer.summarizer.delete_shared_context_cache(
                job.get("cache_name")
            )
        return "collected"

    async def _ingest_batch_results(
        self, meeting_id: str, results: List[Dict[str, Any]]
    ) -> tuple[int, int]:
        """Write summaries/topics for a collected chunk's results.

        Re-fetches each item by id (the collector holds no in-memory item_map,
        and may even be a different process than the one that submitted), then
        persists only the immutable meeting-appearance summary and topics.
        Canonical matter projection is exclusively owned by process_matter.
        """
        processed = 0
        failed = 0
        normalizer = get_normalizer()
        for result in results:
            item_id = result["item_id"]
            if not result.get("success"):
                failed += 1
                logger.warning("item processing failed", item_id=item_id, error=result.get("error"))
                self.metrics.record_llm_call(
                    model="flash", prompt_type="item_batch", duration_seconds=0,
                    input_tokens=0, output_tokens=0, cost_dollars=0, success=False,
                )
                continue

            item = await self.db.items.get_agenda_item(item_id)
            if not item:
                logger.warning("no item found for batch result", item_id=item_id, meeting_id=meeting_id)
                continue
            if item.meeting_id != meeting_id:
                # The job's item_ids should only ever resolve to this meeting;
                # a mismatch means a stale/crossed job. Don't write into the
                # wrong meeting's item.
                logger.warning(
                    "batch result item belongs to a different meeting, skipping",
                    item_id=item_id,
                    expected_meeting=meeting_id,
                    actual_meeting=item.meeting_id,
                )
                continue

            # Isolate per-item writes: one poison item (bad summary, DB
            # constraint) must not throw out of the whole chunk -- that would
            # block mark_collected and re-ingest the chunk forever. It stays
            # unsummarized and the enqueue gate re-runs it on the next sync.
            try:
                normalized_topics = normalizer.normalize(result.get("topics", []))
                await self.db.items.update_agenda_item(
                    item_id=item_id,
                    summary=result["summary"],
                    topics=normalized_topics,
                    prompts_version=self.analyzer.summarizer.prompts_version,
                )
                # city_matters.canonical_summary is a projection owned solely
                # by process_matter. An individual meeting appearance must not
                # race that aggregate with last-write-wins semantics. The
                # durable matter queue job performs the projection update.
            except Exception as e:
                failed += 1
                logger.error(
                    "failed to persist batch item summary",
                    item_id=item_id,
                    meeting_id=meeting_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                self.metrics.record_llm_call(
                    model="flash", prompt_type="item_batch", duration_seconds=0,
                    input_tokens=0, output_tokens=0, cost_dollars=0, success=False,
                )
                continue

            self.metrics.record_llm_call(
                model="flash", prompt_type="item_batch", duration_seconds=0,
                input_tokens=0, output_tokens=0, cost_dollars=0, success=True,
            )
            processed += 1
        return processed, failed

    async def _finalize_batch_meeting(
        self, meeting_id: str, meeting_meta: Dict[str, Any]
    ) -> None:
        """Roll a fully-collected meeting up to meeting-level summary metadata.

        Runs once the meeting's last batch chunk is ingested. Re-reads the
        meeting's now-summarized items from the DB (rather than threading an
        in-memory list through submit->collect, which a restart would lose),
        aggregates topics, merges the submit-time participation, and stamps the
        meeting processed -- the same finalization the inline path does at the
        tail of _process_meeting_with_items.
        """
        meeting = await self.db.meetings.get_meeting(meeting_id)
        if not meeting:
            logger.warning("batch finalize: meeting gone", meeting_id=meeting_id)
            return

        items = await self.db.items.get_agenda_items(meeting_id)
        processed_items = [
            {"sequence": it.sequence, "title": it.title, "summary": it.summary, "topics": it.topics or []}
            for it in items if it.summary
        ]
        if not processed_items:
            logger.warning("batch finalize: no summarized items", meeting_id=meeting_id)
            return

        meeting_topics = self._aggregate_meeting_topics(processed_items)

        participation_data = (meeting_meta or {}).get("participation") or {}
        merged_participation = None
        if participation_data or meeting.participation:
            merged_dict = meeting.participation.model_dump(exclude_none=True) if meeting.participation else {}
            if participation_data:
                merged_dict.update(participation_data)
            merged_participation = ParticipationInfo(**merged_dict) if merged_dict else None

        await self.db.meetings.update_meeting_summary(
            meeting_id=meeting_id,
            summary=None,
            processing_method=f"item_level_{len(processed_items)}_items",
            processing_time=0.0,
            topics=meeting_topics,
            participation=merged_participation,
        )
        logger.info("batch meeting finalized", meeting_id=meeting_id, item_count=len(processed_items))

    async def _requeue_batch_meeting(
        self, meeting_id: str, banana: Optional[str]
    ) -> bool:
        """Re-enqueue a meeting whose batch job failed terminally on Gemini.

        Idempotent via the meeting:// source_url -- the enqueue gate skips items
        that already have summaries, so this re-runs only what's still missing.
        Routed to whichever lane fits the meeting's date at claim time.
        """
        try:
            await self.db.queue.enqueue_job(
                source_url=f"meeting://{meeting_id}",
                job_type="meeting",
                payload={"meeting_id": meeting_id},
                meeting_id=meeting_id,
                banana=banana,
                priority=QUEUE_PRIORITY_BATCH_RETRY,
            )
            logger.info("re-enqueued meeting after batch failure", meeting_id=meeting_id)
            return True
        except Exception as e:
            logger.error(
                "failed to re-enqueue meeting after batch failure",
                meeting_id=meeting_id,
                error=str(e),
            )
            return False

    @property
    def batch_drain_available(self) -> bool:
        """True when this process can claim batch-lane jobs."""
        return bool(config.BATCH_API_ENABLED and self.analyzer)

    async def run_batch_supervisor(self, bananas: List[str]) -> dict:
        """Run the finite/scoped form of the daemon batch supervisor.

        Submission and collection start together and use the same worker and
        leased-collector primitives as daemon mode.  The sole policy change is
        termination: stop only after scoped submitters are dry *and* every
        scoped durable provider row is terminal.
        """
        self._ensure_stale_sweep_running()
        stats: Counter = Counter()

        logger.info(
            "finite batch supervisor started",
            cities=len(bananas),
            concurrency=config.BATCH_JOB_CONCURRENCY,
            urgent_window_days=(config.BATCH_URGENT_PAST_DAYS, config.BATCH_URGENT_FUTURE_DAYS),
        )
        submissions_done = asyncio.Event()
        collector = asyncio.create_task(
            self._run_batch_collector(
                bananas,
                finite=True,
                submissions_done=submissions_done,
                stats=stats,
            )
        )
        try:
            await self._run_batch_submitters(
                bananas, finite=True, stats=stats
            )
        finally:
            submissions_done.set()
        await collector

        logger.info(
            "finite batch supervisor complete",
            submitted_jobs=stats["completed"],
            submit_failed=stats["failed"],
            collected_chunks=stats["collected"],
            provider_failed=stats["provider_failed"],
            transient_polls=stats["transient_failure"],
        )
        return {
            "batch_processed": stats["completed"],
            "batch_failed": stats["failed"] + stats["provider_failed"],
            "batch_collected": stats["collected"],
        }

    async def drain_batch_jobs(self, bananas: List[str]) -> dict:
        """Backward-compatible thin adapter for the finite batch supervisor."""
        return await self.run_batch_supervisor(bananas)

    def _filter_document_versions(self, urls: List[str]) -> List[str]:
        """Keep only latest versions (Ver2 > Ver1, etc.)."""
        return filter_document_version_urls(urls)

    async def _execute_streaming_job(self, job: QueueJob) -> JobOutcome | Dict[str, Any]:
        """Load authoritative state and execute one streaming-lane descriptor."""
        if isinstance(job.payload, MatterJob):
            with self.metrics.processing_duration.labels(job_type="matter").time():
                return await self.process_matter(
                    job.payload.matter_id,
                    job.payload.meeting_id,
                    {"item_ids": job.payload.item_ids},
                )

        if isinstance(job.payload, MeetingJob):
            meeting = await self.db.meetings.get_meeting(job.payload.meeting_id)
            if not meeting:
                raise TerminalJobError(
                    f"meeting {job.payload.meeting_id} no longer exists"
                )
            with self.metrics.processing_duration.labels(job_type="meeting").time():
                result = await self.process_meeting(meeting)
            return result or {}

        raise TerminalJobError(f"invalid payload type: {type(job.payload).__name__}")

    async def _execute_batch_queue_job(
        self, job: QueueJob
    ) -> JobOutcome | Dict[str, Any]:
        """Submit one non-urgent meeting through the provider batch lane."""
        if not isinstance(job.payload, MeetingJob):
            raise TerminalJobError(
                f"batch lane claimed {type(job.payload).__name__}, expected MeetingJob"
            )
        meeting = await self.db.meetings.get_meeting(job.payload.meeting_id)
        if not meeting:
            raise TerminalJobError(
                f"meeting {job.payload.meeting_id} no longer exists"
            )
        result = await self.process_meeting(meeting, use_batch=True)
        return result or {}

    async def _wait_with_shutdown_check(self, seconds: float) -> bool:
        """Wait for specified seconds, but return early if shutdown is signaled.

        Returns:
            True if shutdown was signaled, False if wait completed normally
        """
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
            return True  # Shutdown signaled
        except asyncio.TimeoutError:
            return False  # Normal timeout, continue processing

    async def process_queue(self):
        """Process jobs from the processing queue continuously.

        Claims up to JOB_CONCURRENCY jobs at a time and runs them in parallel.
        Most job time is network I/O, so overlapping gives significant throughput.
        """
        concurrency = config.JOB_CONCURRENCY
        logger.info("starting queue processor", concurrency=concurrency,
                    batch_lane=config.BATCH_API_ENABLED)
        self._ensure_stale_sweep_running()
        self._ensure_batch_lane_running()
        self._ensure_collector_running()

        while self.is_running:
            try:
                self._ensure_batch_lane_running()
                self._ensure_collector_running()

                # Claim up to `concurrency` jobs (streaming lane only when the
                # batch lane is on -- it claims the non-urgent meetings itself)
                batch = []
                for _ in range(concurrency):
                    job = await self.db.queue.get_next_for_processing(**self._streaming_lane_kwargs())
                    if not job:
                        break
                    batch.append(job)

                if not batch:
                    if await self._wait_with_shutdown_check(QUEUE_POLL_INTERVAL):
                        logger.info("shutdown signaled during queue poll")
                        break
                    continue

                logger.info("processing queue batch", batch_size=len(batch))
                await asyncio.gather(
                    *[self._dispatch_and_process_job(job, job.id) for job in batch]
                )

            except (ProcessingError, LLMError, ExtractionError) as e:
                logger.error("queue processor error", error=str(e), error_type=type(e).__name__)
                if await self._wait_with_shutdown_check(QUEUE_FATAL_ERROR_BACKOFF):
                    break
            except Exception as e:
                logger.error(
                    "unexpected queue processor error",
                    error=str(e),
                    error_type=type(e).__name__
                )
                if await self._wait_with_shutdown_check(QUEUE_FATAL_ERROR_BACKOFF):
                    break

        logger.info("queue processor stopped")

    async def process_city_jobs(self, city_banana: str) -> dict:
        """Process all queued jobs for a specific city with concurrent execution.

        Runs up to JOB_CONCURRENCY jobs in parallel. Most job time is network I/O
        (PDF downloads, LLM API calls), so overlapping jobs gives significant
        throughput gains even on single-core machines.
        """
        logger.info("processing queued jobs for city", city=city_banana, concurrency=config.JOB_CONCURRENCY)
        self._ensure_stale_sweep_running()
        stats = Counter()

        async def run_single_job(job) -> None:
            queue_id = job.id
            job_type = job.job_type
            job_start = time.time()
            job_timeout = config.JOB_TIMEOUT_SECONDS

            try:
                if job_type == "meeting":
                    from pipeline.models import MeetingJob
                    if not isinstance(job.payload, MeetingJob):
                        raise ValueError("Invalid payload type for meeting job")
                    meeting = await self.db.meetings.get_meeting(job.payload.meeting_id)
                    if not meeting:
                        await self.db.queue.mark_processing_failed(queue_id, "Meeting not found")
                        stats["failed"] += 1
                        self.metrics.queue_jobs_processed.labels(job_type="meeting", status="failed").inc()
                        return
                    with self.metrics.processing_duration.labels(job_type="meeting").time():
                        # Wall-clock ceiling: if process_meeting hangs (LLM stall,
                        # aiohttp cleanup deadlock, orphaned threads holding the
                        # loop), we cancel and mark the row failed instead of
                        # pinning a worker forever. Caught below as TimeoutError.
                        item_stats = await asyncio.wait_for(
                            self.process_meeting(meeting),
                            timeout=job_timeout,
                        )
                    await self.db.queue.mark_processing_complete(queue_id)
                    stats["processed"] += 1
                    if item_stats:
                        stats["items_processed"] += item_stats.get("items_processed", 0)
                        stats["items_new"] += item_stats.get("items_new", 0)
                        stats["items_skipped"] += item_stats.get("items_skipped", 0)
                        stats["items_failed"] += item_stats.get("items_failed", 0)
                    logger.info("processed meeting", meeting_id=job.payload.meeting_id, duration_seconds=round(time.time() - job_start, 1))
                    self.metrics.queue_jobs_processed.labels(job_type=job_type, status="completed").inc()

                elif job_type == "matter":
                    from pipeline.models import MatterJob
                    if not isinstance(job.payload, MatterJob):
                        raise ValueError("Invalid payload type for matter job")
                    with self.metrics.processing_duration.labels(job_type="matter").time():
                        matter_stats = await asyncio.wait_for(
                            self.process_matter(job.payload.matter_id, job.payload.meeting_id, {"item_ids": job.payload.item_ids}),
                            timeout=job_timeout,
                        )
                    await self.db.queue.mark_processing_complete(queue_id)
                    stats["processed"] += 1
                    stats["items_processed"] += matter_stats.get("items_processed", 0)
                    stats["items_new"] += matter_stats.get("items_new", 0)
                    stats["items_skipped"] += matter_stats.get("items_skipped", 0)
                    stats["items_failed"] += matter_stats.get("items_failed", 0)
                    logger.info("processed matter", matter_id=job.payload.matter_id, duration_seconds=round(time.time() - job_start, 1))
                    self.metrics.queue_jobs_processed.labels(job_type=job_type, status="completed").inc()

                else:
                    raise ValueError(f"Unknown job type: {job_type}")

            except asyncio.TimeoutError:
                error_msg = f"Job exceeded {job_timeout}s wall-clock timeout"
                await self.db.queue.mark_processing_failed(queue_id, error_msg)
                stats["failed"] += 1
                self.metrics.queue_jobs_processed.labels(job_type=job_type, status="failed").inc()
                logger.error(
                    "job timed out",
                    queue_id=queue_id,
                    job_type=job_type,
                    meeting_id=getattr(job.payload, "meeting_id", None),
                    timeout_seconds=job_timeout,
                    duration_seconds=round(time.time() - job_start, 1),
                )

            except (ProcessingError, LLMError, ExtractionError) as e:
                await self.db.queue.mark_processing_failed(queue_id, str(e))
                stats["failed"] += 1
                self.metrics.queue_jobs_processed.labels(job_type=job_type, status="failed").inc()
                self.metrics.record_error(component="processor", error=e)
                logger.error("job processing failed", queue_id=queue_id, job_type=job_type, duration_seconds=round(time.time() - job_start, 1), error=str(e))

            except Exception as e:
                await self.db.queue.mark_processing_failed(queue_id, str(e))
                stats["failed"] += 1
                self.metrics.queue_jobs_processed.labels(job_type=job_type, status="failed").inc()
                self.metrics.record_error(component="processor", error=e)
                logger.error("unexpected job failure", queue_id=queue_id, job_type=job_type, error=str(e), error_type=type(e).__name__, duration_seconds=round(time.time() - job_start, 1))

        # Claim and run jobs in batches of JOB_CONCURRENCY
        concurrency = config.JOB_CONCURRENCY
        while True:
            # Claim up to `concurrency` jobs atomically (streaming lane only;
            # the global batch lane handles this city's non-urgent meetings)
            batch = []
            for _ in range(concurrency):
                job = await self.db.queue.get_next_for_processing(
                    banana=city_banana, **self._streaming_lane_kwargs()
                )
                if not job:
                    break
                batch.append(job)

            if not batch:
                break

            logger.info("processing job batch", city=city_banana, batch_size=len(batch))
            await asyncio.gather(*[run_single_job(j) for j in batch])

        logger.info("processing complete for city", city=city_banana, meetings_succeeded=stats["processed"], meetings_failed=stats["failed"], items_processed=stats["items_processed"], items_new=stats["items_new"], items_skipped=stats["items_skipped"], items_failed=stats["items_failed"])

        return {
            "processed_count": stats["processed"],
            "failed_count": stats["failed"],
            "items_processed": stats["items_processed"],
            "items_new": stats["items_new"],
            "items_skipped": stats["items_skipped"],
            "items_failed": stats["items_failed"],
        }

    async def _process_single_item(self, item, banana: Optional[str] = None):
        """Process a single agenda item. Returns dict with success/summary/topics or None.

        banana is corpus provenance only, passed from process_matter's validated
        jurisdiction (the item row itself doesn't carry one)."""
        if not self.analyzer:
            raise ProcessingError(
                "Analyzer not initialized",
                context={"component": "processor", "function": "_process_single_item"}
            )

        # Skip filtered items (procedural, ceremonial, administrative)
        skip_reason = get_skip_reason(item.title)
        if skip_reason:
            logger.debug("skipping item", title=item.title[:50], reason=skip_reason)
            await self.db.items.update_filter_reason(item.id, skip_reason)
            return None

        if not item.attachments and not item.body_text:
            logger.debug("no attachments or body text for item", title=item.title[:50])
            return None

        # Collect valid attachments to extract
        attachments_to_extract = []
        for att in item.attachments:
            att_url, att_type, att_name = att.url, att.type, att.name

            if att_type not in ("pdf", "doc", "document", "unknown") or not att_url:
                continue

            if att_name and is_public_comment_attachment(att_name):
                logger.info("skipping low-value attachment", name=att_name)
                continue

            attachments_to_extract.append((att_url, att_name))

        # Concurrent PDF extraction (global semaphore caps total across all meetings)
        semaphore = self._pdf_semaphore

        async def extract_attachment(att_url: str, att_name: Optional[str]) -> Optional[tuple[str, str, int]]:
            async with semaphore:
                try:
                    result = await self.analyzer.extract_pdf_async(att_url, banana=banana)
                    if result.get("success") and result.get("text"):
                        if is_likely_public_comment_compilation(result, att_name or att_url):
                            logger.info("skipping public comment compilation", name=att_name or att_url)
                            return None
                        logger.debug("extracted attachment text", attachment=att_name or att_url, pages=result.get('page_count', 0), chars=len(result['text']))
                        return (att_name or att_url, result["text"], result.get("page_count", 0))
                    return None
                except (ExtractionError, OSError, IOError) as e:
                    logger.warning("failed to extract attachment", name=att_name or att_url, error=str(e))
                    return None

        # Extract text from attachments concurrently
        item_parts = []
        total_page_count = 0

        if attachments_to_extract:
            results = await asyncio.gather(
                *[extract_attachment(url, name) for url, name in attachments_to_extract],
                return_exceptions=True
            )

            for result in results:
                if isinstance(result, BaseException):
                    logger.error("unexpected extraction exception", error=str(result))
                    continue
                if result:
                    name, text, page_count = result
                    item_parts.append(f"=== {name} ===\n{text}")
                    total_page_count += page_count

        if not item_parts:
            logger.warning("no text extracted for item", title=item.title[:50])
            raise ProcessingError(
                "No text extracted from agenda item",
                context={"item_id": item.id, "item_title": item.title[:100]}
            )

        combined_text = "\n\n".join(item_parts)

        batch_request = [{
            "item_id": item.id,
            "title": item.title,
            "text": combined_text,
            "sequence": item.sequence,
            "page_count": total_page_count if total_page_count > 0 else None,
        }]

        try:
            async for chunk_results in self.analyzer.process_batch_items_async(batch_request, shared_context=None, meeting_id=None):
                if chunk_results:
                    result = chunk_results[0]
                    if result.get("success"):
                        normalized_topics = get_normalizer().normalize(result.get("topics", []))
                        return {"success": True, "summary": result["summary"], "topics": normalized_topics}
                    else:
                        raise ProcessingError(f"Item processing failed: {result.get('error', 'Unknown error')}", context={"item_id": item.id})
        except LLMError as e:
            raise ProcessingError(f"Item processing failed: {e}", context={"item_id": item.id}) from e

        raise ProcessingError("No results returned from batch processing", context={"item_id": item.id})

    async def process_matter(self, matter_id: str, meeting_id: str, metadata: Optional[Dict] = None) -> Dict[str, int]:
        """Process a matter across all its appearances, updating canonical summary."""
        logger.info("processing matter", matter_id=matter_id)
        empty_stats = {"items_processed": 0, "items_new": 0, "items_skipped": 0, "items_failed": 0}

        if not validate_matter_id(matter_id):
            logger.error("invalid matter_id format", matter_id=matter_id)
            return empty_stats

        banana = extract_banana_from_matter_id(matter_id)
        if not banana:
            logger.error("could not extract banana from matter_id", matter_id=matter_id)
            return empty_stats

        items = []
        if metadata:
            for item_id in metadata.get("item_ids", []):
                item = await self.db.items.get_agenda_item(item_id)
                if item:
                    items.append(item)

        if not items:
            logger.warning("no items in payload, querying database", matter_id=matter_id)
            items = await self.db.items.get_all_items_for_matter(matter_id)

        if not items:
            logger.error("no items found for matter", matter_id=matter_id)
            return empty_stats

        all_attachments = []
        seen_urls = set()
        for item in items:
            for att in (item.attachments or []):
                if att.url and att.url not in seen_urls:
                    seen_urls.add(att.url)
                    all_attachments.append(att)

        representative_item = items[0]
        representative_item.attachments = all_attachments

        logger.info("matter aggregation complete", matter_id=matter_id, appearances=len(items), unique_attachments=len(all_attachments))

        if not all_attachments:
            logger.debug("matter skipped - no attachments", matter_id=matter_id)
            return {"items_processed": len(items), "items_new": 0, "items_skipped": len(items), "items_failed": 0}

        # sv1 identity is invariant under url_refresh, so hashing before the
        # refresh below sees the same value the enqueue decider compared.
        attachment_hash = hash_substantive_attachments(all_attachments)
        existing_matter = await self.db.matters.get_matter(matter_id)
        stored_hash = (
            existing_matter.metadata.attachment_hash
            if existing_matter and existing_matter.metadata
            else None
        )
        hash_current = stored_hash is not None and (
            stored_hash == attachment_hash
            or (
                ":" not in stored_hash
                and stored_hash == hash_substantive_attachments_legacy(all_attachments)
            )
        )

        # Unchanged gate: the enqueue decider applies this same comparison at
        # sync time, but a job claimed today may have been enqueued weeks ago
        # under conditions another run has since resolved. Re-checking here
        # costs one row read; summarizing costs extraction + LLM.
        if existing_matter and existing_matter.canonical_summary and hash_current:
            if ":" not in (stored_hash or ""):
                # Legacy-format match: persist the current format so future
                # syncs compare directly.
                await self.db.matters.update_attachment_hash(matter_id, attachment_hash)
            filled = await self.db.items.bulk_fill_null_item_summaries(
                item_ids=[item.id for item in items],
                summary=existing_matter.canonical_summary,
                topics=existing_matter.canonical_topics or [],
                prompts_version=self.analyzer.summarizer.prompts_version if self.analyzer else None,
            )
            logger.info(
                "canonical summary current, skipping summarization",
                matter_id=matter_id,
                snapshots_filled=filled,
            )
            return {"items_processed": len(items), "items_new": filled, "items_skipped": len(items) - filled, "items_failed": 0}

        # Promotion: a single-appearance matter whose one snapshot is already
        # summarized needs no LLM run -- the freeze-on-summary invariant
        # (store_agenda_items) guarantees the snapshot's attachments are
        # exactly what its summary was computed from, and with one appearance
        # the aggregate set IS the snapshot set. Copy the summary upward
        # instead of regenerating it from the same documents.
        if (
            (existing_matter is None or not existing_matter.canonical_summary)
            and len(items) == 1
            and (existing_matter is None or existing_matter.appearance_count <= 1)
            and items[0].summary
        ):
            matter_obj = Matter(
                id=matter_id,
                banana=banana,
                matter_id=existing_matter.matter_id if existing_matter else None,
                matter_file=representative_item.matter_file,
                matter_type=representative_item.matter_type,
                title=representative_item.title,
                sponsors=getattr(representative_item, 'sponsors', []),
                canonical_summary=items[0].summary,
                canonical_topics=items[0].topics or [],
                attachments=representative_item.attachments,
                metadata=MatterMetadata(attachment_hash=attachment_hash),
                first_seen=existing_matter.first_seen if existing_matter else None,
                last_seen=existing_matter.last_seen if existing_matter else None,
                appearance_count=existing_matter.appearance_count if existing_matter else 1,
            )
            await self.db.matters.store_matter(matter_obj)
            logger.info("promoted item snapshot to canonical summary", matter_id=matter_id)
            return {"items_processed": 1, "items_new": 0, "items_skipped": 1, "items_failed": 0}

        # Terminal disposition: the processing-time title filter is stricter
        # than the sync-time MatterFilter, so a matter can pass enqueueing yet
        # never be summarizable. Without a recorded verdict it re-enqueues at
        # every sync and burns a queue cycle forever (observed: 142 such
        # matters in one city). The disposition is scoped to this attachment
        # set -- changed attachments re-open the matter.
        skip_reason = get_skip_reason(representative_item.title)
        if skip_reason:
            await self.db.items.update_filter_reason(representative_item.id, skip_reason)
            await self.db.matters.record_matter_outcome(
                matter_id, attachment_hash, disposition=f"filtered_{skip_reason}"
            )
            logger.info(
                "matter filtered, disposition recorded",
                matter_id=matter_id,
                reason=skip_reason,
            )
            return {"items_processed": len(items), "items_new": 0, "items_skipped": len(items), "items_failed": 0}

        # Refresh ephemeral signed URLs before extraction. See _process_meeting_with_items.
        city = await self.db.jurisdictions.get_city(banana)
        if city and city.vendor:
            try:
                await refresh_attachment_urls(city.vendor, city.slug, all_attachments)
            except (OSError, RuntimeError) as e:
                logger.warning("url refresh failed, falling back to stored urls", matter_id=matter_id, error=str(e))

        try:
            result = await self._process_single_item(representative_item, banana=banana)
        except ProcessingError as e:
            logger.error("matter processing failed", matter_id=matter_id, error=str(e))
            self.metrics.record_error("processor", e)
            await self.db.matters.record_matter_outcome(
                matter_id, attachment_hash, increment_attempts=True
            )
            return {"items_processed": len(items), "items_new": 0, "items_skipped": 0, "items_failed": len(items)}

        if not result:
            await self.db.matters.record_matter_outcome(
                matter_id, attachment_hash, increment_attempts=True
            )
            return {"items_processed": len(items), "items_new": 0, "items_skipped": 0, "items_failed": len(items)}

        summary = result.get("summary")
        topics = result.get("topics", [])

        if not summary:
            logger.warning("no summary generated for matter", matter_id=matter_id)
            await self.db.matters.record_matter_outcome(
                matter_id, attachment_hash, increment_attempts=True
            )
            return {"items_processed": len(items), "items_new": 0, "items_skipped": 0, "items_failed": len(items)}

        matter_obj = Matter(
            id=matter_id,
            banana=banana,
            matter_id=existing_matter.matter_id if existing_matter else None,
            matter_file=representative_item.matter_file,
            matter_type=representative_item.matter_type,
            title=representative_item.title,
            sponsors=getattr(representative_item, 'sponsors', []),
            canonical_summary=summary,
            canonical_topics=topics,
            attachments=representative_item.attachments,
            metadata=MatterMetadata(attachment_hash=attachment_hash),
            first_seen=existing_matter.first_seen if existing_matter else None,
            last_seen=existing_matter.last_seen if existing_matter else None,
            appearance_count=existing_matter.appearance_count if existing_matter else 1,
        )

        await self.db.matters.store_matter(matter_obj)

        # Temporal-snapshot invariant: each items row is the point-in-time
        # snapshot of one appearance of this matter. items.summary is frozen
        # once set. The matter's canonical summary lives on city_matters and
        # reflects the latest aggregated-attachment run.
        #
        # We DO fill items.summary for any appearance in the payload that
        # does not yet have one -- that covers the common case where a new
        # meeting introduced this matter with fresh attachments and this
        # matter job is the first thing to summarize it. Appearances that
        # already have a summary (from a meeting job, sync-time copy, or a
        # prior matter run) are left untouched.
        item_ids = [item.id for item in items]
        filled = await self.db.items.bulk_fill_null_item_summaries(
            item_ids=item_ids,
            summary=summary,
            topics=topics,
            prompts_version=self.analyzer.summarizer.prompts_version if self.analyzer else None,
        )

        logger.info(
            "stored canonical summary",
            matter_id=matter_id,
            total_appearances=len(items),
            snapshots_filled=filled,
            snapshots_preserved=len(items) - filled,
        )
        # Release any transient extracted-text arenas held during _process_single_item.
        # Big matters with many attachments can balloon peak RSS; this returns it promptly.
        _release_memory_to_os()
        return {"items_processed": len(items), "items_new": filled, "items_skipped": len(items) - filled, "items_failed": 0}

    async def _manufacture_items(self, meeting: Meeting) -> int:
        """Manufacture item shape at claim time -- the stage-2 call sync used to make.

        Mirrors the base adapter's agenda->packet policy: chunk the agenda
        PDF first (short, hyperlinked -- attachment-bearing items win), fall
        back to the packet (compiled TOC document -- body_text items), and
        keep attachment-less agenda items as a last-resort text fallback.
        Bytes come corpus-first (sync archived them even when it deferred
        chunking), download only on a corpus miss. Manufactured shape enters
        the DB through the same funnel sync uses (attach_items: junk filter,
        matter tracking, snapshot-preserving store), so downstream cannot
        tell where shape was born. Returns stored item count; 0 means no
        shape and the caller falls through to the monolithic packet path.
        """
        if not self.analyzer:
            return 0
        city = await self.db.jurisdictions.get_city(banana=meeting.banana)
        vendor = city.vendor if city else ""
        slug = city.slug if city else ""
        corpus_store = get_corpus()

        candidates: List[tuple[str, str]] = []
        if meeting.agenda_url:
            candidates.append((meeting.agenda_url, "agenda"))
        packet_urls = meeting.packet_url  # str | List[str] | None (multi-packet vendors)
        for packet_url in [packet_urls] if isinstance(packet_urls, str) else (packet_urls or []):
            candidates.append((packet_url, "packet"))

        chosen: Optional[List[Dict[str, Any]]] = None
        text_fallback: List[Dict[str, Any]] = []
        for url, ladder in candidates:
            pdf_bytes = None
            if corpus_store:
                pdf_bytes = await corpus_store.get_original_by_identity(url)
            if pdf_bytes is None:
                try:
                    pdf_bytes = await self.analyzer.download_pdf_async(url)
                except Exception as e:
                    logger.warning(
                        "shape manufacture download failed",
                        meeting_id=meeting.id,
                        url=url[:120],
                        error=str(e),
                        error_type=type(e).__name__,
                    )
                    continue

            result = await produce_ground_truth(
                pdf_bytes,
                vendor=vendor,
                slug=slug,
                ladder=ladder,
                source_url=url,
                banana=meeting.banana,
            )
            if not result.items:
                continue
            if ladder == "agenda":
                with_attachments = [it for it in result.items if it.get("attachments")]
                if with_attachments:
                    chosen = with_attachments
                    break
                text_fallback = [it for it in result.items if it.get("body_text")]
            else:
                chosen = result.items
                break

        items_data = chosen or text_fallback
        if not items_data:
            return 0

        stored = await self._sync_orchestrator.attach_items(meeting, items_data)
        if stored:
            logger.info(
                "manufactured item shape at claim time",
                meeting_id=meeting.id,
                items_stored=stored,
            )
        return stored

    @property
    def _sync_orchestrator(self) -> MeetingSyncOrchestrator:
        """Lazy shared orchestrator: the processor enters the item funnel
        through the same code sync uses, one instance per processor."""
        orchestrator = getattr(self, "_sync_orchestrator_instance", None)
        if orchestrator is None:
            orchestrator = MeetingSyncOrchestrator(self.db)
            self._sync_orchestrator_instance = orchestrator
        return orchestrator

    async def _diverts_to_packet(self, meeting: Meeting) -> bool:
        """True when the chunk audit smells under-split and a packet exists.

        Under-split = the document's own numbered headings far outnumber the
        extracted items, so the slices are probably wrong merges. One honest
        monolithic summary beats N confident summaries of wrong slices. Items
        stay stored (unsummarized); only the summarization strategy diverts.
        """
        if not meeting.packet_url:
            return False
        try:
            quality = await self.db.queue.get_chunk_quality(meeting.id)
        except Exception as e:
            logger.debug("chunk quality lookup failed", meeting_id=meeting.id, error=str(e))
            return False
        return bool(quality) and quality.get("seg_smell") == "under_split"

    async def process_meeting(self, meeting: Meeting, use_batch: bool = False):
        """Process summary for a single meeting (items > packet fallback).

        use_batch routes item summarization through the Gemini Batch API
        (50% cost, separate quota, slow turnaround) instead of concurrent
        streaming calls. The monolithic packet fallback always streams --
        it's a single LLM call either way.
        """
        empty_stats = {"items_processed": 0, "items_new": 0, "items_skipped": 0, "items_failed": 0}

        try:
            agenda_items = await self.db.items.get_agenda_items(meeting.id)

            if agenda_items:
                # Check if any items have processable content (attachments or body text).
                # Bare HTML titles without content cannot produce summaries -- fall through
                # to packet processing instead.
                has_content = any(item.attachments or item.body_text for item in agenda_items)
                if has_content and await self._diverts_to_packet(meeting):
                    logger.info(
                        "chunk audit smells under-split, preferring monolithic packet summary",
                        item_count=len(agenda_items),
                        meeting_title=meeting.title,
                    )
                elif has_content:
                    logger.info("found items for meeting", item_count=len(agenda_items), meeting_title=meeting.title)
                    if not self.analyzer:
                        logger.warning("analyzer not available")
                        return empty_stats
                    return await self._process_meeting_with_items(meeting, agenda_items, use_batch=use_batch)
                else:
                    logger.info("items exist but lack content, falling through to packet", item_count=len(agenda_items), meeting_title=meeting.title)

            # Manufacture shape at claim time (the collapse: chunking is a
            # produce-ground-truth concern, not a sync concern). Meetings
            # arrive here item-less either because sync deferred chunking
            # (SYNC_CHUNKING=false) or because the vendor exposes only
            # documents and sync's chunk found nothing. Same producer, same
            # item funnel -- shape born here is indistinguishable from shape
            # born at sync. Gated to ZERO existing items: manufactured item
            # IDs are chunk-derived and would coexist with, not replace, a
            # different-shaped existing set. Falls through to the monolithic
            # packet path when no shape can be manufactured.
            if not agenda_items:
                manufactured = await self._manufacture_items(meeting)
                if manufactured:
                    agenda_items = await self.db.items.get_agenda_items(meeting.id)
                    if agenda_items and self.analyzer:
                        return await self._process_meeting_with_items(meeting, agenda_items, use_batch=use_batch)

            if meeting.packet_url:
                # Item-level processing has a per-item guard (_filter_processed_items);
                # the monolithic packet call's only guard is the sync-time enqueue
                # gate. Retried or manually requeued jobs can arrive here with a
                # summary already stored -- re-read and skip rather than re-burn
                # the most expensive single call in the pipeline. Deliberate
                # re-summarization requires nulling meetings.summary first.
                stored = await self.db.meetings.get_meeting(meeting.id)
                if stored and stored.summary:
                    logger.info("packet already summarized, skipping", meeting_title=meeting.title)
                    return {"items_processed": 1, "items_new": 0, "items_skipped": 1, "items_failed": 0}

                logger.info("processing packet as monolithic unit - no items found", meeting_title=meeting.title)
                if not self.analyzer:
                    logger.warning("skipping meeting - analyzer not available", packet_url=meeting.packet_url)
                    return empty_stats

                meeting_data = {
                    "packet_url": meeting.packet_url,
                    "city_banana": meeting.banana,
                    "meeting_name": meeting.title,
                    "meeting_date": meeting.date.isoformat() if meeting.date else None,
                    "meeting_id": meeting.id,
                }
                result = await self.analyzer.process_agenda_with_cache_async(meeting_data)
                if result.get("success"):
                    await self.db.meetings.update_meeting_summary(
                        meeting_id=meeting.id,
                        summary=result.get("summary"),
                        processing_method=result.get("processing_method") or "pymupdf_gemini",
                        processing_time=result.get("processing_time") or 0.0,
                        topics=None,
                        participation=result.get("participation"),
                    )
                    logger.info("processed packet", packet_url=meeting.packet_url, processing_time_seconds=round(result['processing_time'], 1))
                    return {"items_processed": 1, "items_new": 1, "items_skipped": 0, "items_failed": 0}
                else:
                    logger.error("failed to process packet", packet_url=meeting.packet_url, error=result.get('error'))
                    return {"items_processed": 0, "items_new": 0, "items_skipped": 0, "items_failed": 1}

            return empty_stats

        except (ProcessingError, LLMError, ExtractionError) as e:
            logger.error("error processing summary", packet_url=meeting.packet_url, error=str(e))
            if use_batch:
                # Batch submit failures must reach the queue runner so retry
                # state changes instead of being misclassified as completed.
                raise
            return {"items_processed": 0, "items_new": 0, "items_skipped": 0, "items_failed": 1}

    async def _extract_participation_info(
        self, meeting: Meeting, city_has_participation: bool = False
    ) -> Dict[str, Any]:
        """Extract participation info from agenda_url (PDF or HTML)."""
        if not meeting.agenda_url:
            return {}

        try:
            agenda_url_lower = meeting.agenda_url.lower()

            if agenda_url_lower.endswith('.pdf') or '.ashx' in agenda_url_lower:
                if not self.analyzer:
                    logger.warning("analyzer not initialized, skipping participation extraction")
                    return {}
                agenda_result = await self.analyzer.extract_pdf_async(meeting.agenda_url, banana=meeting.banana)
                if agenda_result.get("success") and agenda_result.get("text"):
                    agenda_participation = parse_participation_info(agenda_result["text"][:5000])
                    if agenda_participation:
                        parsed = agenda_participation.model_dump(exclude_none=True)
                        return filter_participation_for_city(parsed, city_has_participation)

            elif meeting.participation:
                return meeting.participation.model_dump(exclude_none=True)

        except (ExtractionError, OSError, IOError) as e:
            logger.warning("failed to extract participation from agenda_url", error=str(e))

        return {}

    async def _filter_processed_items(self, agenda_items: List) -> tuple[List[Dict], List]:
        """Separate already-processed items from items needing processing.

        Under the temporal-snapshot model, an item is "already processed" iff
        its own items.summary column is populated -- there is no longer a
        read-through to matter.canonical_summary at this layer. Items with
        unchanged substantive attachments relative to a prior appearance get
        their snapshot filled at sync-time via copy_summary_from_prior_appearance;
        items whose attachments changed are handled by process_matter filling
        nulls via bulk_fill_null_item_summaries. Anything still null here is
        a genuine new appearance that needs its own LLM call.
        """
        already_processed = []
        need_processing = []

        for item in agenda_items:
            skip_reason = get_skip_reason(item.title)
            if skip_reason:
                logger.debug("skipping item", title=item.title[:50], reason=skip_reason)
                await self.db.items.update_filter_reason(item.id, skip_reason)
                continue

            if not item.attachments and not item.body_text:
                logger.debug("skipping item without attachments or body text", title=item.title[:50])
                continue

            if item.summary:
                logger.debug("item already processed", title=item.title[:50])
                already_processed.append({"sequence": item.sequence, "title": item.title, "summary": item.summary, "topics": item.topics or []})
            else:
                need_processing.append(item)

        return already_processed, need_processing

    async def _build_document_cache(self, need_processing: List, banana: Optional[str] = None) -> tuple[Dict, Dict, set]:
        """Build meeting-level document cache with version filtering and deduplication.

        banana is corpus provenance only, from the meeting row (DB truth)."""
        logger.info("building meeting-level document cache")
        document_cache = {}
        item_attachments = {}
        all_urls = set()
        url_to_items = {}
        url_to_name = {}

        for item in need_processing:
            item_urls = []
            for att in (item.attachments or []):
                if att.type in DOCUMENT_ATTACHMENT_TYPES and att.url:
                    item_urls.append(att.url)
                    if att.name and att.url not in url_to_name:
                        url_to_name[att.url] = att.name

            filtered_item_urls = self._filter_document_versions(item_urls)
            item_attachments[item.id] = filtered_item_urls

            for url in filtered_item_urls:
                all_urls.add(url)
                url_to_items.setdefault(url, []).append(item.id)

        logger.info("collected unique URLs", url_count=len(all_urls), item_count=len(need_processing))

        if not self.analyzer:
            logger.error("analyzer not initialized, cannot extract attachments")
            return {}, item_attachments, all_urls

        # Public-comment attachments are extracted but excerpted below, never
        # silently dropped: the public's position is part of the record.
        urls_to_extract = []
        comment_urls = set()
        for att_url in all_urls:
            att_name = url_to_name.get(att_url, "")
            if att_name and is_public_comment_attachment(att_name):
                comment_urls.add(att_url)
            urls_to_extract.append((att_url, att_name))

        # Concurrent PDF extraction (global semaphore caps total across all meetings)
        semaphore = self._pdf_semaphore

        async def extract_with_limit(att_url: str, att_name: str) -> tuple[str, Optional[Dict]]:
            async with semaphore:
                try:
                    result = await self.analyzer.extract_pdf_async(att_url, banana=banana)
                    if result.get("success") and result.get("text"):
                        text = result["text"]
                        page_count = result.get("page_count", 0)
                        is_comment = att_url in comment_urls or is_likely_public_comment_compilation(
                            result, att_name or att_url
                        )
                        if is_comment and len(text) > PUBLIC_COMMENT_EXCERPT_CHARS:
                            logger.info("excerpting public comment document", attachment=att_name or att_url, pages=page_count, chars=len(text))
                            text = (
                                text[:PUBLIC_COMMENT_EXCERPT_CHARS]
                                + "\n\n[PIPELINE NOTE: this attachment appears to be a public-comment"
                                f" document ({page_count} pages, {len(result['text']):,} characters);"
                                " only the excerpt above is included]"
                            )
                        return att_url, {"text": text, "page_count": page_count, "name": att_name or att_url}
                    return att_url, None
                except (ExtractionError, OSError, IOError) as e:
                    logger.warning("failed to extract document", attachment=att_name or att_url, error=str(e))
                    return att_url, None

        # Run extractions concurrently
        if urls_to_extract:
            logger.info("extracting documents concurrently", count=len(urls_to_extract))
            extraction_results = await asyncio.gather(
                *[extract_with_limit(url, name) for url, name in urls_to_extract],
                return_exceptions=True
            )

            for result in extraction_results:
                if isinstance(result, BaseException):
                    logger.error("unexpected extraction exception", error=str(result))
                    continue
                att_url, data = result
                if data:
                    document_cache[att_url] = data
                    item_count = len(url_to_items[att_url])
                    logger.info("extracted document", attachment=data["name"], pages=data.get('page_count', 0), shared=(item_count > 1))

        shared_urls = {url for url, items in url_to_items.items() if len(items) > 1 and url in document_cache}
        logger.info("cached documents", total_cached=len(document_cache), shared_count=len(shared_urls))

        return document_cache, item_attachments, shared_urls

    def _build_batch_requests(
        self,
        need_processing: List,
        document_cache: Dict,
        item_attachments: Dict,
        shared_urls: set,
        participation_data: Dict[str, Any],
        first_sequence: Optional[int],
        last_sequence: Optional[int],
        city_has_participation: bool = False,
        shared_context_chars: int = 0,
    ) -> tuple[List[Dict], Dict, List[str]]:
        """Build batch requests from cached documents."""
        batch_requests = []
        item_map = {}
        failed_items = []
        # The shared documents count toward every item's context window whether
        # they are inline or held in Gemini cached content.
        item_text_budget = max(0, MAX_ITEM_INPUT_CHARS - shared_context_chars - 1_000)

        for item in need_processing:
            try:
                doc_parts = []
                unreadable = []
                total_page_count = 0
                has_shared_attachments = False
                url_names = {
                    att.url: (att.name or att.url)
                    for att in (item.attachments or [])
                    if att.url
                }

                for att_url in item_attachments.get(item.id, []):
                    if att_url in shared_urls:
                        has_shared_attachments = True
                        continue
                    if att_url in document_cache:
                        doc = document_cache[att_url]
                        doc_parts.append((doc['name'], doc['text']))
                        total_page_count += doc['page_count']
                    else:
                        # Extraction failed or was filtered upstream: the model
                        # must know a document exists that it cannot see, or
                        # the summary silently claims more coverage than it has.
                        unreadable.append(url_names.get(att_url, att_url))

                unreadable_note = ""
                if unreadable:
                    unreadable_note = (
                        "[PIPELINE NOTE: the following attachments exist for this"
                        " item but could not be read: " + "; ".join(unreadable) + "]"
                    )

                # Items with only shared attachments can still be processed
                # using shared context + item metadata
                if doc_parts:
                    note_chars = len(unreadable_note) + 2 if unreadable_note else 0
                    combined_text, trim_notes = render_document_parts(
                        doc_parts,
                        max(0, item_text_budget - note_chars),
                    )
                    if trim_notes:
                        logger.warning("item input trimmed to budget", title=item.title[:50], notes=trim_notes)
                    if unreadable_note:
                        combined_text += "\n\n" + unreadable_note
                elif has_shared_attachments:
                    # Item relies on shared context - use description or title as anchor
                    desc = getattr(item, 'description', '') or ''
                    combined_text = f"[Item: {item.title}]\n{desc}".strip() if desc else f"[Item: {item.title}]"
                    logger.debug("item uses shared attachments only", title=item.title[:50])
                elif item.body_text:
                    combined_text = truncate_text_to_budget(
                        item.body_text,
                        max(0, item_text_budget - len(unreadable_note) - 2),
                    )
                    if unreadable_note:
                        combined_text += "\n\n" + unreadable_note
                    logger.debug("using coversheet body text", title=item.title[:50], chars=len(combined_text))
                else:
                    logger.warning("no text extracted for item", title=item.title[:50])
                    failed_items.append(item.title)
                    continue

                # Notes and coversheet fallbacks are assembled after document
                # fitting, so enforce the absolute item share one final time.
                combined_text = truncate_text_to_budget(combined_text, item_text_budget)

                if item.sequence in (first_sequence, last_sequence):
                    item_participation = parse_participation_info(combined_text)
                    if item_participation:
                        parsed = item_participation.model_dump(exclude_none=True)
                        filtered = filter_participation_for_city(parsed, city_has_participation)
                        participation_data.update(filtered)

                batch_requests.append({
                    "item_id": item.id,
                    "title": item.title,
                    "text": combined_text,
                    "sequence": item.sequence,
                    "page_count": total_page_count if total_page_count > 0 else None,
                })
                item_map[item.id] = item
                logger.debug("prepared item for batch processing", title=item.title[:50], chars=len(combined_text))

            except (KeyError, AttributeError, TypeError) as e:
                logger.error("error extracting text for item", title=item.title[:50], error=str(e))
                failed_items.append(item.title)

        return batch_requests, item_map, failed_items

    async def _process_batch_incrementally(
        self,
        batch_requests: List[Dict],
        item_map: Dict,
        shared_context: Optional[str],
        meeting_id: str,
    ) -> tuple[List[Dict], List[str]]:
        """Stream item summaries inline, saving after each chunk.

        This is the streaming lane only -- the Batch API path no longer flows
        through here: it submits fire-and-forget (see _submit_batch_for_meeting)
        and the collector ingests results later via _ingest_batch_results.
        """
        processed_items = []
        failed_items = []

        if not batch_requests or not self.analyzer:
            return processed_items, failed_items

        logger.info("submitting batch to Gemini", item_count=len(batch_requests))
        result_gen = self.analyzer.process_batch_items_async(
            batch_requests, shared_context=shared_context, meeting_id=meeting_id
        )

        async for chunk_results in result_gen:
            logger.debug("saving results from completed chunk", result_count=len(chunk_results))

            for result in chunk_results:
                item_id = result["item_id"]
                item = item_map.get(item_id)
                if not item:
                    logger.warning("no item mapping found", item_id=item_id)
                    continue

                if result["success"]:
                    normalized_topics = get_normalizer().normalize(result.get("topics", []))
                    await self.db.items.update_agenda_item(
                        item_id=item_id,
                        summary=result["summary"],
                        topics=normalized_topics,
                        prompts_version=self.analyzer.summarizer.prompts_version,
                    )

                    if item.matter_id:
                        await self._store_canonical_summary(item=item, summary=result["summary"], topics=normalized_topics)

                    processed_items.append({"sequence": item.sequence, "title": item.title, "summary": result["summary"], "topics": normalized_topics})
                    logger.info("item saved", title=item.title[:60])
                else:
                    failed_items.append(item.title)
                    logger.warning("item processing failed", title=item.title[:60], error=result.get('error'))

        return processed_items, failed_items

    async def _submit_batch_for_meeting(
        self,
        meeting: Meeting,
        batch_requests: List[Dict],
        shared_context: Optional[str],
        participation_data: Optional[Dict[str, Any]],
    ) -> int:
        """Submit a meeting's items to the Batch API and persist the job rows.

        Fire-and-forget: creates the shared-context cache (if any), submits one
        batch job per chunk, and writes a durable batch_jobs row per chunk so
        the collector can ingest results later -- even across a restart. The
        slot and document cache are freed by the caller immediately after.

        Returns the number of chunks successfully submitted (0 on total
        failure, which leaves items unsummarized for the enqueue gate to retry).
        The submit-time participation rides along in meeting_meta so the
        collector can apply it at finalization without re-deriving it.
        """
        cache_name = await self.analyzer.summarizer.create_shared_context_cache(
            shared_context, meeting.id
        )
        meeting_meta = {"participation": participation_data or {}}

        async def reserve(descriptor: Dict[str, Any]) -> bool:
            return await self.db.batch_jobs.reserve_submission(
                submission_key=descriptor["submission_key"],
                meeting_id=meeting.id,
                item_ids=descriptor["item_ids"],
                chunk_num=descriptor["chunk_num"],
                banana=meeting.banana,
                cache_name=cache_name,
                prompts_version=self.analyzer.summarizer.prompts_version,
                meeting_meta=meeting_meta,
            )

        async def activate(descriptor: Dict[str, Any]) -> None:
            await self.db.batch_jobs.activate_submission(
                submission_key=descriptor["submission_key"],
                gemini_job_name=descriptor["gemini_job_name"],
                submit_attempts=descriptor.get("attempts", 1),
            )

        async def fail(descriptor: Dict[str, Any]) -> None:
            await self.db.batch_jobs.mark_submission_intent_failed(
                submission_key=descriptor["submission_key"],
                error_message=descriptor.get("error") or "Batch submission failed",
                submit_attempts=descriptor.get("attempts", 1),
            )

        descriptors = await self.analyzer.summarizer.submit_item_batches(
            batch_requests,
            cache_name=cache_name,
            shared_context=shared_context,
            submission_scope=meeting.id,
            reserve_submission=reserve,
            record_submission=activate,
            fail_submission=fail,
            include_failures=True,
        )
        submitted = [
            descriptor
            for descriptor in descriptors
            if descriptor.get("gemini_job_name") and not descriptor.get("error")
        ]
        failures = [descriptor for descriptor in descriptors if descriptor.get("error")]
        already_reserved = [
            descriptor for descriptor in descriptors if descriptor.get("already_reserved")
        ]

        provider_created = [
            descriptor for descriptor in descriptors if descriptor.get("gemini_job_name")
        ]
        if not provider_created:
            # This newly-created cache is unused when another submitter already
            # owns every chunk or every provider call failed.
            await self.analyzer.summarizer.delete_shared_context_cache(cache_name)

        if failures:
            # Successful siblings are already durable and can collect normally.
            # Raising retries the queue promptly; the next attempt excludes
            # their covered item_ids and submits only the failed chunks.
            raise ProcessingError(
                "one or more batch chunks failed to submit",
                context={
                    "meeting_id": meeting.id,
                    "failed_chunks": [d["chunk_num"] for d in failures],
                    "submitted_chunks": len(submitted),
                },
            )

        if not submitted and not already_reserved:
            # Nothing made it to Gemini (API down, quota, etc.). Raise so the
            # queue job fails and retries promptly, rather than silently
            # completing and waiting for the next scrape to re-enqueue.
            raise ProcessingError(
                "batch submission produced no jobs",
                context={"meeting_id": meeting.id, "items": len(batch_requests)},
            )
        return len(submitted)

    async def _store_canonical_summary(self, item, summary: str, topics: List[str]) -> None:
        """Store canonical summary for matter deduplication across meetings."""
        banana = extract_banana_from_matter_id(item.matter_id)
        if not banana:
            logger.warning("could not extract banana from matter_id", matter_id=item.matter_id)
            return

        existing_matter = await self.db.matters.get_matter(item.matter_id)
        matter_obj = Matter(
            id=item.matter_id,
            banana=banana,
            matter_id=existing_matter.matter_id if existing_matter else None,
            matter_file=item.matter_file,
            matter_type=item.matter_type,
            title=item.title,
            sponsors=getattr(item, 'sponsors', []),
            canonical_summary=summary,
            canonical_topics=topics,
            attachments=item.attachments,
            metadata=MatterMetadata(attachment_hash=hash_substantive_attachments(item.attachments or [])),
            first_seen=existing_matter.first_seen if existing_matter else None,
            last_seen=existing_matter.last_seen if existing_matter else None,
            appearance_count=existing_matter.appearance_count if existing_matter else 1,
        )

        await self.db.matters.store_matter(matter_obj)
        logger.info("stored canonical summary", matter_id=item.matter_id)

    def _aggregate_meeting_topics(self, processed_items: List[Dict]) -> List[str]:
        """Aggregate topics from processed items, sorted by frequency."""
        topic_counts = Counter(topic for item in processed_items for topic in item.get("topics", []))
        meeting_topics = [topic for topic, _ in topic_counts.most_common()]
        logger.info("aggregated meeting topics", unique_topic_count=len(meeting_topics), item_count=len(processed_items))
        return meeting_topics

    async def _process_meeting_with_items(self, meeting: Meeting, agenda_items: List, use_batch: bool = False):
        """Process meeting at item-level granularity."""
        start_time = time.time()

        if not self.analyzer:
            logger.warning("analyzer not available")
            return {"items_processed": 0, "items_new": 0, "items_skipped": 0, "items_failed": 0}

        # Check if city has centralized participation configured
        city = await self.db.jurisdictions.get_city(meeting.banana)
        city_has_participation = bool(city and city.participation)

        item_sequences = [item.sequence for item in agenda_items]
        first_sequence = min(item_sequences) if item_sequences else None
        last_sequence = max(item_sequences) if item_sequences else None

        participation_data = await self._extract_participation_info(meeting, city_has_participation)
        already_processed, need_processing = await self._filter_processed_items(agenda_items)
        processed_items = list(already_processed)
        failed_items = []

        if not need_processing:
            logger.info("all items already processed", item_count=len(already_processed))
        else:
            if use_batch:
                # Release pre-provider intents abandoned by process death, then
                # exclude only item IDs owned by surviving open chunks. This is
                # item-granular so a partial submit can retry its uncovered
                # siblings while successful chunks continue running.
                await self.db.batch_jobs.expire_stale_submission_intents(meeting.id)
                covered_ids = await self.db.batch_jobs.get_open_item_ids_for_meeting(
                    meeting.id
                )
                if covered_ids:
                    covered_count = sum(
                        1 for item in need_processing if str(item.id) in covered_ids
                    )
                    need_processing = [
                        item for item in need_processing if str(item.id) not in covered_ids
                    ]
                    logger.info(
                        "excluded items already owned by open batch chunks",
                        meeting_id=meeting.id,
                        covered_items=covered_count,
                        uncovered_items=len(need_processing),
                    )
                    if not need_processing:
                        return {
                            "items_processed": 0,
                            "items_new": 0,
                            "items_skipped": len(already_processed),
                            "items_failed": 0,
                            "items_submitted": 0,
                        }

            logger.info("extracting text from items for batch processing", item_count=len(need_processing))

            # Refresh ephemeral signed URLs (CivicClerk SAS, etc.) just before fetch.
            # Stored att.url is whatever the vendor returned at scrape time and may
            # be hours-to-weeks expired; durable identifiers on AttachmentInfo let
            # url_refresh resolve a fresh URL via the vendor API.
            if city and city.vendor:
                all_atts = [att for it in need_processing for att in (it.attachments or [])]
                if all_atts:
                    try:
                        await refresh_attachment_urls(city.vendor, city.slug, all_atts)
                    except (OSError, RuntimeError) as e:
                        logger.warning("url refresh failed, falling back to stored urls", banana=meeting.banana, error=str(e))

            document_cache, item_attachments, shared_urls = await self._build_document_cache(need_processing, banana=meeting.banana)

            shared_context = None
            if shared_urls:
                shared_parts = [
                    (document_cache[url]["name"], document_cache[url]["text"])
                    for url in sorted(shared_urls)
                ]
                shared_context, shared_trim_notes = render_document_parts(
                    shared_parts,
                    MAX_SHARED_CONTEXT_CHARS,
                )
                if shared_trim_notes:
                    logger.warning(
                        "shared context trimmed to budget",
                        notes=shared_trim_notes,
                    )
                logger.info("built meeting-level shared context", chars=len(shared_context), shared_document_count=len(shared_urls))

            batch_requests, item_map, failed_items = self._build_batch_requests(
                need_processing, document_cache, item_attachments, shared_urls,
                participation_data, first_sequence, last_sequence,
                city_has_participation, len(shared_context or ""),
            )

            if batch_requests and use_batch:
                # Decoupled Batch API path: submit fire-and-forget and hand off
                # to the collector. The meeting is intentionally NOT finalized
                # here -- its items have no summaries yet; the collector writes
                # them and runs meeting-level finalization once the last chunk
                # lands (see _collect_one_job -> _finalize_batch_meeting).
                submitted_chunks = await self._submit_batch_for_meeting(
                    meeting, batch_requests, shared_context, participation_data
                )
                document_cache.clear()
                _release_memory_to_os()
                logger.info(
                    "batch submitted, deferring summaries to collector",
                    meeting_id=meeting.id,
                    chunks=submitted_chunks,
                    items=len(batch_requests),
                )
                return {
                    "items_processed": 0,
                    "items_new": 0,
                    "items_skipped": len(already_processed),
                    "items_failed": len(failed_items),
                    "items_submitted": len(batch_requests) if submitted_chunks else 0,
                }

            if batch_requests:
                new_processed, new_failed = await self._process_batch_incrementally(batch_requests, item_map, shared_context, meeting.id)
                processed_items.extend(new_processed)
                failed_items.extend(new_failed)

            # Free memory immediately - document_cache can be 100MB+ for meetings with many large PDFs
            document_cache.clear()
            # Force glibc to release the now-empty arenas back to the kernel.
            # Without this, Python's peak RSS stays pinned at whatever the largest
            # meeting needed, and the parent conductor grows monotonically.
            _release_memory_to_os()

        if processed_items and self.analyzer:
            meeting_topics = self._aggregate_meeting_topics(processed_items)

            merged_participation = None
            if participation_data or meeting.participation:
                merged_dict = meeting.participation.model_dump(exclude_none=True) if meeting.participation else {}
                if participation_data:
                    merged_dict.update(participation_data)
                merged_participation = ParticipationInfo(**merged_dict) if merged_dict else None

            processing_time = time.time() - start_time
            await self.db.meetings.update_meeting_summary(
                meeting_id=meeting.id,
                summary=None,
                processing_method=f"item_level_{len(processed_items)}_items",
                processing_time=processing_time,
                topics=meeting_topics,
                participation=merged_participation,
            )

            skipped_count = len(already_processed)
            new_count = len(processed_items) - skipped_count
            logger.info("item processing completed", processed_count=len(processed_items), new_items=new_count, skipped_items=skipped_count, failed_count=len(failed_items), processing_time_seconds=round(processing_time, 1))

            return {"items_processed": len(processed_items), "items_new": new_count, "items_skipped": skipped_count, "items_failed": len(failed_items)}

        logger.warning("no items could be processed")
        return {"items_processed": 0, "items_new": 0, "items_skipped": 0, "items_failed": 0}

    async def close(self):
        """Cleanup resources (HTTP sessions, background tasks)"""
        if self._batch_lane_task is not None and not self._batch_lane_task.done():
            self._batch_lane_task.cancel()
            try:
                await self._batch_lane_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._collector_task is not None and not self._collector_task.done():
            # Cancelling mid-poll only abandons the GET loop; submitted jobs are
            # durable rows, so the next process to run the collector resumes them.
            self._collector_task.cancel()
            try:
                await self._collector_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stale_sweep_task is not None and not self._stale_sweep_task.done():
            self._stale_sweep_task.cancel()
            try:
                await self._stale_sweep_task
            except (asyncio.CancelledError, Exception) as e:
                logger.debug("stale sweep task cleanup", error=str(e))
        if self.analyzer:
            await self.analyzer.close()
            logger.debug("analyzer http session closed")
        await AsyncSessionManager.close_all()
        logger.debug("vendor http sessions closed")
