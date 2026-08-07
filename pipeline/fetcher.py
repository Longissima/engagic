"""Pipeline Fetcher - City sync and vendor routing"""

import asyncio
import time
import random
from datetime import datetime
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from enum import Enum

import aiohttp

from database.db_postgres import Database
from database.models import Jurisdiction
from database.repositories_async.jurisdictions import JurisdictionSyncStats
from exceptions import VendorError
from vendors.adapters.base_adapter_async import FetchResult
from vendors.adapters.parsers.router import seed_city_hints
from vendors.factory import get_async_adapter, VENDOR_ADAPTERS
from vendors.rate_limiter_async import get_rate_limiter
from config import config, get_logger
from pipeline.protocols import MetricsCollector, NullMetrics
from pipeline.orchestrators import MeetingSyncOrchestrator

logger = get_logger(__name__).bind(component="fetcher")

SYNC_ERROR_DELAY_BASE = 2
SYNC_ERROR_DELAY_JITTER = 1
# Concurrent cities per vendor - balances throughput vs vendor politeness.
# Legistar dominates the long tail (~30 cities, 4 req/matter), so this is mainly
# its dial. 8 stays under the legistar rate limiter's 12 slots (rate_limiter_async.py).
CITY_SYNC_CONCURRENCY = 8


class SyncStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SyncResult:
    city_banana: str
    status: SyncStatus
    meetings_found: int = 0
    meetings_processed: int = 0
    meetings_skipped: int = 0
    items_stored: int = 0
    duration_seconds: float = 0.0
    error_message: Optional[str] = None


# Terminal-status precedence when merging primary + extra vendor passes.
# COMPLETED wins over SKIPPED (extras missing adapter shouldn't mask a good primary);
# FAILED is sticky (any failed pass degrades the aggregate). PENDING/IN_PROGRESS
# shouldn't appear in merged results but rank lowest for safety.
_STATUS_SEVERITY = {
    SyncStatus.FAILED: 3,
    SyncStatus.COMPLETED: 2,
    SyncStatus.SKIPPED: 1,
    SyncStatus.IN_PROGRESS: 0,
    SyncStatus.PENDING: 0,
}


def _merge_sync_results(primary: SyncResult, extra: SyncResult) -> SyncResult:
    """Sum counts across two passes for the same banana; pick worst status."""
    winner = extra if _STATUS_SEVERITY[extra.status] > _STATUS_SEVERITY[primary.status] else primary
    return SyncResult(
        city_banana=primary.city_banana,
        status=winner.status,
        meetings_found=primary.meetings_found + extra.meetings_found,
        meetings_processed=primary.meetings_processed + extra.meetings_processed,
        meetings_skipped=primary.meetings_skipped + extra.meetings_skipped,
        items_stored=primary.items_stored + extra.items_stored,
        duration_seconds=primary.duration_seconds + extra.duration_seconds,
        error_message=winner.error_message,
    )


