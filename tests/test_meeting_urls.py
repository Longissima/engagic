"""Canonical backend meeting-slug and consumer contracts."""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from server.utils.meeting_urls import generate_meeting_slug
from database.repositories_async.items import ItemRepository
from database.repositories_async.matters import MatterRepository
from userland.database.models import Alert
from userland.matching.matcher import match_alert, match_matters_for_alert


@pytest.mark.parametrize(
    ("meeting_date", "expected"),
    [
        (datetime(2026, 8, 7, 18, 30), "2026-08-07-meeting-1"),
        (
            datetime(2026, 8, 7, 18, 30, tzinfo=timezone.utc),
            "2026-08-07-meeting-1",
        ),
        (date(2026, 8, 7), "2026-08-07-meeting-1"),
        ("2026-08-07T18:30:00Z", "2026-08-07-meeting-1"),
        ("2026-08-07 - 6:30 PM", "2026-08-07-meeting-1"),
        (None, "undated-meeting-1"),
        ("", "undated-meeting-1"),
        ("not-a-date", "undated-meeting-1"),
    ],
)
def test_generate_meeting_slug_matches_frontend_contract(
    meeting_date, expected
) -> None:
    assert generate_meeting_slug("meeting-1", meeting_date) == expected


def alert() -> Alert:
    return Alert(
        id="alert-1",
        user_id="user-1",
        name="Housing",
        cities=["exampleCA"],
        criteria={"keywords": ["housing"]},
    )


@pytest.mark.asyncio
async def test_retrospective_alert_searches_use_ingest_time_for_undated_work():
    queries = []

    class Connection:
        async def fetch(self, query, *_args):
            queries.append(" ".join(query.split()))
            return []

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    pool = cast(Any, Pool())
    await ItemRepository(pool).search_by_keyword(
        "exampleCA",
        "housing",
        "2026-08-06T00:00:00",
    )
    await MatterRepository(pool).search_by_keyword(
        ["exampleCA"],
        "housing",
        "2026-08-06T00:00:00",
    )

    assert (
        "COALESCE( m.date, i.summary_updated_at, i.created_at ) >= $2"
        in queries[0]
    )
    assert "i.summary_updated_at DESC NULLS LAST" in queries[0]
    assert "JOIN LATERAL" in queries[1]
    assert (
        "COALESCE(ma.appeared_at, appearance_meeting.created_at)"
        in queries[1]
    )
    assert "freshness.latest_activity_at >= $2" in queries[1]
    assert "ORDER BY freshness.latest_activity_at DESC" in queries[1]


@pytest.mark.asyncio
async def test_item_alert_uses_undated_meeting_slug() -> None:
    class Items:
        async def search_by_keyword(self, **_kwargs):
            return [
                {
                    "id": "item-1",
                    "meeting_id": "meeting-1",
                    "summary": "Housing update",
                    "date": None,
                    "banana": "exampleCA",
                    "city_name": "Example",
                    "state": "CA",
                    "meeting_title": "City Council",
                    "title": "Housing",
                }
            ]

    class Userland:
        async def get_matches(self, **_kwargs):
            return []

    matches = await match_alert(
        alert(), cast(Any, SimpleNamespace(items=Items(), userland=Userland()))
    )

    assert matches[0].matched_criteria["url"] == (
        "https://engagic.org/exampleCA/undated-meeting-1?item=item-item-1"
    )
    assert matches[0].matched_criteria["date"] is None


@pytest.mark.asyncio
async def test_matter_alert_uses_undated_latest_appearance_slug() -> None:
    class Matters:
        async def search_by_keyword(self, **_kwargs):
            return [
                {
                    "id": "matter-1",
                    "banana": "exampleCA",
                    "matter_file": "ORD-1",
                    "matter_type": "Ordinance",
                    "title": "Housing",
                    "city_name": "Example",
                    "state": "CA",
                    "canonical_summary": "Housing update",
                    "sponsors": [],
                    "canonical_topics": ["housing"],
                    "first_seen": None,
                    "last_seen": None,
                    "appearance_count": 1,
                }
            ]

        async def check_existing_match(self, *_args):
            return False

        async def get_timeline(self, _matter_id):
            return [
                {
                    "appeared_at": None,
                    "committee": "City Council",
                    "action": None,
                    "meeting_title": "City Council",
                    "meeting_id": "meeting-1",
                    "item_id": "item-1",
                }
            ]

    matches = await match_matters_for_alert(
        alert(), cast(Any, SimpleNamespace(matters=Matters()))
    )

    assert matches[0].matched_criteria["url"] == (
        "https://engagic.org/exampleCA/undated-meeting-1?item=item-item-1"
    )
    assert matches[0].matched_criteria["timeline"] == [
        {
            "date": None,
            "committee": "City Council",
            "action": None,
            "meeting_title": "City Council",
        }
    ]
    assert matches[0].matched_criteria["first_seen"] is None
    assert matches[0].matched_criteria["last_seen"] is None
