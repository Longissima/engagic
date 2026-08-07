"""
Pipeline Conductor - Lightweight orchestration

Coordinates:
- Sync loop (via Fetcher)
- Processing loop (via Processor)
- Admin commands (force sync, status)

Pure async architecture - no threading, uses asyncio.create_task()
"""


import asyncio
import logging
import signal
import sys
from contextlib import contextmanager
from typing import Dict, Any, Optional, List, AsyncGenerator, Iterable, cast

from database.db_postgres import Database
from database.models import Jurisdiction
from exceptions import ProcessingError
from pipeline.fetcher import Fetcher, SyncResult, SyncStatus
from pipeline.models import MatterJob, MeetingJob
from pipeline.processor import Processor
from pipeline.protocols import MetricsCollector
from pipeline.click_types import BANANA

from config import get_logger

logger = get_logger(__name__).bind(component="engagic")

# Shutdown polling interval (seconds)
SHUTDOWN_POLL_INTERVAL = 1
PROCESSOR_RESTART_DELAY_SECONDS = 10
PROCESSOR_ANALYZER_ERROR = (
    "Analyzer not available; use the fetcher command for sync-only service"
)
OOM_PROTECTED_COMMANDS = frozenset(
    {
        "sync",
        "process",
        "sync-and-process",
        "full-sync",
        "sync-watchlist",
        "process-watchlist",
        "fetcher",
        "extract-text",
        "preview-items",
        "daemon",
        "processor",
    }
)


async def _await_daemon_tasks(*tasks: asyncio.Task[Any]) -> None:
    """Await sibling daemon loops and preserve the first terminal failure.

    ``asyncio.gather`` returns as soon as one child raises but leaves its
    siblings running. Cancel and drain every sibling before propagating the
    original exception so cleanup is complete and the service exits nonzero.
    """
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def get_sync_status_snapshot(
    db: Database,
    *,
    is_running: bool = False,
    failed_cities: Iterable[str] = (),
) -> Dict[str, Any]:
    """Read status without constructing the fetcher and processor runtimes."""
    stats = await db.get_stats()
    pipeline = await db.pipeline_lifecycle.get_operational_snapshot()
    failures = list(failed_cities)
    return {
        "is_running": is_running,
        "active_cities": stats.get("active_cities", 0),
        "total_meetings": stats.get("total_meetings", 0),
        "summarized_meetings": stats.get("summarized_meetings", 0),
        "pending_meetings": stats.get("pending_meetings", 0),
        "failed_cities": failures,
        "failed_count": len(failures),
        "pipeline": pipeline,
    }


