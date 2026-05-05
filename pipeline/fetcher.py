"""Pipeline Fetcher - City sync and vendor routing"""

import asyncio
import time
import random
from datetime import datetime
from typing import List, Optional, Set
from dataclasses import dataclass
from enum import Enum

import aiohttp

from database.db_postgres import Database
from database.models import Jurisdiction
from exceptions import VendorError
from vendors.adapters.base_adapter_async import FetchResult
from vendors.factory import get_async_adapter, VENDOR_ADAPTERS
from vendors.rate_limiter_async import get_rate_limiter
from config import config, get_logger
from pipeline.protocols import MetricsCollector, NullMetrics
from pipeline.orchestrators import MeetingSyncOrchestrator

logger = get_logger(__name__).bind(component="fetcher")

SYNC_ERROR_DELAY_BASE = 2
SYNC_ERROR_DELAY_JITTER = 1
# Concurrent cities per vendor - balances throughput vs vendor politeness
CITY_SYNC_CONCURRENCY = 3


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
        """Sync all active cities with vendor-aware rate limiting."""
        start_time = time.time()
        logger.info("starting polite city sync")

        self.failed_cities.clear()
        cities = await self.db.jurisdictions.get_all_cities(status="active")
        logger.info("syncing cities with rate limiting", city_count=len(cities))

        by_vendor = {}
        skipped_count = 0

        for city in cities:
            if city.vendor in VENDOR_ADAPTERS:
                by_vendor.setdefault(city.vendor, []).append(city)
            else:
                skipped_count += 1
                logger.debug("skipping city with no adapter", city_name=city.name, vendor=city.vendor)

        total_supported = sum(len(v) for v in by_vendor.values())
        if skipped_count:
            logger.info("cities skipped due to missing adapter", skipped_count=skipped_count)
        logger.info("processing cities", vendor_count=len(by_vendor), city_count=total_supported)

        results = []

        for vendor, vendor_cities in by_vendor.items():
            if not self.is_running:
                break

            sorted_cities = await self._prioritize_cities(vendor_cities)
            logger.info("syncing vendor cities", vendor=vendor, city_count=len(sorted_cities), concurrency=CITY_SYNC_CONCURRENCY)

            # Parallel sync with semaphore for controlled concurrency
            semaphore = asyncio.Semaphore(CITY_SYNC_CONCURRENCY)

            async def sync_city_with_limit(city: Jurisdiction) -> Optional[SyncResult]:
                if not self.is_running:
                    return None

                if not await self._should_sync_city(city):
                    logger.debug("skipping city - not due for sync", city_name=city.name)
                    return SyncResult(city_banana=city.banana, status=SyncStatus.SKIPPED, error_message="Not due for sync")

                async with semaphore:
                    if not self.is_running:
                        return None
                    # Per-request gating in adapter `_request` handles vendor delay
                    result = await self._sync_city_with_retry(city)
                    logger.info("sync completed", city=city.banana, status=result.status.value)

                    if result.status == SyncStatus.FAILED:
                        self.failed_cities.add(city.banana)
                    return result

            vendor_results = await asyncio.gather(*[sync_city_with_limit(c) for c in sorted_cities], return_exceptions=True)

            for result in vendor_results:
                if isinstance(result, BaseException):
                    logger.error("unexpected sync exception", error=str(result))
                elif result is not None:
                    results.append(result)

            if vendor_cities:
                vendor_break = 30 + random.uniform(0, 10)
                logger.info("completed vendor cities - taking break", vendor=vendor, break_seconds=round(vendor_break, 1))
                await asyncio.sleep(vendor_break)

        total_meetings = sum(r.meetings_found for r in results)
        total_processed = sum(r.meetings_processed for r in results)
        duration = time.time() - start_time

        logger.info("polite sync completed", duration_seconds=round(duration, 1), meetings_found=total_meetings, meetings_processed=total_processed, cities_failed=len(self.failed_cities))
        if self.failed_cities:
            logger.warning("cities failed during sync", failed_cities=sorted(self.failed_cities))

        return results

    async def sync_cities(self, city_bananas: List[str]) -> List[SyncResult]:
        """Sync specific cities by city_banana."""
        logger.info("syncing specific cities", city_count=len(city_bananas))
        results = []

        for banana in city_bananas:
            city = await self.db.jurisdictions.get_city(banana=banana)
            if not city:
                logger.warning("city not found", banana=banana)
                results.append(SyncResult(city_banana=banana, status=SyncStatus.FAILED, error_message="City not found in database"))
                continue

            # Per-request gating in adapter `_request` handles vendor delay
            result = await self._sync_city_with_retry(city)
            results.append(result)

            if result.status == SyncStatus.FAILED:
                self.failed_cities.add(banana)

        return results

    async def sync_city(self, city_banana: str) -> SyncResult:
        """Sync a single city by city_banana."""
        city = await self.db.get_city(banana=city_banana)
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

            logger.info("storing meetings", city=city.banana, vendor=vendor, meeting_count=len(all_meetings))
            for i, meeting_dict in enumerate(all_meetings):
                if (i + 1) % 10 == 0:
                    logger.info("storage progress", city=city.banana, progress=i + 1, total=len(all_meetings))

                if not self.is_running:
                    logger.warning("processing stopped - is_running flag is false")
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

    async def _sync_city_with_retry(self, city: Jurisdiction, max_retries: int = 1) -> SyncResult:
        """Sync city with retry (5s, 20s delays)."""
        wait_times = [5, 20]
        last_error = "Unknown retry error"
        last_result: Optional[SyncResult] = None

        for attempt in range(max_retries):
            try:
                result = await self._sync_city(city)
                last_result = result
                if result.status in (SyncStatus.COMPLETED, SyncStatus.SKIPPED):
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

    async def _should_sync_city(self, city: Jurisdiction) -> bool:
        """Determine if city needs syncing based on activity patterns."""
        try:
            recent_meetings = await self.db.jurisdictions.get_city_meeting_frequency(city.banana, days=30)
            last_sync = await self.db.jurisdictions.get_city_last_sync(city.banana)

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

    async def _prioritize_cities(self, cities: List[Jurisdiction]) -> List[Jurisdiction]:
        """Sort cities by sync priority (high activity first)."""
        async def get_priority(city: Jurisdiction) -> float:
            try:
                recent_meetings = await self.db.jurisdictions.get_city_meeting_frequency(city.banana, days=30)
                last_sync = await self.db.jurisdictions.get_city_last_sync(city.banana)
                if not last_sync:
                    return 1000
                hours_since_sync = (datetime.now() - last_sync).total_seconds() / 3600
                return recent_meetings * 10 + min(hours_since_sync / 24, 10)
            except (AttributeError, TypeError) as e:
                logger.warning("failed to calculate priority", city=city.banana, error=str(e))
                return 100

        priorities = [(await get_priority(city), city) for city in cities]
        priorities.sort(key=lambda x: x[0], reverse=True)
        return [city for _, city in priorities]
