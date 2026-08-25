import asyncio
from collections import Counter

import pytest

from database.db_postgres import Database
from server.routes.monitoring import get_analytics


class MetricsDatabaseStub(Database):
    def __init__(self):
        self._platform_metrics_cache = None
        self._platform_metrics_lock = asyncio.Lock()
        self.calls: Counter[str] = Counter()

    async def _fetchrow_on_own_connection(self, query: str):
        await asyncio.sleep(0.01)
        if query == self._PLATFORM_METRICS_CONTENT:
            self.calls["content"] += 1
            return {
                "meetings": 10,
                "summarized_meetings": 4,
                "summarized_items": 12,
                "items_analyzed": 20,
                "meeting_trend": [1] * 8,
                "item_trend": [2] * 8,
                "matter_trend": [3] * 8,
                "summary_trend": [4] * 8,
            }
        if query == self._PLATFORM_METRICS_INFRASTRUCTURE:
            self.calls["infrastructure"] += 1
            return {"committees": 3}
        if query == self._PLATFORM_METRICS_VOTES:
            self.calls["votes"] += 1
            return {
                "votes": 30,
                "vote_trend": [5] * 8,
                "votes_by_city": [{"city": "example", "votes": 30, "voters": 2}],
            }
        raise AssertionError("unexpected metrics query")


@pytest.mark.asyncio
async def test_platform_metrics_single_flight_and_cache():
    db = MetricsDatabaseStub()

    first, second, third = await asyncio.gather(
        db.get_platform_metrics(),
        db.get_platform_metrics(),
        db.get_platform_metrics(),
    )

    assert first is second is third
    assert db.calls == {"content": 1, "infrastructure": 1, "votes": 1}
    assert first["meeting_summary_rate"] == 40.0
    assert first["item_summary_rate"] == 60.0
    assert first["trends"] == {
        "meetings": [1] * 8,
        "items": [2] * 8,
        "matters": [3] * 8,
        "votes": [5] * 8,
        "summaries": [4] * 8,
    }

    assert await db.get_platform_metrics() is first
    assert db.calls == {"content": 1, "infrastructure": 1, "votes": 1}


def test_content_snapshot_scans_items_heap_once():
    query = Database._PLATFORM_METRICS_CONTENT

    assert query.count("FROM items i") == 1
    assert "FROM item_flags" in query
    assert "MATERIALIZED" in query


@pytest.mark.asyncio
async def test_analytics_reuses_platform_snapshot():
    metrics = {
        "total_cities": 100,
        "active_cities": 80,
        "total_cities_only": 60,
        "total_counties": 20,
        "total_school_districts": 20,
        "active_cities_only": 50,
        "active_counties": 15,
        "active_school_districts": 15,
        "live_cities": 40,
        "live_counties": 10,
        "live_school_districts": 10,
        "live_jurisdictions_total": 60,
        "frequently_updated": 25,
        "frequently_updated_pop": 1_000_000,
        "meetings": 500,
        "live_meetings": 300,
        "meetings_with_items": 450,
        "packets_count": 400,
        "summaries_count": 100,
        "agenda_items": 5_000,
        "matters": 2_000,
        "matters_with_summary": 700,
        "standalone_items": 50,
        "total_pop": 2_000_000,
        "pop_with_data": 1_500_000,
        "pop_with_summaries": 1_200_000,
    }

    class AnalyticsDatabaseStub:
        calls = 0

        async def get_platform_metrics(self):
            self.calls += 1
            return metrics

    db = AnalyticsDatabaseStub()
    response = await get_analytics(db)  # type: ignore[arg-type]

    assert db.calls == 1
    assert response["real_metrics"]["meetings_tracked"] == 500
    assert response["real_metrics"]["unique_item_summaries"] == 750

