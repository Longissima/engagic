"""Atomic publication contracts for administrative meeting writers."""

from types import SimpleNamespace
from typing import cast

import pytest

from database.db_postgres import Database
from database.models import AgendaItem, Meeting
from pipeline.utils import meeting_work_version
from scripts import ingest_manual_pdfs, resummarize_items


class _Transaction:
    def __init__(self, connection, events):
        self.connection = connection
        self.events = events

    async def __aenter__(self):
        self.events.append("begin")
        return self.connection

    async def __aexit__(self, exc_type, *_args):
        self.events.append("rollback" if exc_type else "commit")
        return False


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_resummarize_unfreeze_and_exact_retry_commit_together() -> None:
    events: list[str] = []
    meeting = SimpleNamespace(
        id="meeting-1", banana="currentCA", title="Current meeting"
    )
    items = [SimpleNamespace(id="item-1", sequence=1, title="Current item")]
    publication: dict = {}

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def execute(self, query, item_ids, meeting_id, below):
            assert "SET summary = NULL" in query
            assert "prompts_version < $3" in query
            assert item_ids == ["item-1", "item-2"]
            assert meeting_id == "meeting-1"
            assert below == "v3"
            events.append("unfreeze")
            return "UPDATE 2"

    connection = Connection()

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock meeting")
            return meeting

    class Items:
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock items")
            return items

    class Queue:
        async def enqueue_job(self, *, conn, **kwargs):
            assert conn is connection
            events.append("enqueue")
            publication.update(kwargs)

        async def reactivate_job_version(self, *, conn, **kwargs):
            assert conn is connection
            events.append("reactivate")
            assert kwargs["work_version"] == publication["work_version"]
            return True

    class BatchJobs:
        async def count_open_for_meeting(self, meeting_id, *, conn):
            assert (meeting_id, conn) == ("meeting-1", connection)
            events.append("check batch")
            return 0

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        items=Items(),
        queue=Queue(),
        batch_jobs=BatchJobs(),
    )

    result = await resummarize_items.apply(
        cast(Database, db),
        [
            {
                "meeting_id": "meeting-1",
                "banana": "staleCA",
                "item_ids": ["item-1", "item-2"],
            }
        ],
        "v3",
    )

    assert result == (2, 1)
    assert publication["banana"] == "currentCA"
    assert publication["work_version"] == meeting_work_version(meeting, items)
    assert events == [
        "begin",
        "lock meeting",
        "check batch",
        "unfreeze",
        "lock items",
        "enqueue",
        "reactivate",
        "commit",
    ]


@pytest.mark.asyncio
async def test_resummarize_skips_target_that_became_current_before_lock() -> None:
    events: list[str] = []

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def execute(self, query, item_ids, meeting_id, below):
            assert "prompts_version < $3" in query
            assert (item_ids, meeting_id, below) == (
                ["item-1"],
                "meeting-1",
                "v3",
            )
            events.append("recheck target")
            return "UPDATE 0"

    connection = Connection()

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock meeting")
            return SimpleNamespace(id="meeting-1", banana="currentCA")

    class Items:
        async def get_agenda_items(self, *_args, **_kwargs):
            raise AssertionError("current output must not be requeued")

    class Queue:
        async def enqueue_job(self, **_kwargs):
            raise AssertionError("current output must not be requeued")

        async def reactivate_job_version(self, **_kwargs):
            raise AssertionError("current output must not be reactivated")

    class BatchJobs:
        async def count_open_for_meeting(self, meeting_id, *, conn):
            assert (meeting_id, conn) == ("meeting-1", connection)
            events.append("check batch")
            return 0

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        items=Items(),
        queue=Queue(),
        batch_jobs=BatchJobs(),
    )

    result = await resummarize_items.apply(
        cast(Database, db),
        [
            {
                "meeting_id": "meeting-1",
                "banana": "staleCA",
                "item_ids": ["item-1"],
            }
        ],
        "v3",
    )

    assert result == (0, 0)
    assert events == [
        "begin",
        "lock meeting",
        "check batch",
        "recheck target",
        "commit",
    ]


@pytest.mark.asyncio
async def test_resummarize_defers_meeting_with_open_batch_work() -> None:
    events: list[str] = []

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("open provider work must fence unfreeze")

    connection = Connection()

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock meeting")
            return SimpleNamespace(id=meeting_id, banana="exampleCA")

    class BatchJobs:
        async def count_open_for_meeting(self, meeting_id, *, conn):
            assert (meeting_id, conn) == ("meeting-1", connection)
            events.append("check batch")
            return 1

    class Forbidden:
        def __getattr__(self, _name):
            raise AssertionError("deferred work must not touch items or queue")

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        batch_jobs=BatchJobs(),
        items=Forbidden(),
        queue=Forbidden(),
    )

    result = await resummarize_items.apply(
        cast(Database, db),
        [
            {
                "meeting_id": "meeting-1",
                "banana": "exampleCA",
                "item_ids": ["item-1"],
            }
        ],
        "v3",
    )

    assert result == (0, 0)
    assert events == ["begin", "lock meeting", "check batch", "commit"]


@pytest.mark.asyncio
async def test_manual_ingest_stores_and_publishes_authoritative_snapshot() -> None:
    events: list[str] = []
    proposed = SimpleNamespace(
        id="meeting-1", banana="exampleCA", title="Proposed title"
    )
    proposed_items = [
        SimpleNamespace(id="item-1", sequence=1, title="Proposed item")
    ]
    authoritative = SimpleNamespace(
        id="meeting-1", banana="exampleCA", title="Stored title"
    )
    authoritative_items = [
        SimpleNamespace(id="item-1", sequence=1, title="Stored frozen item")
    ]
    publication: dict = {}

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

    connection = Connection()

    class Meetings:
        async def store_meeting(self, meeting, *, conn):
            assert (meeting, conn) == (proposed, connection)
            events.append("store meeting")

        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock meeting")
            return authoritative

    class Items:
        async def store_agenda_items(self, meeting_id, items, *, conn):
            assert (meeting_id, items, conn) == (
                "meeting-1",
                proposed_items,
                connection,
            )
            events.append("store items")
            return 1

        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock items")
            return authoritative_items

    class Lifecycle:
        async def enqueue_queue_job(self, *, conn, **kwargs):
            assert conn is connection
            events.append("outbox")
            publication.update(kwargs)

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        items=Items(),
        pipeline_lifecycle=Lifecycle(),
    )

    stored = await ingest_manual_pdfs._store_and_publish_meeting(
        cast(Database, db),
        cast(Meeting, proposed),
        cast(list[AgendaItem], proposed_items),
    )

    assert stored == 1
    assert publication["work_version"] == meeting_work_version(
        authoritative, authoritative_items
    )
    assert publication["work_version"] != meeting_work_version(
        proposed, proposed_items
    )
    assert events == [
        "begin",
        "store meeting",
        "store items",
        "lock meeting",
        "lock items",
        "outbox",
        "commit",
    ]
