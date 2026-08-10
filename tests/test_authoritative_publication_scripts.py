"""Atomic publication contracts for administrative meeting writers."""

from types import SimpleNamespace
from typing import cast

import pytest

from database.db_postgres import Database
from database.models import AgendaItem, Meeting
from pipeline.utils import matter_work_version, meeting_work_version
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
    items = [
        SimpleNamespace(
            id="item-1", sequence=1, title="Current item", matter_id=None
        )
    ]
    publication: dict = {}

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def fetch(self, query, item_ids, meeting_id, below):
            assert "SET summary = NULL" in query
            assert "prompts_version < $3" in query
            assert "RETURNING matter_id" in query
            assert item_ids == ["item-1", "item-2"]
            assert meeting_id == "meeting-1"
            assert below == "v3"
            events.append("unfreeze")
            return [{"matter_id": None}, {"matter_id": None}]

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

        async def count_open_for_matter(self, *_args, **_kwargs):
            raise AssertionError("matterless items must not probe matter batches")

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

    assert result == (2, 1, 0)
    assert publication["banana"] == "currentCA"
    assert publication["work_version"] == meeting_work_version(meeting, items)
    assert events == [
        "begin",
        "lock meeting",
        "check batch",
        "lock items",
        "unfreeze",
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

        async def fetch(self, query, item_ids, meeting_id, below):
            assert "prompts_version < $3" in query
            assert (item_ids, meeting_id, below) == (
                ["item-1"],
                "meeting-1",
                "v3",
            )
            events.append("recheck target")
            return []

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
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                "meeting-1",
                connection,
                True,
            )
            events.append("lock items")
            return [
                SimpleNamespace(
                    id="item-1", sequence=1, title="Current item", matter_id=None
                )
            ]

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

    assert result == (0, 0, 0)
    assert events == [
        "begin",
        "lock meeting",
        "check batch",
        "lock items",
        "recheck target",
        "commit",
    ]


@pytest.mark.asyncio
async def test_resummarize_defers_meeting_with_open_batch_work() -> None:
    events: list[str] = []

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def fetch(self, *_args, **_kwargs):
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

    assert result == (0, 0, 0)
    assert events == ["begin", "lock meeting", "check batch", "commit"]


@pytest.mark.asyncio
async def test_resummarize_invalidates_matter_and_publishes_in_one_transaction() -> None:
    events: list[str] = []
    meeting = SimpleNamespace(
        id="meeting-1", banana="currentCA", title="Current meeting"
    )
    items = [
        SimpleNamespace(
            id="item-1", sequence=1, title="Widen Main St", matter_id="m-1"
        )
    ]
    appearances = [
        SimpleNamespace(
            id="item-1",
            meeting_id="meeting-1",
            sequence=1,
            title="Widen Main St",
            attachments=[],
            summary=None,
        ),
        SimpleNamespace(
            id="item-9",
            meeting_id="meeting-9",
            sequence=3,
            title="Widen Main St",
            attachments=[],
            summary="outside-target snapshot stays frozen",
        ),
    ]
    publications: dict[str, dict] = {}

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def fetch(self, query, item_ids, meeting_id, below):
            assert "RETURNING matter_id" in query
            assert (item_ids, meeting_id, below) == (
                ["item-1"],
                "meeting-1",
                "v3",
            )
            events.append("unfreeze")
            return [{"matter_id": "m-1"}]

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

        async def get_all_items_for_matter(self, matter_id, *, conn, lock_for_update):
            assert (matter_id, conn, lock_for_update) == ("m-1", connection, True)
            events.append("lock matter items")
            return appearances

    class Matters:
        async def get_matter(self, matter_id, *, conn, lock_for_update):
            assert (matter_id, conn, lock_for_update) == ("m-1", connection, True)
            events.append("lock matter")
            return SimpleNamespace(
                id="m-1", banana="currentCA", canonical_summary="old-prompt summary"
            )

        async def invalidate_canonical_summary(self, matter_id, *, conn):
            assert (matter_id, conn) == ("m-1", connection)
            events.append("invalidate matter")
            return True

    class Queue:
        async def enqueue_job(self, *, conn, **kwargs):
            assert conn is connection
            events.append(f"enqueue {kwargs['job_type']}")
            publications[kwargs["job_type"]] = kwargs

        async def reactivate_job_version(self, *, conn, source_url, **kwargs):
            assert conn is connection
            job_type = "matter" if source_url.startswith("matter://") else "meeting"
            events.append(f"reactivate {job_type}")
            assert source_url == publications[job_type]["source_url"]
            assert kwargs["work_version"] == publications[job_type]["work_version"]
            return True

    class BatchJobs:
        async def count_open_for_meeting(self, meeting_id, *, conn):
            assert (meeting_id, conn) == ("meeting-1", connection)
            events.append("check batch")
            return 0

        async def count_open_for_matter(self, matter_id, *, conn):
            assert (matter_id, conn) == ("m-1", connection)
            events.append("check matter batch")
            return 0

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        items=Items(),
        matters=Matters(),
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

    assert result == (1, 1, 1)
    matter_publication = publications["matter"]
    assert matter_publication["source_url"] == "matter://m-1"
    assert matter_publication["payload"] == {"matter_id": "m-1"}
    assert matter_publication["meeting_id"] is None
    assert matter_publication["banana"] == "currentCA"
    assert matter_publication["priority"] == resummarize_items.BACKFILL_PRIORITY
    assert matter_publication["work_version"] == matter_work_version(appearances)
    # The canonical invalidation and its queue publication share one
    # begin/commit pair, separate from the per-meeting transaction.
    assert events == [
        "begin",
        "lock meeting",
        "check batch",
        "lock items",
        "check matter batch",
        "unfreeze",
        "enqueue meeting",
        "reactivate meeting",
        "commit",
        "begin",
        "lock matter",
        "lock matter items",
        "invalidate matter",
        "enqueue matter",
        "reactivate matter",
        "commit",
    ]