class Fetcher:
    """City sync and meeting fetching orchestrator"""

    def __init__(self, db: Database, metrics: Optional[MetricsCollector] = None):
        self.db = db
        self.metrics = metrics or NullMetrics()
        # Shared with adapter `_request` -- per-vendor delays are enforced once
        # per request, not once per city, so the same limiter coordinates both.
        self.rate_limiter = get_rate_limiter()
        self.failed_cities: Set[str] = set()
        # Use asyncio.Event for proper async-safe shutdown signaling
        self._shutdown_event = asyncio.Event()
        self._running = True
        self.meeting_sync = MeetingSyncOrchestrator(db)
        self._chunker_hints_seeded = False

    async def _ensure_chunker_hints(self) -> None:
        """Seed the router's sticky per-city rungs from persisted audits.

        Once per Fetcher lifetime on success; a failed read retries on the
        next sync run instead of leaving hints cold until restart. After
        seeding, the registry self-updates in-process as cities chunk, and
        each win re-persists via the queue audit trail.
        """
        if self._chunker_hints_seeded:
            return
        try:
            rows = await self.db.queue.get_chunker_hints()
            count = seed_city_hints(rows)
            self._chunker_hints_seeded = True
            if count:
                logger.info("seeded chunker routing hints", count=count)
        except Exception as e:
            logger.warning("chunker hint seeding failed, will retry next sync", error=str(e))

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

    async def sync_all(self) -> List[SyncResult]:
        """Sync all active jurisdictions due for refresh.

        Loads adaptive scheduling inputs once, filters jurisdictions that are
        not due, then enters the same canonical vendor-parallel path used by
        explicit targets.
        """
        start_time = time.time()
        self.failed_cities.clear()
        await self._ensure_chunker_hints()

        cities = await self.db.jurisdictions.get_all_cities(status="active")
        logger.info("starting full sync", candidate_count=len(cities))

        sync_stats = await self.db.jurisdictions.get_city_sync_stats(
            [city.banana for city in cities]
        )

        due_cities: List[Jurisdiction] = []
        skipped_not_due = 0
        for city in cities:
            if not await self._should_sync_city(
                city, sync_stats.get(city.banana, JurisdictionSyncStats())
            ):
                skipped_not_due += 1
                continue
            due_cities.append(city)

        if skipped_not_due:
            logger.info("cities skipped - not due for sync", skipped_count=skipped_not_due)

        return await self._sync_resolved_cities(
            due_cities,
            sync_stats=sync_stats,
            start_time=start_time,
        )

    async def sync_cities(self, city_bananas: List[str]) -> List[SyncResult]:
        """Canonical sync path. Sync the given jurisdictions.

        Vendor-grouped parallel: each vendor's cities run with
        CITY_SYNC_CONCURRENCY parallelism; vendor batches themselves run in
        parallel since the per-vendor rate limiter already serializes traffic
        to each vendor's infra. Within a vendor, high-activity / stale cities
        sync first so partial runs prioritize useful work.

        Result order is NOT preserved.
        """
        start_time = time.time()
        self.failed_cities.clear()
        await self._ensure_chunker_hints()

        city_map = await self.db.jurisdictions.get_cities_by_bananas(
            list(dict.fromkeys(city_bananas))
        )
        cities: List[Jurisdiction] = []
        results: List[SyncResult] = []
        for banana in city_bananas:
            city = city_map.get(banana)
            if not city:
                logger.warning("city not found", banana=banana)
                results.append(SyncResult(city_banana=banana, status=SyncStatus.FAILED, error_message="City not found in database"))
                continue
            cities.append(city)

        return await self._sync_resolved_cities(
            cities,
            initial_results=results,
            start_time=start_time,
        )

    async def _sync_resolved_cities(
        self,
        cities: List[Jurisdiction],
        *,
        initial_results: Optional[List[SyncResult]] = None,
        sync_stats: Optional[Dict[str, JurisdictionSyncStats]] = None,
        start_time: Optional[float] = None,
    ) -> List[SyncResult]:
        """Run the canonical vendor-parallel path for resolved jurisdictions.

        Both full-sync and explicitly targeted CLI/daemon calls converge here,
        so target resolution never forces a second round of point lookups.
        """
        start_time = start_time if start_time is not None else time.time()
        by_vendor: Dict[str, List[Jurisdiction]] = {}
        results = list(initial_results or [])
        for city in cities:
            if city.vendor not in VENDOR_ADAPTERS:
                logger.debug(
                    "skipping city - no adapter",
                    banana=city.banana,
                    vendor=city.vendor,
                )
                results.append(SyncResult(city_banana=city.banana, status=SyncStatus.SKIPPED, error_message=f"No adapter for vendor {city.vendor}"))
                continue
            by_vendor.setdefault(city.vendor, []).append(city)

        if sync_stats is None:
            sync_stats = await self.db.jurisdictions.get_city_sync_stats(
                list(dict.fromkeys(city.banana for city in cities))
            )

        total_supported = sum(len(v) for v in by_vendor.values())
        logger.info(
            "vendor-parallel sync",
            vendor_count=len(by_vendor),
            cities=total_supported,
            concurrency_per_vendor=CITY_SYNC_CONCURRENCY,
        )

        async def sync_vendor_batch(vendor: str, vendor_cities: List[Jurisdiction]) -> List[SyncResult]:
            sorted_cities = await self._prioritize_cities(
                vendor_cities, sync_stats
            )
            sem = asyncio.Semaphore(CITY_SYNC_CONCURRENCY)

            async def sync_one(city: Jurisdiction) -> Optional[SyncResult]:
                if not self.is_running:
                    return None
                async with sem:
                    if not self.is_running:
                        return None
                    city_start = time.time()
                    try:
                        result = await self._sync_city_with_retry(city)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # A database/integration bug used to be returned by
                        # gather as a bare exception, logged, and omitted from
                        # results entirely. Preserve one terminal result per
                        # attempted city so operators can reconcile a run.
                        result = SyncResult(
                            city_banana=city.banana,
                            status=SyncStatus.FAILED,
                            duration_seconds=time.time() - city_start,
                            error_message=(
                                f"Unexpected {type(e).__name__}: {e}"
                            ),
                        )
                        self.metrics.vendor_requests.labels(
                            vendor=vendor, status="unexpected_error"
                        ).inc()
                        self.metrics.record_error("fetcher", e)
                        logger.exception(
                            "unexpected sync exception",
                            vendor=vendor,
                            city=city.banana,
                            error=str(e),
                        )
                    logger.info("sync completed", city=city.banana, status=result.status.value)
                    if result.status == SyncStatus.FAILED:
                        self.failed_cities.add(city.banana)
                    return result

            raw = await asyncio.gather(*[sync_one(c) for c in sorted_cities])
            return [result for result in raw if result is not None]

        vendor_batches = await asyncio.gather(
            *[sync_vendor_batch(v, cs) for v, cs in by_vendor.items()],
        )
        for batch in vendor_batches:
            results.extend(batch)

        total_meetings = sum(r.meetings_found for r in results)
        total_processed = sum(r.meetings_processed for r in results)
        duration = time.time() - start_time
        logger.info(
            "sync complete",
            duration_seconds=round(duration, 1),
            meetings_found=total_meetings,
            meetings_processed=total_processed,
            cities_failed=len(self.failed_cities),
        )
        if self.failed_cities:
            logger.warning("cities failed during sync", failed_cities=sorted(self.failed_cities))

        return results

    async def sync_city(self, city_banana: str) -> SyncResult:
        """Sync a single city by city_banana."""
        city = await self.db.jurisdictions.get_city(banana=city_banana)
        if not city:
            return SyncResult(city_banana=city_banana, status=SyncStatus.FAILED, error_message="City not found")
        return await self._sync_city_with_retry(city)

    async def _sync_city(self, city: Jurisdiction) -> SyncResult:
        """Sync a city across its primary vendor and any extra_vendors.

        Primary runs first; extras run sequentially afterward (rate-limited per vendor).
        All streams key off the same banana, so meetings/matters/items merge naturally.
        The aggregate SyncResult sums counts; status degrades to FAILED if any pass failed
        and primary succeeded (full failure keeps FAILED from primary)."""
        if not city.vendor:
            return SyncResult(city_banana=city.banana, status=SyncStatus.SKIPPED,
                              error_message="No vendor configured")

        aggregate = await self._sync_with_vendor(city, city.vendor, city.slug)

        extras = city.extra_vendors or []
        if not extras:
            return aggregate

        for extra in extras:
            if not self.is_running:
                break
            vendor, slug = extra.get("vendor"), extra.get("slug")
            if not vendor or not slug:
                logger.warning("malformed extra_vendor", city=city.banana, extra=extra)
                continue

            # Per-request gating in adapter `_request` handles vendor delay
            extra_result = await self._sync_with_vendor(city, vendor, slug)
            aggregate = _merge_sync_results(aggregate, extra_result)

        return aggregate

    async def _sync_with_vendor(self, city: Jurisdiction, vendor: str, slug: str) -> SyncResult:
        """Single-vendor sync pass for a city. One adapter, one fetch, store all meetings."""
        result = SyncResult(city_banana=city.banana, status=SyncStatus.PENDING)
        start_time = time.time()

        kwargs = {}
        if vendor == "legistar" and slug == "nyc":
            kwargs["api_token"] = config.NYC_LEGISTAR_TOKEN

        try:
            adapter = get_async_adapter(vendor, slug, **kwargs)
            # Corpus provenance: adapters only know (vendor, slug); stamp the
            # jurisdiction so archived bytes record which government they're from.
            adapter.banana = city.banana
        except (VendorError, ValueError) as e:
            result.status = SyncStatus.SKIPPED
            result.error_message = str(e)
            logger.warning("adapter init failed", city=city.banana, vendor=vendor, slug=slug, error=str(e))
            self.metrics.record_error("vendor", e)
            return result

        try:
            logger.info("starting sync", city=city.banana, vendor=vendor, slug=slug)
            result.status = SyncStatus.IN_PROGRESS

            try:
                fetch_result: FetchResult = await adapter.fetch_meetings()
            except (VendorError, ValueError, KeyError) as e:
                logger.error("error fetching meetings", city=city.banana, vendor=vendor, error=str(e))
                result.status = SyncStatus.FAILED
                result.error_message = str(e)
                self.metrics.vendor_requests.labels(vendor=vendor, status='error').inc()
                self.metrics.record_error('vendor', e)
                return result

            if not fetch_result.success:
                logger.error(
                    "adapter fetch failed",
                    city=city.banana,
                    vendor=vendor,
                    error=fetch_result.error,
                    error_type=fetch_result.error_type
                )
                result.status = SyncStatus.FAILED
                result.error_message = f"Adapter failed: {fetch_result.error}"
                self.metrics.vendor_requests.labels(vendor=vendor, status='adapter_error').inc()
                return result

            all_meetings = fetch_result.meetings
            total_items = sum(len(m.get("items", [])) for m in all_meetings)
            total_matters = sum(1 for m in all_meetings for item in m.get("items", []) if item.get("matter_file") or item.get("matter_id"))

            result.meetings_found = len(all_meetings)
            logger.info("found meetings for city", city=city.banana, vendor=vendor, meeting_count=len(all_meetings), total_items=total_items, matters_with_tracking=total_matters)

            processed_count = 0
            items_stored_count = 0
            matters_tracked_count = 0
            matters_duplicate_count = 0
            skipped_meetings = 0
            interrupted = False

            logger.info("storing meetings", city=city.banana, vendor=vendor, meeting_count=len(all_meetings))
            for i, meeting_dict in enumerate(all_meetings):
                if (i + 1) % 10 == 0:
                    logger.info("storage progress", city=city.banana, progress=i + 1, total=len(all_meetings))

                if not self.is_running:
                    logger.warning("processing stopped - is_running flag is false")
                    interrupted = True
                    break

                stored_meeting, storage_stats = await self.meeting_sync.sync_meeting(meeting_dict, city)
                if not stored_meeting:
                    if storage_stats.get('meetings_skipped', 0):
                        skipped_meetings += 1
                        logger.warning("skipped meeting", meeting_title=storage_stats.get('skipped_title') or meeting_dict.get("title", "Unknown"), reason=storage_stats.get('skip_reason') or 'unknown')
                    continue

                processed_count += 1
                items_stored_count += storage_stats.get('items_stored', 0)
                matters_tracked_count += storage_stats.get('matters_tracked', 0)
                matters_duplicate_count += storage_stats.get('matters_duplicate', 0)

            if interrupted:
                result.meetings_processed = processed_count
                result.meetings_skipped = skipped_meetings
                result.items_stored = items_stored_count
                result.status = SyncStatus.SKIPPED
                result.error_message = "Sync interrupted before all meetings were stored"
                result.duration_seconds = time.time() - start_time
                self.metrics.vendor_requests.labels(
                    vendor=vendor, status="interrupted"
                ).inc()
                return result

            result.meetings_processed = processed_count
            result.meetings_skipped = skipped_meetings
            result.items_stored = items_stored_count
            result.status = SyncStatus.COMPLETED
            result.duration_seconds = time.time() - start_time

            self.metrics.vendor_requests.labels(vendor=vendor, status='success').inc()
            self.metrics.meetings_synced.labels(city=city.banana, vendor=vendor).inc(processed_count)
            self.metrics.items_extracted.labels(city=city.banana, vendor=vendor).inc(items_stored_count)
            self.metrics.matters_tracked.labels(city=city.banana).inc(matters_tracked_count)

            logger.info("sync complete", city=city.banana, vendor=vendor, meetings=processed_count, skipped_meetings=skipped_meetings, items=items_stored_count, new_matters=matters_tracked_count, duplicate_matters=matters_duplicate_count, duration_seconds=round(result.duration_seconds, 1))

        except (VendorError, asyncio.TimeoutError, aiohttp.ClientError) as e:
            result.status = SyncStatus.FAILED
            result.error_message = str(e)
            result.duration_seconds = time.time() - start_time
            self.metrics.vendor_requests.labels(vendor=vendor, status='error').inc()
            self.metrics.record_error(component="fetcher", error=e)
            logger.error("sync failed", city=city.banana, vendor=vendor, duration_seconds=round(result.duration_seconds, 1), error=str(e))
            await asyncio.sleep(SYNC_ERROR_DELAY_BASE + random.uniform(0, SYNC_ERROR_DELAY_JITTER))

        return result

    async def _sync_city_with_retry(self, city: Jurisdiction, max_retries: int = 3) -> SyncResult:
        """Sync city with retry on transient failures.

        max_retries=3 yields up to 2 backoff attempts after the initial try
        (waits 5s then 20s before each retry). Bumped from 1 on 2026-05-20 after
        single TCP resets and rate-limit blips were wiping whole-city syncs
        (Miami/iqm2, Brighton/legistar).
        """
        wait_times = [5, 20]
        last_error = "Unknown retry error"
        last_result: Optional[SyncResult] = None

        for attempt in range(max_retries):
            try:
                result = await self._sync_city(city)
                last_result = result
                if result.status == SyncStatus.COMPLETED:
                    try:
                        checkpointed = await self.db.jurisdictions.mark_city_synced(
                            city.banana
                        )
                        if not checkpointed:
                            raise RuntimeError(
                                "jurisdiction disappeared before sync checkpoint"
                            )
                    except Exception as e:
                        # Meeting writes are already durable, but without this
                        # checkpoint the scheduler must regard the city as due.
                        result.status = SyncStatus.FAILED
                        result.error_message = (
                            "Sync completed but lifecycle checkpoint failed: "
                            f"{type(e).__name__}: {e}"
                        )
                        self.metrics.vendor_requests.labels(
                            vendor=city.vendor, status="checkpoint_error"
                        ).inc()
                        self.metrics.record_error("fetcher", e)
                        logger.exception(
                            "sync checkpoint failed",
                            city=city.banana,
                            error=str(e),
                        )
                    return result
                if result.status == SyncStatus.SKIPPED:
                    return result
                last_error = result.error_message or "Sync failed"
            except (VendorError, asyncio.TimeoutError, aiohttp.ClientError) as e:
                last_error = str(e)

            if attempt >= max_retries - 1:
                logger.error("final sync failure after retries", city=city.name, attempts=max_retries, error=last_error)
                if last_result:
                    last_result.status = SyncStatus.FAILED
                    last_result.error_message = last_error
                    return last_result
                return SyncResult(city_banana=city.banana, status=SyncStatus.FAILED, error_message=last_error)

            wait_time = wait_times[attempt] + random.uniform(0, 2)
            logger.warning("sync failed - retrying", city=city.name, attempt=attempt + 1, max_retries=max_retries, wait_seconds=round(wait_time, 1), error=last_error)
            await asyncio.sleep(wait_time)

        if last_result:
            last_result.status = SyncStatus.FAILED
            last_result.error_message = last_error
            return last_result
        return SyncResult(city_banana=city.banana, status=SyncStatus.FAILED, error_message=last_error)

    async def _should_sync_city(
        self,
        city: Jurisdiction,
        stats: Optional[JurisdictionSyncStats] = None,
    ) -> bool:
        """Determine if city needs syncing based on activity patterns."""
        try:
            if stats is None:
                stats = (
                    await self.db.jurisdictions.get_city_sync_stats([city.banana])
                ).get(city.banana, JurisdictionSyncStats())
            recent_meetings = stats.recent_meetings
            last_sync = stats.last_synced_at

            if not last_sync:
                return True

            hours_since_sync = (datetime.now() - last_sync).total_seconds() / 3600

            # Adaptive scheduling: high activity = 12h, medium = 24h, low = weekly
            if recent_meetings >= 8:
                return hours_since_sync >= 12
            elif recent_meetings >= 4:
                return hours_since_sync >= 24
            else:
                return hours_since_sync >= 168

        except (AttributeError, TypeError) as e:
            logger.warning("error checking sync schedule", city=city.banana, error=str(e))
            return True

    async def _prioritize_cities(
        self,
        cities: List[Jurisdiction],
        sync_stats: Optional[Dict[str, JurisdictionSyncStats]] = None,
    ) -> List[Jurisdiction]:
        """Sort cities by sync priority (high activity first)."""
        if sync_stats is None:
            sync_stats = await self.db.jurisdictions.get_city_sync_stats(
                [city.banana for city in cities]
            )
        now = datetime.now()

        def get_priority(city: Jurisdiction) -> float:
            try:
                stats = sync_stats.get(city.banana, JurisdictionSyncStats())
                recent_meetings = stats.recent_meetings
                last_sync = stats.last_synced_at
                if not last_sync:
                    return 1000
                hours_since_sync = (now - last_sync).total_seconds() / 3600
                return recent_meetings * 10 + min(hours_since_sync / 24, 10)
            except (AttributeError, TypeError) as e:
                logger.warning("failed to calculate priority", city=city.banana, error=str(e))
                return 100

        priorities = [(get_priority(city), city) for city in cities]
        priorities.sort(key=lambda x: x[0], reverse=True)
        return [city for _, city in priorities]