async def get_queue_preview(
    db: Database,
    *,
    city_banana: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Read queued work and its display metadata without runtime construction."""
    logger.info("previewing queue")
    jobs = await db.queue.preview_jobs(banana=city_banana, limit=limit)

    previews = []
    for job in jobs[:limit]:
        matter_id = None
        if isinstance(job.payload, MeetingJob):
            meeting_id = job.payload.meeting_id
            meeting = await db.meetings.get_meeting(meeting_id)
            if not meeting:
                continue
            title = meeting.title
            date = meeting.date.isoformat() if meeting.date else None
        elif isinstance(job.payload, MatterJob):
            meeting_id = None
            matter_id = job.payload.matter_id
            matter = await db.matters.get_matter(matter_id)
            if not matter:
                continue
            title = matter.title
            date = matter.last_seen.isoformat() if matter.last_seen else None
        else:
            continue
        previews.append(
            {
                "queue_id": job.id,
                "job_type": job.job_type,
                "meeting_id": meeting_id,
                "matter_id": matter_id,
                "city_banana": job.banana,
                "title": title,
                "date": date,
                "priority": job.priority,
                "status": job.status,
            }
        )

    return {"total_queued": len(jobs), "previews": previews}


def _adjust_worker_oom_score() -> None:
    """Bias the OOM killer away from a command that owns worker children.

    PDF/OCR subprocesses override their own score to +500. Keeping the parent
    at -500 makes those memory-heavy children preferable victims while leaving
    the conductor killable if broader system health is at risk.
    """
    try:
        with open("/proc/self/oom_score_adj", "w") as oom_score:
            oom_score.write("-500")
    except (PermissionError, OSError) as exc:
        logger.warning(
            "could not set oom_score_adj on conductor parent", error=str(exc)
        )


def _configure_worker_oom_score(command: Optional[str]) -> None:
    """Apply the parent OOM bias only to commands that run pipeline work."""
    if command in OOM_PROTECTED_COMMANDS:
        _adjust_worker_oom_score()


class Conductor:
    """Lightweight orchestrator for sync and processing loops"""

    def __init__(
        self,
        db: Database,
        metrics: Optional[MetricsCollector] = None,
    ):
        """Initialize the conductor

        Args:
            db: PostgreSQL database instance
            metrics: Optional metrics collector (injected from server when available)
        """
        self.db = db
        # Use asyncio.Event for proper async-safe shutdown signaling
        self._shutdown_event = asyncio.Event()
        self._running = False

        # Initialize fetcher and processor with database and metrics
        self.fetcher = Fetcher(db=db, metrics=metrics)
        logger.info("fetcher initialized")

        self.processor = Processor(db=db, metrics=metrics)
        logger.info(
            "processor initialized",
            has_analyzer=self.processor.analyzer is not None
        )

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

    @contextmanager
    def enable_processing(self):
        """Context manager for temporarily enabling processing state.

        Does NOT restore state if shutdown was signaled during context -
        prevents accidentally re-enabling after shutdown.
        """
        old_state = self.is_running
        self.is_running = True
        self.fetcher.is_running = True
        self.processor.is_running = True
        try:
            yield
        finally:
            # Only restore old state if shutdown wasn't signaled
            # If shutdown was triggered, keep everything stopped
            if not self._shutdown_event.is_set():
                self.is_running = old_state
                self.fetcher.is_running = old_state
                self.processor.is_running = old_state
            else:
                logger.debug("shutdown signaled, not restoring processing state")

    async def close(self):
        """Cleanup resources (HTTP sessions)"""
        await self.processor.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def get_sync_status(self) -> Dict[str, Any]:
        """Get current sync status"""
        return await get_sync_status_snapshot(
            self.db,
            is_running=self.is_running,
            failed_cities=self.fetcher.failed_cities,
        )

    async def force_sync_city(self, city_banana: str) -> SyncResult:
        """Force sync a specific city

        Args:
            city_banana: City identifier

        Returns:
            SyncResult object
        """
        with self.enable_processing():
            results = await self.run_sync_cycle(
                [city_banana], command="sync-cli"
            )
            result = results[0]

            # Update failed cities tracking
            if result.status == SyncStatus.FAILED:
                self.fetcher.failed_cities.add(city_banana)
            else:
                # Remove from failed set if it succeeds
                self.fetcher.failed_cities.discard(city_banana)

            return result

    async def _heartbeat_run(self, run_id: int) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self.db.pipeline_lifecycle.heartbeat_run(run_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("sync run heartbeat failed", run_id=run_id, error=str(exc))

    async def recover_stale_processing_claims(self) -> int:
        """Compatibility adapter for the processor-owned recovery primitive."""
        return await self.processor.recover_stale_processing_claims()

    async def run_processing_daemon(
        self, *, restart_delay_seconds: float = PROCESSOR_RESTART_DELAY_SECONDS
    ) -> None:
        """Supervise the canonical continuous processor until shutdown.

        A failed runtime closes its durable run as failed; this supervisor then
        starts a fresh run after a bounded delay. That keeps the combined daemon
        alive *and* processing instead of leaving only its sync task running.
        """
        if not self.processor.analyzer:
            raise ProcessingError(PROCESSOR_ANALYZER_ERROR)

        while self.is_running and self.processor.is_running:
            try:
                logger.info("starting processing runtime")
                await self.processor.process_queue()
                if self.is_running and self.processor.is_running:
                    logger.error("processing runtime returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Intentionally broad: daemon supervision
                logger.error(
                    "processing runtime failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

            if not self.is_running or not self.processor.is_running:
                break
            logger.info(
                "restarting processing runtime",
                delay_seconds=restart_delay_seconds,
            )
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=restart_delay_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def run_sync_cycle(
        self,
        city_bananas: Optional[List[str]],
        *,
        command: str,
    ) -> List[SyncResult]:
        """Run one canonical sync cycle for either CLI or daemon."""
        run = await self.db.pipeline_lifecycle.start_run(
            command,
            targets=city_bananas,
            metadata={"scope": "all" if city_bananas is None else "targets"},
        )
        run_id = int(run["id"])
        stage_id = await self.db.pipeline_lifecycle.start_stage(
            attempt_id=None,
            run_id=run_id,
            stage="sync.cycle",
            metrics={"target_count": len(city_bananas or [])},
        )
        heartbeat = asyncio.create_task(self._heartbeat_run(run_id))
        try:
            results = (
                await self.fetcher.sync_all()
                if city_bananas is None
                else await self.fetcher.sync_cities(city_bananas)
            )
            outbox_published = await self.processor.publish_due_outbox(city_bananas)
            succeeded = sum(result.status is SyncStatus.COMPLETED for result in results)
            failed = sum(result.status is SyncStatus.FAILED for result in results)
            cancelled = sum(
                result.status is SyncStatus.CANCELLED for result in results
            )
            skipped = len(results) - succeeded - failed - cancelled
            metrics = {
                "jurisdictions": len(results),
                "succeeded": succeeded,
                "failed": failed,
                "skipped": skipped,
                "cancelled": cancelled,
                "meetings_found": sum(result.meetings_found for result in results),
                "items_stored": sum(result.items_stored for result in results),
                "outbox_published": outbox_published,
            }
            await self.db.pipeline_lifecycle.finish_stage(
                stage_id,
                status="succeeded" if failed == 0 and cancelled == 0 else "failed",
                metrics=metrics,
            )
            run_status = (
                "cancelled"
                if cancelled
                else "completed" if failed == 0 else "failed"
            )
            run_error = (
                None
                if failed == 0 and cancelled == 0
                else (
                    f"{cancelled} jurisdiction sync(s) cancelled"
                    if cancelled
                    else f"{failed} jurisdiction sync(s) failed"
                )
            )
            await self.db.pipeline_lifecycle.finish_run(
                run_id, run_status, error_message=run_error
            )
            return results
        except asyncio.CancelledError:
            await self.db.pipeline_lifecycle.finish_stage(
                stage_id,
                status="failed",
                error_type="CancelledError",
                error_message="sync cycle cancelled",
            )
            await self.db.pipeline_lifecycle.finish_run(run_id, "cancelled")
            raise
        except Exception as exc:
            await self.db.pipeline_lifecycle.finish_stage(
                stage_id,
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            await self.db.pipeline_lifecycle.finish_run(
                run_id, "failed", error_message=f"{type(exc).__name__}: {exc}"
            )
            raise
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def sync_and_process_city(self, city_banana: str) -> Dict[str, Any]:
        """Sync a city and immediately process all its queued jobs

        Args:
            city_banana: City identifier

        Returns:
            Dictionary with sync_result and processing stats
        """
        logger.info("starting sync-and-process", city=city_banana)

        # Step 1: Sync the city (fetches meetings, stores, enqueues)
        sync_result = await self.force_sync_city(city_banana)

        if sync_result.status != SyncStatus.COMPLETED:
            logger.error(
                "sync failed for city",
                city=city_banana,
                error=sync_result.error_message
            )
            return {
                "sync_status": sync_result.status.value,
                "sync_error": sync_result.error_message,
                "meetings_found": sync_result.meetings_found,
                "processed_count": 0,
            }

        logger.info(
            "sync complete",
            meetings_found=sync_result.meetings_found
        )

        # Step 2: Process all queued jobs for this city
        if not self.processor.analyzer:
            logger.warning(
                "analyzer not available - meetings queued but not processed"
            )
            return {
                "sync_status": sync_result.status.value,
                "meetings_found": sync_result.meetings_found,
                "processed_count": 0,
                "warning": "Analyzer not available",
            }

        logger.info("processing queued jobs", city=city_banana)

        with self.enable_processing():
            processing_stats = await self.processor.process_city_jobs(city_banana)

            return {
                "sync_status": sync_result.status.value,
                "meetings_found": sync_result.meetings_found,
                "processed_count": processing_stats["processed_count"],
                "failed_count": processing_stats["failed_count"],
            }

    async def sync_cities(self, city_bananas: List[str]) -> List[Dict[str, Any]]:
        """Sync multiple cities (fetches meetings, enqueues for processing)

        Args:
            city_bananas: List of city banana identifiers

        Returns:
            List of sync results
        """
        logger.info("syncing cities", city_count=len(city_bananas))
        results = await self.run_sync_cycle(city_bananas, command="sync-cli")

        # Convert SyncResult objects to dicts
        return [
            {
                "city_banana": r.city_banana,
                "status": r.status.value,
                "meetings_found": r.meetings_found,
                "meetings_processed": r.meetings_processed,
                "items_stored": r.items_stored,
                "duration": r.duration_seconds,
                "error": r.error_message,
            }
            for r in results
        ]

    async def process_cities(self, city_bananas: List[str]) -> AsyncGenerator[Dict[str, Any], None]:
        """Drain one jurisdiction scope through the canonical pipeline runtime.

        Args:
            city_bananas: List of city banana identifiers

        Yields:
            Per-city compatibility summaries, then explicit batch lifecycle totals
        """
        logger.info("processing queued jobs for cities", city_count=len(city_bananas))

        if not self.processor.analyzer:
            logger.warning("analyzer not available - cannot process meetings")
            return  # Generator yields nothing if analyzer unavailable

        with self.enable_processing():
            stats = await self.processor.run_pipeline_runtime(
                bananas=city_bananas,
                continuous=False,
                command="process-cli",
            )

        by_banana = stats.get("by_banana", {})
        for banana in city_bananas:
            city = by_banana.get(banana, {})
            yield {
                "city_banana": banana,
                "processed": city.get("processed", 0),
                "failed": city.get("failed", 0),
                "items_processed": city.get("items_processed", 0),
                "items_new": city.get("items_new", 0),
                "items_skipped": city.get("items_skipped", 0),
                "items_failed": city.get("items_failed", 0),
            }

        yield {
            "phase": "batch_complete",
            "batch_queue_completed": stats.get("batch_queue_completed", 0),
            "batch_chunks_collected": stats.get("batch_chunks_collected", 0),
            "batch_failed": stats.get("batch_failed", 0),
            # Temporary output compatibility; callers should move to the
            # explicit queue/chunk lifecycle counters above.
            "batch_processed": stats.get(
                "batch_processed", stats.get("batch_queue_completed", 0)
            ),
        }

    async def sync_and_process_cities(self, city_bananas: List[str]) -> AsyncGenerator[Dict[str, Any], None]:
        """Sync multiple cities and immediately process all their meetings.

        Args:
            city_bananas: List of city banana identifiers

        Yields:
            Sync results first, then per-city processing results
        """
        logger.info("sync and process cities", city_count=len(city_bananas))

        # Step 1: Sync all cities
        sync_results = await self.sync_cities(city_bananas)
        total_meetings = sum(r["meetings_found"] for r in sync_results)
        total_items = sum(r["items_stored"] for r in sync_results)

        logger.info("sync complete", total_meetings=total_meetings, total_items=total_items, city_count=len(city_bananas))

        # Yield sync summary
        yield {
            "phase": "sync_complete",
            "sync_results": sync_results,
            "total_meetings_found": total_meetings,
            "total_items_stored": total_items,
        }

        # Step 2: Process all queued jobs, yielding per-city results
        async for result in self.process_cities(city_bananas):
            yield result

    async def preview_queue(self, city_banana: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
        """Preview queued jobs without processing them

        Args:
            city_banana: Optional city filter
            limit: Max jobs to show

        Returns:
            List of queued jobs with meeting info
        """
        return await get_queue_preview(
            self.db,
            city_banana=city_banana,
            limit=limit,
        )



# CLI commands create their own Conductor instances


def _parse_city_list(arg: str) -> List[str]:
    """Helper to parse city list (supports comma-separated or @file)"""
    if arg.startswith("@"):
        file_path = arg[1:]
        with open(file_path, "r") as f:
            cities = []
            for line in f:
                line = line.split('#')[0].strip()
                if line:
                    cities.append(line)
            return cities
    return [c.strip() for c in arg.split(",") if c.strip()]


async def _expand_jurisdictions(db: Database, bananas: List[str]) -> List[str]:
    """Expand county and state inputs into the full jurisdiction set.

    Three input forms are accepted per entry:
      - State code (2 uppercase letters, e.g. "GA"): all jurisdictions in that state
      - County banana (type='county'): the county plus every linked city
      - Anything else: passed through as a literal banana

    Preserves order, deduplicates.
    """
    unique_inputs = list(dict.fromkeys(bananas))
    state_codes = [
        banana
        for banana in unique_inputs
        if len(banana) == 2 and banana.isalpha() and banana.isupper()
    ]
    state_code_set = set(state_codes)
    literal_bananas = [
        banana for banana in unique_inputs if banana not in state_code_set
    ]

    resolved, state_expansions = await asyncio.gather(
        db.jurisdictions.get_cities_by_bananas(literal_bananas),
        db.jurisdictions.get_cities_by_states(state_codes),
    )
    county_inputs = [
        banana
        for banana in literal_bananas
        if (city := resolved.get(banana)) is not None and city.type == "county"
    ]
    county_expansions = await db.jurisdictions.get_county_jurisdictions_batch(
        county_inputs
    )

    expanded: List[str] = []
    seen: set[str] = set()
    processed_inputs: set[str] = set()
    for banana in bananas:
        if banana in processed_inputs or banana in seen:
            continue
        processed_inputs.add(banana)

        # State code: 2 uppercase letters
        if len(banana) == 2 and banana.isalpha() and banana.isupper():
            state_jurisdictions = state_expansions.get(banana, [])
            state_bananas = [j.banana for j in state_jurisdictions]
            logger.info("expanded state", state=banana, jurisdictions=len(state_bananas))
            candidates = state_bananas
        elif (city := resolved.get(banana)) is not None and city.type == "county":
            candidates = county_expansions.get(banana, [])
            logger.info(
                "expanded county", county=banana, jurisdictions=len(candidates)
            )
        else:
            # Known non-county and unknown literals both pass through. Fetcher
            # is the canonical place that classifies missing/unsupported cities.
            candidates = [banana]

        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return expanded


async def _partition_known_jurisdictions(
    db: Database, bananas: List[str]
) -> tuple[List[str], List[str], Dict[str, Jurisdiction]]:
    """Batch-partition jurisdiction IDs, preserving first-seen order."""
    ordered = list(dict.fromkeys(bananas))
    resolved = await db.jurisdictions.get_cities_by_bananas(ordered)
    valid = [banana for banana in ordered if banana in resolved]
    unknown = [banana for banana in ordered if banana not in resolved]
    return valid, unknown, resolved


def main():
    """Entry point for engagic-conductor and engagic-daemon CLI"""
    import click
    import json

    # Configure logging for CLI usage
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )

    @click.group(invoke_without_command=True)
    @click.pass_context
    def cli(ctx):
        """Background processor for engagic"""
        # Long-running and memory-intensive commands own subprocess workers;
        # read-only inspection and help should neither mutate /proc nor warn.
        _configure_worker_oom_score(ctx.invoked_subcommand)
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    @cli.command("sync")
    @click.argument("targets")
    def sync(targets):
        """Sync jurisdictions (comma-separated bananas, state codes, or @file path).

        Each entry is interpreted as one of:
          - 2-letter state code (GA): every jurisdiction in that state
          - County banana (montereycountyCA): the county plus all linked cities
          - City banana (paloaltoCA): that city
        """
        city_list = _parse_city_list(targets)

        async def run():
            db = await Database.create()
            try:
                expanded = await _expand_jurisdictions(db, city_list)
                click.echo(f"Syncing {len(expanded)} jurisdictions: {', '.join(expanded)}")
                async with Conductor(db) as conductor:
                    return await conductor.sync_cities(expanded)
            finally:
                await db.close()

        results = asyncio.run(run())
        click.echo(json.dumps(results, indent=2))

    @cli.command("process")
    @click.argument("targets")
    def process(targets):
        """Process queued jobs for jurisdictions (comma-separated bananas, state codes, or @file path).

        Each entry is interpreted as one of:
          - 2-letter state code (GA): every jurisdiction in that state
          - County banana (montereycountyCA): the county plus all linked cities
          - City banana (paloaltoCA): that city
        """
        city_list = _parse_city_list(targets)

        async def run():
            db = await Database.create()
            totals = {"processed": 0, "failed": 0, "items_processed": 0, "items_new": 0,
                      "batch_queue_completed": 0, "batch_chunks_collected": 0,
                      "batch_failed": 0}
            try:
                async with Conductor(db) as conductor:
                    expanded = await _expand_jurisdictions(db, city_list)
                    click.echo(f"Processing queued jobs for {len(expanded)} jurisdictions: {', '.join(expanded)}")
                    async for result in conductor.process_cities(expanded):
                        if result.get("phase") == "batch_complete":
                            totals["batch_queue_completed"] += result.get(
                                "batch_queue_completed", 0
                            )
                            totals["batch_chunks_collected"] += result.get(
                                "batch_chunks_collected", 0
                            )
                            totals["batch_failed"] += result.get("batch_failed", 0)
                            continue
                        # Stream results - log each city as it completes
                        city = result.get("city_banana", "unknown")
                        logger.info("city complete",
                            city=city,
                            meetings=result.get("processed", 0),
                            items=result.get("items_processed", 0),
                            new=result.get("items_new", 0),
                        )
                        # Accumulate totals for final summary
                        totals["processed"] += result.get("processed", 0)
                        totals["failed"] += result.get("failed", 0)
                        totals["items_processed"] += result.get("items_processed", 0)
                        totals["items_new"] += result.get("items_new", 0)
                return totals
            finally:
                await db.close()

        results = asyncio.run(run())
        click.echo(
            f"Complete: {results['processed']} streaming queue jobs + "
            f"{results['batch_queue_completed']} batch queue jobs; "
            f"{results['batch_chunks_collected']} provider chunks collected; "
            f"{results['items_new']} new items"
        )

    @cli.command("sync-and-process")
    @click.argument("targets")
    def sync_and_process(targets):
        """Sync and process jurisdictions (comma-separated bananas, state codes, or @file path).

        Each entry is interpreted as one of:
          - 2-letter state code (GA): every jurisdiction in that state
          - County banana (montereycountyCA): the county plus all linked cities
          - City banana (paloaltoCA): that city
        """
        city_list = _parse_city_list(targets)

        async def run():
            db = await Database.create()
            totals = {"processed": 0, "failed": 0, "items_processed": 0, "items_new": 0,
                      "meetings_found": 0, "batch_queue_completed": 0,
                      "batch_chunks_collected": 0, "batch_failed": 0}
            try:
                expanded = await _expand_jurisdictions(db, city_list)
                click.echo(f"Syncing and processing {len(expanded)} jurisdictions: {', '.join(expanded)}")
                async with Conductor(db) as conductor:
                    async for result in conductor.sync_and_process_cities(expanded):
                        if result.get("phase") == "sync_complete":
                            # Sync phase complete
                            totals["meetings_found"] = result.get("total_meetings_found", 0)
                            logger.info("sync complete", meetings_found=totals["meetings_found"])
                        elif result.get("phase") == "batch_complete":
                            totals["batch_queue_completed"] += result.get(
                                "batch_queue_completed", 0
                            )
                            totals["batch_chunks_collected"] += result.get(
                                "batch_chunks_collected", 0
                            )
                            totals["batch_failed"] += result.get("batch_failed", 0)
                        else:
                            # Per-city processing result
                            city = result.get("city_banana", "unknown")
                            logger.info("city complete",
                                city=city,
                                meetings=result.get("processed", 0),
                                items=result.get("items_processed", 0),
                                new=result.get("items_new", 0),
                            )
                            totals["processed"] += result.get("processed", 0)
                            totals["failed"] += result.get("failed", 0)
                            totals["items_processed"] += result.get("items_processed", 0)
                            totals["items_new"] += result.get("items_new", 0)
                return totals
            finally:
                await db.close()

        results = asyncio.run(run())
        click.echo(
            f"Complete: {results['meetings_found']} found, "
            f"{results['processed']} streaming queue jobs + "
            f"{results['batch_queue_completed']} batch queue jobs, "
            f"{results['batch_chunks_collected']} provider chunks collected, "
            f"{results['items_new']} new items"
        )

    @cli.command("full-sync")
    def full_sync():
        """Run full sync once"""
        async def run():
            db = await Database.create()
            try:
                async with Conductor(db) as conductor:
                    return await conductor.run_sync_cycle(
                        None, command="full-sync-cli"
                    )
            finally:
                await db.close()

        results = asyncio.run(run())
        click.echo(f"Full sync complete: {len(results)} cities processed")

    @cli.command("preview-watchlist")
    def preview_watchlist():
        """Show cities that users are watching (no sync or processing)

        Displays which cities have active alert subscriptions from users.
        """
        async def run():
            db = await Database.create(initialize_corpus=False)
            try:
                demanded = await db.userland.get_demanded_cities()
                if not demanded:
                    return {"message": "No cities in user watchlists", "valid": [], "unknown": []}

                valid_bananas, unknown_cities, resolved = (
                    await _partition_known_jurisdictions(db, demanded)
                )
                valid_cities = [
                    {
                        "banana": banana,
                        "name": resolved[banana].name,
                        "state": resolved[banana].state,
                    }
                    for banana in valid_bananas
                ]

                return {
                    "total": len(valid_bananas) + len(unknown_cities),
                    "valid": valid_cities,
                    "unknown": unknown_cities
                }
            finally:
                await db.close()

        result = asyncio.run(run())
        if "message" in result:
            click.echo(result["message"])
        else:
            click.echo(f"Watchlist: {result['total']} cities")
            if result["valid"]:
                click.echo(f"\nValid ({len(result['valid'])}):")
                for city in result["valid"]:
                    click.echo(f"  {city['banana']} - {city['name']}, {city['state']}")
            if result["unknown"]:
                click.echo(f"\nUnknown (need setup): {', '.join(result['unknown'])}")

    @cli.command("sync-watchlist")
    def sync_watchlist():
        """Sync and process cities that users are watching

        Queries userland for cities with active alert subscriptions.
        These are cities users explicitly requested - they get priority.
        """
        async def run():
            db = await Database.create()
            try:
                # Get demanded cities from userland
                demanded = await db.userland.get_demanded_cities()
                if not demanded:
                    return {"message": "No cities in user watchlists", "cities": []}

                # Filter to cities that exist in our database
                valid_cities, unknown_cities, _ = (
                    await _partition_known_jurisdictions(db, demanded)
                )

                if unknown_cities:
                    click.echo(f"Unknown cities (need manual setup): {', '.join(unknown_cities)}")
                    # Record unknown cities for tracking
                    for banana in unknown_cities:
                        await db.userland.record_city_request(banana)

                if not valid_cities:
                    return {"message": "No valid cities to sync", "cities": []}

                click.echo(f"Syncing {len(valid_cities)} watchlist cities: {', '.join(valid_cities)}")

                # Sync and process, streaming results
                totals = {"processed": 0, "items_new": 0}
                async with Conductor(db) as conductor:
                    async for result in conductor.sync_and_process_cities(valid_cities):
                        if result.get("phase") == "sync_complete":
                            logger.info("sync complete", meetings_found=result.get("total_meetings_found", 0))
                        elif result.get("phase") == "batch_complete":
                            totals["processed"] += result.get("batch_processed", 0)
                        elif result.get("city_banana"):
                            totals["processed"] += result.get("processed", 0)
                            totals["items_new"] += result.get("items_new", 0)
                return {
                    "cities_synced": len(valid_cities),
                    "unknown_cities": unknown_cities,
                    "totals": totals
                }
            finally:
                await db.close()

        results = asyncio.run(run())
        if "message" in results:
            click.echo(results["message"])
        else:
            click.echo(f"Watchlist sync complete: {results['cities_synced']} cities, {results['totals']['items_new']} new items")

    @cli.command("process-watchlist")
    def process_watchlist():
        """Process queued jobs for cities that users are watching

        No sync - just processes existing queue for watchlist cities.
        """
        async def run():
            db = await Database.create()
            try:
                demanded = await db.userland.get_demanded_cities()
                if not demanded:
                    return {"message": "No cities in user watchlists"}

                # Filter to valid cities
                valid_cities, unknown_cities, _ = (
                    await _partition_known_jurisdictions(db, demanded)
                )

                if unknown_cities:
                    # Record unknown cities for tracking
                    for banana in unknown_cities:
                        await db.userland.record_city_request(banana)

                if not valid_cities:
                    return {"message": "No valid cities to process"}

                click.echo(f"Processing {len(valid_cities)} watchlist cities: {', '.join(valid_cities)}")

                totals = {"processed": 0, "items_new": 0}
                async with Conductor(db) as conductor:
                    async for result in conductor.process_cities(valid_cities):
                        if result.get("phase") == "batch_complete":
                            totals["processed"] += result.get("batch_processed", 0)
                        elif result.get("city_banana"):
                            totals["processed"] += result.get("processed", 0)
                            totals["items_new"] += result.get("items_new", 0)
                return {"cities_processed": len(valid_cities), "totals": totals}
            finally:
                await db.close()

        results = asyncio.run(run())
        if "message" in results:
            click.echo(results["message"])
        else:
            totals = cast(Dict[str, int], results["totals"])
            click.echo(f"Watchlist processing complete: {results['cities_processed']} cities, {totals['items_new']} new items")

    @cli.command("city-requests")
    def city_requests():
        """Show pending city requests from users

        Lists cities that users have requested but don't exist in the database.
        Sorted by demand (request count).
        """
        async def run():
            db = await Database.create(initialize_corpus=False)
            try:
                requests = await db.userland.get_pending_city_requests()
                return requests
            finally:
                await db.close()

        requests = asyncio.run(run())
        if not requests:
            click.echo("No pending city requests")
            return

        click.echo(f"\nPending city requests ({len(requests)} total):\n")
        click.echo(f"{'City':<25} {'Requests':<10} {'First Requested':<20} {'Last Requested':<20}")
        click.echo("-" * 75)
        for req in requests:
            first = req['first_requested'].strftime('%Y-%m-%d %H:%M') if req['first_requested'] else '-'
            last = req['last_requested'].strftime('%Y-%m-%d %H:%M') if req['last_requested'] else '-'
            click.echo(f"{req['city_banana']:<25} {req['request_count']:<10} {first:<20} {last:<20}")

    @cli.command("status")
    def status():
        """Show sync status"""
        async def run():
            db = await Database.create(initialize_corpus=False)
            try:
                return await get_sync_status_snapshot(db)
            finally:
                await db.close()

        sync_status = asyncio.run(run())
        click.echo(json.dumps(sync_status, indent=2, default=str))

    @cli.command("fetcher")
    def fetcher():
        """Run as fetcher service (auto sync only, no processing)"""
        async def run():
            db = await Database.create()
            try:
                conductor = Conductor(db)

                def signal_handler(signum, frame):
                    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
                    logger.info("received signal - graceful shutdown", signal=sig_name)
                    conductor.is_running = False
                    conductor.fetcher.is_running = False
                    logger.info("shutdown complete")
                    sys.exit(0)

                signal.signal(signal.SIGTERM, signal_handler)
                signal.signal(signal.SIGINT, signal_handler)

                logger.info("starting fetcher service (sync only, no processing)")
                logger.info("sync interval: 24 hours")

                conductor.is_running = True
                conductor.fetcher.is_running = True

                while conductor.is_running:
                    try:
                        logger.info("starting city sync cycle")
                        results = await conductor.run_sync_cycle(
                            None, command="fetcher-daemon"
                        )

                        succeeded = len([r for r in results if r.status == SyncStatus.COMPLETED])
                        failed = len([r for r in results if r.status == SyncStatus.FAILED])
                        logger.info("sync cycle complete", succeeded=succeeded, failed=failed)

                        logger.info("sleeping for 24 hours until next sync")
                        for _ in range(24 * 60 * 60):
                            if not conductor.is_running:
                                break
                            await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)

                    except Exception as e:  # Intentionally broad: daemon resilience
                        logger.error("sync loop error", error=str(e), error_type=type(e).__name__)
                        logger.info("sleeping for 2 hours after error")
                        for _ in range(2 * 60 * 60):
                            if not conductor.is_running:
                                break
                            await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)
            finally:
                await conductor.close()
                await db.close()

        asyncio.run(run())

    @cli.command("preview-queue")
    @click.argument("banana", type=BANANA, required=False)
    def preview_queue(banana):
        """Preview queued jobs (optionally specify city_banana)"""
        async def run():
            db = await Database.create(initialize_corpus=False)
            try:
                return await get_queue_preview(db, city_banana=banana)
            finally:
                await db.close()

        result = asyncio.run(run())
        click.echo(json.dumps(result, indent=2))

    @cli.command("extract-text")
    @click.argument("meeting_id")
    @click.option("--output-file", "-o", help="Output file for extracted text")
    def extract_text(meeting_id, output_file):
        """Extract text from meeting PDF for manual review"""
        async def run():
            from pipeline.admin import extract_text_preview
            return await extract_text_preview(meeting_id, output_file=output_file)

        result = asyncio.run(run())
        click.echo(json.dumps(result, indent=2))

    @cli.command("preview-items")
    @click.argument("meeting_id")
    @click.option("--extract-text", is_flag=True, help="Extract text from item attachments")
    @click.option("--output-dir", "-o", help="Output directory for item texts")
    def preview_items(meeting_id, extract_text, output_dir):
        """Preview items for a meeting"""
        async def run():
            from pipeline.admin import preview_items as preview_items_func
            return await preview_items_func(meeting_id, extract_text=extract_text, output_dir=output_dir)

        result = asyncio.run(run())
        click.echo(json.dumps(result, indent=2))

    @cli.command("daemon")
    def daemon():
        """Run as combined daemon (sync + processing)

        Pure async architecture using asyncio.create_task() for concurrent loops.
        Shares single event loop and connection pool.
        """
        async def run():

            db = await Database.create()
            try:
                conductor = Conductor(db)

                def signal_handler(signum, frame):
                    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
                    logger.info("received signal - graceful shutdown", signal=sig_name)
                    conductor.is_running = False
                    conductor.fetcher.is_running = False
                    conductor.processor.is_running = False
                    logger.info("shutdown initiated")

                signal.signal(signal.SIGTERM, signal_handler)
                signal.signal(signal.SIGINT, signal_handler)

                logger.info("starting combined daemon (sync + processing)")
                logger.info("sync interval: 24 hours")

                conductor.is_running = True
                conductor.fetcher.is_running = True
                conductor.processor.is_running = True

                # Define sync loop as async task
                async def sync_task():
                    """Sync loop - runs every 24 hours"""
                    while conductor.is_running:
                        try:
                            logger.info("starting city sync cycle")
                            results = await conductor.run_sync_cycle(
                                None, command="sync-daemon"
                            )

                            succeeded = len([r for r in results if r.status == SyncStatus.COMPLETED])
                            failed = len([r for r in results if r.status == SyncStatus.FAILED])
                            logger.info("sync cycle complete", succeeded=succeeded, failed=failed)

                            logger.info("sleeping for 24 hours until next sync")
                            for _ in range(24 * 60 * 60):
                                if not conductor.is_running:
                                    break
                                await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)

                        except Exception as e:  # Intentionally broad: daemon resilience
                            logger.error("sync loop error", error=str(e), error_type=type(e).__name__)
                            logger.info("sleeping for 2 hours after error")
                            for _ in range(2 * 60 * 60):
                                if not conductor.is_running:
                                    break
                                await asyncio.sleep(SHUTDOWN_POLL_INTERVAL)

                # Define processing loop as async task
                async def processing_task():
                    """Processing loop - continuously processes queue"""
                    await conductor.run_processing_daemon()

                # Run both tasks concurrently (single event loop, shared connection pool)
                sync_loop = asyncio.create_task(sync_task())
                processing_loop = asyncio.create_task(processing_task())

                # A terminal task failure must stop the whole service. The
                # shared boundary drains the sibling before preserving the
                # original exception for a nonzero process exit.
                await _await_daemon_tasks(sync_loop, processing_loop)

                logger.info("shutdown complete")

            finally:
                await conductor.close()
                await db.close()

        asyncio.run(run())

    @cli.command("processor")
    def processor():
        """Run as processor service (continuous queue processing, no sync)

        Runs alongside fetcher service. Fetcher syncs cities -> queue,
        processor works through the queue.
        """
        async def run():
            db = await Database.create()
            try:
                conductor = Conductor(db)

                def signal_handler(signum, frame):
                    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
                    logger.info("received signal - graceful shutdown", signal=sig_name)
                    conductor.is_running = False
                    conductor.processor.is_running = False
                    logger.info("shutdown initiated")

                signal.signal(signal.SIGTERM, signal_handler)
                signal.signal(signal.SIGINT, signal_handler)

                if not conductor.processor.analyzer:
                    logger.error("analyzer not available - cannot start processor")
                    raise ProcessingError(PROCESSOR_ANALYZER_ERROR)

                logger.info("starting processor service")
                conductor.is_running = True
                conductor.processor.is_running = True
                await conductor.run_processing_daemon()
                logger.info("shutdown complete")

            finally:
                await conductor.close()
                await db.close()

        asyncio.run(run())

    cli()


if __name__ == "__main__":
    main()
