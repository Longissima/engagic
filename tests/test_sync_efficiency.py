"""Focused contracts for set-based sync planning and adapter validation."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from database.models import Jurisdiction
from database.repositories_async.jurisdictions import (
    JurisdictionRepository,
    JurisdictionSyncStats,
)
from pipeline.conductor import (
    _expand_jurisdictions,
    _partition_known_jurisdictions,
)
from pipeline.fetcher import Fetcher, SyncResult, SyncStatus
from vendors.adapters.base_adapter_async import AsyncBaseAdapter


def _city(banana: str, *, vendor: str = "primegov") -> Jurisdiction:
    return Jurisdiction(
        banana=banana,
        name=banana,
        state="CA",
        vendor=vendor,
        slug=f"{banana}-slug",
    )


class _QueueRepository:
    async def get_chunker_hints(self):
        return []


class _JurisdictionRepository:
    def __init__(self, cities):
        self.cities = {city.banana: city for city in cities}
        self.resolve_calls = []
        self.stats_calls = []
        self.mark_calls = []
        self.mark_result = True
        self.all_calls = 0

    async def get_cities_by_bananas(self, bananas):
        self.resolve_calls.append(list(bananas))
        return {
            banana: self.cities[banana]
            for banana in bananas
            if banana in self.cities
        }

    async def get_city_sync_stats(self, bananas, *, days=30):
        self.stats_calls.append((list(bananas), days))
        return {
            banana: JurisdictionSyncStats()
            for banana in bananas
            if banana in self.cities
        }

    async def get_all_cities(self, status="active"):
        self.all_calls += 1
        return list(self.cities.values())

    async def mark_city_synced(self, banana):
        self.mark_calls.append(banana)
        return self.mark_result

    async def get_city(self, banana):
        raise AssertionError("targeted sync must use the set-based resolver")


class _Database:
    def __init__(self, cities):
        self.jurisdictions = _JurisdictionRepository(cities)
        self.queue = _QueueRepository()


def test_target_resolution_batches_and_accounts_for_missing_and_unsupported():
    supported = _city("supportedCA")
    unsupported = _city("unsupportedCA", vendor="mystery")
    db = _Database([supported, unsupported])
    fetcher = Fetcher(cast(Any, db))
    attempted = []

    async def sync_city(city: Jurisdiction, max_retries: int = 3):
        del max_retries
        attempted.append(city.banana)
        return SyncResult(city_banana=city.banana, status=SyncStatus.COMPLETED)

    fetcher._sync_city_with_retry = sync_city
    results = asyncio.run(
        fetcher.sync_cities(["supportedCA", "missingCA", "unsupportedCA"])
    )

    by_banana = {result.city_banana: result for result in results}
    assert db.jurisdictions.resolve_calls == [
        ["supportedCA", "missingCA", "unsupportedCA"]
    ]
    assert db.jurisdictions.stats_calls == [
        (["supportedCA", "unsupportedCA"], 30)
    ]
    assert attempted == ["supportedCA"]
    assert by_banana["supportedCA"].status is SyncStatus.COMPLETED
    assert by_banana["missingCA"].status is SyncStatus.FAILED
    assert by_banana["missingCA"].error_message == "City not found in database"
    assert by_banana["unsupportedCA"].status is SyncStatus.SKIPPED
    assert by_banana["unsupportedCA"].error_message == "No adapter for vendor mystery"


def test_full_sync_batches_schedule_and_reuses_resolved_cities():
    due = _city("dueCA")
    not_due = _city("notdueCA")
    db = _Database([due, not_due])
    now = datetime.now()

    async def stats(bananas, *, days=30):
        db.jurisdictions.stats_calls.append((list(bananas), days))
        return {
            "dueCA": JurisdictionSyncStats(
                recent_meetings=9,
                last_synced_at=now - timedelta(hours=13),
            ),
            "notdueCA": JurisdictionSyncStats(
                recent_meetings=9,
                last_synced_at=now - timedelta(hours=2),
            ),
        }

    db.jurisdictions.get_city_sync_stats = stats
    attempted = []
    fetcher = Fetcher(cast(Any, db))

    async def sync_city(city: Jurisdiction, max_retries: int = 3):
        del max_retries
        attempted.append(city.banana)
        return SyncResult(city_banana=city.banana, status=SyncStatus.COMPLETED)

    fetcher._sync_city_with_retry = sync_city
    results = asyncio.run(fetcher.sync_all())

    assert db.jurisdictions.all_calls == 1
    assert db.jurisdictions.resolve_calls == []
    assert db.jurisdictions.stats_calls == [(["dueCA", "notdueCA"], 30)]
    assert attempted == ["dueCA"]
    assert [result.city_banana for result in results] == ["dueCA"]


def test_priority_order_uses_one_stats_snapshot_and_is_stable():
    unsynced = _city("unsyncedCA")
    active = _city("activeCA")
    quiet = _city("quietCA")
    db = _Database([unsynced, active, quiet])
    fetcher = Fetcher(cast(Any, db))
    now = datetime.now()
    stats = {
        "unsyncedCA": JurisdictionSyncStats(),
        "activeCA": JurisdictionSyncStats(
            recent_meetings=8, last_synced_at=now - timedelta(days=2)
        ),
        "quietCA": JurisdictionSyncStats(
            recent_meetings=1, last_synced_at=now - timedelta(days=10)
        ),
    }

    ordered = asyncio.run(
        fetcher._prioritize_cities([quiet, active, unsynced], stats)
    )

    assert [city.banana for city in ordered] == [
        "unsyncedCA",
        "activeCA",
        "quietCA",
    ]
    assert db.jurisdictions.stats_calls == []


def test_unexpected_city_exception_becomes_failed_result():
    good = _city("goodCA")
    broken = _city("brokenCA")
    fetcher = Fetcher(cast(Any, _Database([good, broken])))

    async def sync_city(city: Jurisdiction, max_retries: int = 3):
        del max_retries
        if city.banana == "brokenCA":
            raise RuntimeError("storage exploded")
        return SyncResult(city_banana=city.banana, status=SyncStatus.COMPLETED)

    fetcher._sync_city_with_retry = sync_city
    results = asyncio.run(fetcher.sync_cities(["goodCA", "brokenCA"]))
    by_banana = {result.city_banana: result for result in results}

    assert by_banana["goodCA"].status is SyncStatus.COMPLETED
    assert by_banana["brokenCA"].status is SyncStatus.FAILED
    assert by_banana["brokenCA"].error_message == (
        "Unexpected RuntimeError: storage exploded"
    )
    assert fetcher.failed_cities == {"brokenCA"}


def test_sync_lifecycle_checkpoint_is_written_only_for_completed_status():
    city = _city("checkpointCA")

    async def run(status):
        db = _Database([city])
        fetcher = Fetcher(cast(Any, db))

        async def sync_city(city: Jurisdiction, *, max_retries: int = 3):
            del max_retries
            return SyncResult(city_banana=city.banana, status=status)

        fetcher._sync_city = sync_city
        result = await fetcher._sync_city_with_retry(city, max_retries=1)
        return result, db.jurisdictions.mark_calls

    completed, completed_marks = asyncio.run(run(SyncStatus.COMPLETED))
    skipped, skipped_marks = asyncio.run(run(SyncStatus.SKIPPED))
    failed, failed_marks = asyncio.run(run(SyncStatus.FAILED))

    assert completed.status is SyncStatus.COMPLETED
    assert completed_marks == ["checkpointCA"]
    assert skipped.status is SyncStatus.SKIPPED
    assert skipped_marks == []
    assert failed.status is SyncStatus.FAILED
    assert failed_marks == []


def test_sync_checkpoint_failure_fails_closed_without_refetching():
    city = _city("checkpointCA")
    db = _Database([city])
    db.jurisdictions.mark_result = False
    fetcher = Fetcher(cast(Any, db))
    sync_calls = 0

    async def sync_city(city: Jurisdiction, *, max_retries: int = 3):
        nonlocal sync_calls
        del max_retries
        sync_calls += 1
        return SyncResult(city_banana=city.banana, status=SyncStatus.COMPLETED)

    fetcher._sync_city = sync_city
    result = asyncio.run(fetcher._sync_city_with_retry(city))

    assert result.status is SyncStatus.FAILED
    assert result.error_message is not None
    assert "checkpoint failed" in result.error_message
    assert sync_calls == 1
    assert db.jurisdictions.mark_calls == ["checkpointCA"]


def test_extra_vendor_retry_does_not_replay_successful_primary(monkeypatch):
    city = _city("multiCA")
    city.extra_vendors = [{"vendor": "legistar", "slug": "extra"}]
    fetcher = Fetcher(cast(Any, _Database([city])))
    calls = []

    async def sync_vendor(city, vendor, slug):
        del city
        calls.append((vendor, slug))
        if vendor == "legistar" and calls.count((vendor, slug)) == 1:
            return SyncResult(
                city_banana="multiCA",
                status=SyncStatus.FAILED,
                error_message="transient extra failure",
            )
        return SyncResult(city_banana="multiCA", status=SyncStatus.COMPLETED)

    async def no_sleep(_seconds):
        return None

    fetcher._sync_with_vendor = sync_vendor
    monkeypatch.setattr("pipeline.fetcher.asyncio.sleep", no_sleep)

    result = asyncio.run(fetcher._sync_city(city, max_retries=2))

    assert result.status is SyncStatus.COMPLETED
    assert calls == [
        ("primegov", "multiCA-slug"),
        ("legistar", "extra"),
        ("legistar", "extra"),
    ]


def test_shutdown_between_vendor_streams_does_not_checkpoint_city():
    city = _city("multiCA")
    city.extra_vendors = [{"vendor": "legistar", "slug": "extra"}]
    db = _Database([city])
    fetcher = Fetcher(cast(Any, db))
    calls = []

    async def sync_vendor(city, vendor, slug, *, max_retries=3):
        del city, max_retries
        calls.append((vendor, slug))
        fetcher.is_running = False
        return SyncResult(
            city_banana="multiCA", status=SyncStatus.COMPLETED
        )

    fetcher._sync_vendor_with_retry = sync_vendor
    result = asyncio.run(fetcher._sync_city_with_retry(city))

    assert result.status is SyncStatus.CANCELLED
    assert result.error_message == (
        "Sync interrupted before all vendor streams completed"
    )
    assert calls == [("primegov", "multiCA-slug")]
    assert db.jurisdictions.mark_calls == []


class _SchemaAdapter(AsyncBaseAdapter):
    def __init__(self):
        super().__init__("test-city", "test-vendor")

    async def _fetch_meetings_impl(self, days_back, days_forward):
        return [
            {
                "vendor_id": 123,
                "title": "  Council Meeting  ",
                "start": "2026-08-07T18:00:00",
                "agenda_sources": [{"type": "agenda", "url": "https://x"}],
                "items": [
                    {
                        "vendor_item_id": 456,
                        "title": "  Public hearing  ",
                        "sequence": "2",
                        "body_text": "adapter-specific extra",
                        "attachments": [
                            {
                                "name": "Staff report",
                                "url": "  https://example.test/report  ",
                                "history_id": "durable-id",
                            }
                        ],
                    }
                ],
            },
            {
                "vendor_id": "optional-fields",
                "title": "Meeting without documents",
                "start": "2026-08-08T18:00:00Z",
                "items": None,
                "packet_url": None,
            },
            {
                "vendor_id": "invalid-date",
                "title": "Broken meeting",
                "start": "not-a-date",
            },
            {
                "vendor_id": "invalid-attachment",
                "title": "Broken attachment",
                "start": "2026-08-09T18:00:00",
                "items": [
                    {
                        "title": "Item",
                        "sequence": 1,
                        "attachments": [
                            {"name": "Missing URL", "url": "", "type": "pdf"}
                        ],
                    }
                ],
            },
        ]


def test_adapter_boundary_normalizes_valid_output_and_filters_schema_failures():
    result = asyncio.run(_SchemaAdapter().fetch_meetings())

    assert result.success is True
    assert [meeting["vendor_id"] for meeting in result.meetings] == [
        "123",
        "optional-fields",
    ]
    meeting = result.meetings[0]
    assert meeting["title"] == "Council Meeting"
    assert meeting["agenda_sources"] == [
        {"type": "agenda", "url": "https://x"}
    ]
    item = meeting["items"][0]
    assert item["title"] == "Public hearing"
    assert item["vendor_item_id"] == "456"
    assert item["sequence"] == 2
    assert item["body_text"] == "adapter-specific extra"
    assert item["attachments"] == [
        {
            "name": "Staff report",
            "url": "https://example.test/report",
            "type": "unknown",
            "history_id": "durable-id",
        }
    ]
    assert "items" not in result.meetings[1]
    assert "packet_url" not in result.meetings[1]


class _Connection:
    def __init__(self):
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "requested.county_banana" in query:
            return [
                {"county_banana": "countyCA", "banana": "countyCA"},
                {"county_banana": "countyCA", "banana": "childCA"},
            ]
        if "FROM jurisdictions" in query:
            return [
                {
                    "banana": "oneCA",
                    "name": "One",
                    "state": "CA",
                    "vendor": "primegov",
                    "slug": "one",
                    "extra_vendors": None,
                    "type": "city",
                    "county_banana": None,
                    "status": "active",
                    "participation": None,
                }
            ]
        return [
            {
                "banana": "oneCA",
                "recent_meetings": 7,
                "last_synced_at": datetime(2026, 8, 1),
            },
            {
                "banana": "missingCA",
                "recent_meetings": 0,
                "last_synced_at": None,
            },
        ]

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "UPDATE 1"


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self):
        self.connection = _Connection()

    def acquire(self):
        return _Acquire(self.connection)


def test_jurisdiction_repository_methods_deduplicate_into_single_queries():
    pool = _Pool()
    repository = JurisdictionRepository(cast(Any, pool))

    cities = asyncio.run(
        repository.get_cities_by_bananas(["oneCA", "oneCA", "missingCA"])
    )
    stats = asyncio.run(
        repository.get_city_sync_stats(["oneCA", "oneCA", "missingCA"])
    )
    states = asyncio.run(repository.get_cities_by_states(["CA", "CA"]))
    counties = asyncio.run(
        repository.get_county_jurisdictions_batch(["countyCA", "countyCA"])
    )
    marked = asyncio.run(repository.mark_city_synced("oneCA"))

    assert list(cities) == ["oneCA"]
    assert stats["oneCA"].recent_meetings == 7
    assert stats["oneCA"].last_synced_at == datetime(2026, 8, 1)
    assert stats["missingCA"] == JurisdictionSyncStats()
    assert [city.banana for city in states["CA"]] == ["oneCA"]
    assert counties == {"countyCA": ["countyCA", "childCA"]}
    assert marked is True
    assert len(pool.connection.calls) == 5
    assert pool.connection.calls[0][1] == (["oneCA", "missingCA"],)
    assert pool.connection.calls[1][1] == (["oneCA", "missingCA"], 30)
    assert pool.connection.calls[2][1] == (["CA"], "active")
    assert pool.connection.calls[3][1] == (["countyCA"],)
    assert pool.connection.calls[4][1] == ("oneCA",)
    stats_sql = pool.connection.calls[1][0]
    assert "jurisdiction.last_synced_at" in stats_sql
    assert "MAX(m.date)" not in stats_sql


def test_jurisdiction_lifecycle_migration_is_additive_and_reversible():
    migration_dir = Path(__file__).parents[1] / "database" / "migrations"
    up = (
        migration_dir / "031_jurisdiction_sync_lifecycle.sql"
    ).read_text()
    down = (
        migration_dir / "031_jurisdiction_sync_lifecycle.down.sql"
    ).read_text()

    assert "ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP" in up
    assert "DROP COLUMN IF EXISTS last_synced_at" in down
    assert "MAX(" not in up


class _ExpansionRepository:
    def __init__(self):
        self.calls = []
        self.cities = {
            "countyCA": _city("countyCA"),
            "literalCA": _city("literalCA"),
            "sharedCA": _city("sharedCA"),
        }
        self.cities["countyCA"].type = "county"

    async def get_cities_by_bananas(self, bananas):
        self.calls.append(("bananas", list(bananas)))
        return {
            banana: self.cities[banana]
            for banana in bananas
            if banana in self.cities
        }

    async def get_county_jurisdictions_batch(self, bananas):
        self.calls.append(("counties", list(bananas)))
        return {
            "countyCA": ["countyCA", "childCA", "sharedCA"]
        }

    async def get_cities_by_states(self, states):
        self.calls.append(("states", list(states)))
        return {
            "CA": [self.cities["sharedCA"], _city("stateonlyCA")],
            "TX": [_city("stateonlyTX")],
        }


class _ExpansionDatabase:
    def __init__(self):
        self.jurisdictions = _ExpansionRepository()


def test_conductor_expansion_batches_and_preserves_order_and_semantics():
    db = _ExpansionDatabase()
    expanded = asyncio.run(
        _expand_jurisdictions(
            cast(Any, db),
            [
                "countyCA",
                "literalCA",
                "CA",
                "missingCA",
                "countyCA",
                "TX",
            ],
        )
    )

    assert expanded == [
        "countyCA",
        "childCA",
        "sharedCA",
        "literalCA",
        "stateonlyCA",
        "missingCA",
        "stateonlyTX",
    ]
    assert db.jurisdictions.calls == [
        ("bananas", ["countyCA", "literalCA", "missingCA"]),
        ("states", ["CA", "TX"]),
        ("counties", ["countyCA"]),
    ]


def test_watchlist_partition_batches_and_deduplicates_in_input_order():
    db = _ExpansionDatabase()
    valid, unknown, resolved = asyncio.run(
        _partition_known_jurisdictions(
            cast(Any, db),
            ["literalCA", "missingCA", "literalCA", "sharedCA"],
        )
    )

    assert valid == ["literalCA", "sharedCA"]
    assert unknown == ["missingCA"]
    assert list(resolved) == ["literalCA", "sharedCA"]
    assert db.jurisdictions.calls == [
        ("bananas", ["literalCA", "missingCA", "sharedCA"])
    ]