@pytest.mark.asyncio
async def test_resummarize_defers_matter_with_open_batch_work() -> None:
    events: list[str] = []
    items_by_meeting = {
        "meeting-1": [
            SimpleNamespace(
                id="item-1", sequence=1, title="Deferred matter item", matter_id="m-1"
            )
        ],
        "meeting-2": [
            SimpleNamespace(
                id="item-2", sequence=1, title="Deferred sibling", matter_id="m-1"
            ),
            SimpleNamespace(
                id="item-3", sequence=2, title="Free item", matter_id=None
            ),
        ],
    }
    publication: dict = {}

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def fetch(self, query, item_ids, meeting_id, below):
            assert "RETURNING matter_id" in query
            # The deferred matter's items are excluded before the unfreeze so
            # they keep their below-version provenance for a future run.
            assert (item_ids, meeting_id, below) == (
                ["item-3"],
                "meeting-2",
                "v3",
            )
            events.append("unfreeze")
            return [{"matter_id": None}]

    connection = Connection()

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert conn is connection and lock_for_update
            events.append(f"lock {meeting_id}")
            return SimpleNamespace(id=meeting_id, banana="currentCA")

    class Items:
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert conn is connection and lock_for_update
            events.append("lock items")
            return items_by_meeting[meeting_id]

    class Matters:
        def __getattr__(self, _name):
            raise AssertionError("deferred matter must not be invalidated")

    class Queue:
        async def enqueue_job(self, *, conn, **kwargs):
            assert conn is connection
            assert kwargs["job_type"] == "meeting"
            events.append("enqueue meeting")
            publication.update(kwargs)

        async def reactivate_job_version(self, *, conn, **kwargs):
            assert conn is connection
            assert kwargs["work_version"] == publication["work_version"]
            events.append("reactivate meeting")
            return True

    class BatchJobs:
        async def count_open_for_meeting(self, meeting_id, *, conn):
            assert conn is connection
            events.append("check batch")
            return 0

        async def count_open_for_matter(self, matter_id, *, conn):
            assert (matter_id, conn) == ("m-1", connection)
            events.append("check matter batch")
            return 1

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        items=Items(),
        matters=Matters(),
        queue=Queue(),
        batch_jobs=BatchJobs(),
    )

    result = await resummarize_items.apply(
        cast(Database, db),
        [
            {
                "meeting_id": "meeting-1",
                "banana": "currentCA",
                "item_ids": ["item-1"],
            },
            {
                "meeting_id": "meeting-2",
                "banana": "currentCA",
                "item_ids": ["item-2", "item-3"],
            },
        ],
        "v3",
    )

    assert result == (1, 1, 0)
    assert events == [
        # meeting-1: every candidate belongs to the deferred matter -- nothing
        # is written at all.
        "begin",
        "lock meeting-1",
        "check batch",
        "lock items",
        "check matter batch",
        "commit",
        # meeting-2: only the matterless item unfreezes; m-1 stays untouched.
        "begin",
        "lock meeting-2",
        "check batch",
        "lock items",
        "check matter batch",
        "unfreeze",
        "enqueue meeting",
        "reactivate meeting",
        "commit",
    ]


@pytest.mark.asyncio
async def test_resummarize_invalidates_shared_matter_once() -> None:
    events: list[str] = []
    items_by_meeting = {
        "meeting-1": [
            SimpleNamespace(
                id="item-1", sequence=1, title="Shared matter", matter_id="m-1"
            )
        ],
        "meeting-2": [
            SimpleNamespace(
                id="item-2", sequence=1, title="Shared matter", matter_id="m-1"
            )
        ],
    }
    appearances = [
        SimpleNamespace(
            id="item-1",
            meeting_id="meeting-1",
            sequence=1,
            title="Shared matter",
            attachments=[],
            summary=None,
        ),
        SimpleNamespace(
            id="item-2",
            meeting_id="meeting-2",
            sequence=1,
            title="Shared matter",
            attachments=[],
            summary=None,
        ),
    ]
    matter_publications: list[dict] = []

    class Connection:
        def transaction(self):
            return _Transaction(self, events)

        async def fetch(self, query, item_ids, meeting_id, below):
            assert "RETURNING matter_id" in query
            events.append("unfreeze")
            return [{"matter_id": "m-1"}]

    connection = Connection()

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert conn is connection and lock_for_update
            events.append("lock meeting")
            return SimpleNamespace(id=meeting_id, banana="currentCA")

    class Items:
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert conn is connection and lock_for_update
            events.append("lock items")
            return items_by_meeting[meeting_id]

        async def get_all_items_for_matter(self, matter_id, *, conn, lock_for_update):
            assert (matter_id, conn, lock_for_update) == ("m-1", connection, True)
            events.append("lock matter items")
            return appearances

    class Matters:
        async def get_matter(self, matter_id, *, conn, lock_for_update):
            assert (matter_id, conn, lock_for_update) == ("m-1", connection, True)
            events.append("lock matter")
            return SimpleNamespace(
                id="m-1", banana="currentCA", canonical_summary="old-prompt summary"
            )

        async def invalidate_canonical_summary(self, matter_id, *, conn):
            assert (matter_id, conn) == ("m-1", connection)
            events.append("invalidate matter")
            return True

    class Queue:
        async def enqueue_job(self, *, conn, **kwargs):
            assert conn is connection
            events.append(f"enqueue {kwargs['job_type']}")
            if kwargs["job_type"] == "matter":
                matter_publications.append(kwargs)

        async def reactivate_job_version(self, *, conn, source_url, **kwargs):
            assert conn is connection
            job_type = "matter" if source_url.startswith("matter://") else "meeting"
            events.append(f"reactivate {job_type}")
            return True

    class BatchJobs:
        async def count_open_for_meeting(self, meeting_id, *, conn):
            assert conn is connection
            events.append("check batch")
            return 0

        async def count_open_for_matter(self, matter_id, *, conn):
            assert (matter_id, conn) == ("m-1", connection)
            events.append("check matter batch")
            return 0

    db = SimpleNamespace(
        pool=_Pool(connection),
        meetings=Meetings(),
        items=Items(),
        matters=Matters(),
        queue=Queue(),
        batch_jobs=BatchJobs(),
    )

    result = await resummarize_items.apply(
        cast(Database, db),
        [
            {
                "meeting_id": "meeting-1",
                "banana": "currentCA",
                "item_ids": ["item-1"],
            },
            {
                "meeting_id": "meeting-2",
                "banana": "currentCA",
                "item_ids": ["item-2"],
            },
        ],
        "v3",
    )

    assert result == (2, 2, 1)
    # The matter reached through both targeted meetings is invalidated and
    # published exactly once.
    assert events.count("lock matter") == 1
    assert events.count("invalidate matter") == 1
    assert events.count("enqueue matter") == 1
    assert events.count("reactivate matter") == 1
    assert len(matter_publications) == 1
    assert matter_publications[0]["source_url"] == "matter://m-1"
    assert matter_publications[0]["work_version"] == matter_work_version(appearances)


TRANSACTIONAL_NAME_RE = (
    '"name": *"[^"]*(quote|contract|agreement|proposal|exhibit|purchase order|'
    'order form|pricing|bid tab|statement of work|sow)[^"]*"'
)


class _CapturingConnection:
    def __init__(self):
        self.sql: str | None = None
        self.params: tuple = ()

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return []


@pytest.mark.asyncio
async def test_find_targets_without_attachments_flag_keeps_sql_shape() -> None:
    connection = _CapturingConnection()
    db = SimpleNamespace(pool=_Pool(connection))

    result = await resummarize_items.find_targets(
        cast(Database, db), "v3", "gainesvilleFL", 200
    )

    assert result == []
    assert connection.sql is not None
    assert (
        "WHERE i.summary IS NOT NULL"
        " AND (i.prompts_version IS NULL OR i.prompts_version < $1)"
        " AND m.banana = $2" in connection.sql
    )
    assert "~*" not in connection.sql
    assert connection.sql.rstrip().endswith("LIMIT $3")
    assert connection.params == ("v3", "gainesvilleFL", 200)


@pytest.mark.asyncio
async def test_find_targets_attachments_regex_appends_numbered_condition() -> None:
    connection = _CapturingConnection()
    db = SimpleNamespace(pool=_Pool(connection))

    result = await resummarize_items.find_targets(
        cast(Database, db),
        "v3.2",
        "gainesvilleFL",
        200,
        attachments_matching=TRANSACTIONAL_NAME_RE,
    )

    assert result == []
    assert connection.sql is not None
    assert (
        "WHERE i.summary IS NOT NULL"
        " AND (i.prompts_version IS NULL OR i.prompts_version < $1)"
        " AND m.banana = $2"
        " AND i.attachments::text ~* $3" in connection.sql
    )
    assert connection.sql.rstrip().endswith("LIMIT $4")
    # The regex travels as a bind parameter, never interpolated into the SQL.
    assert TRANSACTIONAL_NAME_RE not in connection.sql
    assert connection.params == ("v3.2", "gainesvilleFL", TRANSACTIONAL_NAME_RE, 200)


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
