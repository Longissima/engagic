"""Contracts for meeting-sync unit-of-work and deterministic work versions."""

import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncpg import Connection

from database.id_generation import generate_matter_id, generate_meeting_id
from database.models import AttachmentInfo, MatterMetadata
from database.repositories_async.items import ItemRepository
from database.repositories_async.matters import MatterRepository
from database.repositories_async.pipeline_lifecycle import PipelineLifecycleRepository
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
import pipeline.orchestrators.meeting_sync as meeting_sync_module
from pipeline.utils import (
    MatterWorkSnapshot,
    aggregate_matter_attachments,
    hash_substantive_attachments,
    matter_attachment_version,
    matter_no_work_version,
    matter_work_version,
    meeting_work_version,
)


MATTER_ID = cast(str, generate_matter_id("alphaCA", matter_file="ORD-1"))


def _attachment(name: str, url: str) -> AttachmentInfo:
    return AttachmentInfo(name=name, url=url, type="pdf")


def _item(
    item_id: str,
    meeting_id: str,
    sequence: int,
    attachments: list[AttachmentInfo],
    *,
    body_text: str = "",
):
    return SimpleNamespace(
        id=item_id,
        meeting_id=meeting_id,
        sequence=sequence,
        title=f"Item {sequence}",
        matter_id=MATTER_ID,
        matter_file="ORD-1",
        matter_type="Ordinance",
        attachments=attachments,
        body_text=body_text,
        summary=None,
        filter_reason=None,
    )


def _meeting(**overrides):
    values = {
        "id": "meeting-1",
        "banana": "alphaCA",
        "title": "City Council",
        "date": datetime(2026, 2, 1, 18, 0),
        "agenda_url": "https://example.gov/agenda?id=1",
        "agenda_sources": None,
        "packet_url": "https://blob.example.gov/packet.pdf?sig=old",
        "minutes_url": None,
        "participation": None,
        "summary": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_aggregate_matter_version_is_stable_and_matches_processor_contract():
    first = _item(
        "item-1",
        "meeting-1",
        1,
        [_attachment("Ordinance", "https://blob.example.gov/ord.pdf?sig=one")],
    )
    second = _item(
        "item-2",
        "meeting-2",
        1,
        [_attachment("Exhibit", "https://example.gov/exhibit.pdf")],
    )

    expected = hash_substantive_attachments(
        aggregate_matter_attachments([first, second])
    )
    expected_work = matter_work_version([first, second])
    assert matter_attachment_version([second, first]) == expected
    assert matter_work_version([second, first]) == expected_work

    rotated = _item(
        "item-1",
        "meeting-1",
        1,
        [_attachment("Ordinance", "https://blob.example.gov/ord.pdf?sig=rotated")],
    )
    assert matter_attachment_version([rotated, second]) == expected
    assert matter_work_version([rotated, second]) == expected_work

    changed = _item(
        "item-3",
        "meeting-3",
        1,
        [_attachment("Amendment", "https://example.gov/amendment.pdf")],
    )
    assert matter_work_version([first, second, changed]) != expected_work

    retitled = _item(
        "item-1",
        "meeting-1",
        1,
        [_attachment("Ordinance", "https://blob.example.gov/ord.pdf?sig=one")],
    )
    retitled.title = "Substantively revised title"
    assert matter_attachment_version([retitled, second]) == expected
    assert matter_work_version([retitled, second]) != expected_work


def test_matter_no_work_versions_are_bounded_distinct_and_deterministic():
    executable = matter_work_version([])
    versions = {
        reason: matter_no_work_version(executable, reason)
        for reason in (
            "procedural",
            "no_appearances",
            "no_substantive_work",
        )
    }

    assert len(set(versions.values())) == 3
    assert executable not in versions.values()
    assert all(
        version.startswith(f"mnw1:{reason}:")
        for reason, version in versions.items()
    )
    assert matter_no_work_version(executable, "procedural") == versions["procedural"]
    with pytest.raises(ValueError, match="unsupported matter no-work reason"):
        matter_no_work_version(executable, cast(Any, "unbounded-policy-text"))
    with pytest.raises(ValueError, match="must be a matter work version"):
        matter_no_work_version("mnw1:procedural:not-executable", "procedural")


@pytest.mark.asyncio
async def test_repeated_undated_sync_uses_one_stable_meeting_identity(monkeypatch):
    generated_dates = []
    real_generate = generate_meeting_id

    def capture_meeting_id(*, banana, vendor_id, date, title):
        generated_dates.append(date)
        return real_generate(banana, vendor_id, date, title)

    monkeypatch.setattr(
        meeting_sync_module,
        "generate_meeting_id",
        capture_meeting_id,
    )

    class Transaction:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class SyncConnection:
        def transaction(self):
            return Transaction()

    connection = SyncConnection()

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Meetings:
        def __init__(self):
            self.rows = {}
            self.stored_ids = []

        async def get_meeting(self, meeting_id, **_kwargs):
            return self.rows.get(meeting_id)

        async def store_meeting(self, meeting, **_kwargs):
            self.rows[meeting.id] = meeting
            self.stored_ids.append(meeting.id)

    meetings = Meetings()
    database = SimpleNamespace(pool=Pool(), meetings=meetings)
    orchestrator = MeetingSyncOrchestrator(database)

    async def publish_authoritative(_self, *, meeting_id, **_kwargs):
        return meetings.rows[meeting_id]

    monkeypatch.setattr(
        MeetingSyncOrchestrator,
        "_publish_authoritative_work",
        publish_authoritative,
    )
    source = {
        "vendor_id": "vendor-undated-1",
        "title": "City Council",
        "start": None,
    }
    city = cast(Any, SimpleNamespace(banana="exampleCA"))

    first, _ = await orchestrator.sync_meeting(source, city)
    second, _ = await orchestrator.sync_meeting(source, city)

    assert first is not None and second is not None
    assert generated_dates == [None, None]
    assert first.id == second.id
    assert meetings.stored_ids == [first.id, first.id]
    assert set(meetings.rows) == {first.id}


def test_meeting_version_is_order_and_signature_stable_but_input_sensitive():
    first = _item(
        "item-1",
        "meeting-1",
        1,
        [_attachment("Packet", "https://blob.example.gov/item.pdf?sig=one")],
        body_text="original body",
    )
    second = _item("item-2", "meeting-1", 2, [])

    expected = meeting_work_version(_meeting(), [first, second])
    rotated_meeting = _meeting(
        packet_url="https://blob.example.gov/packet.pdf?sig=rotated"
    )
    rotated_item = _item(
        "item-1",
        "meeting-1",
        1,
        [_attachment("Packet", "https://blob.example.gov/item.pdf?sig=rotated")],
        body_text="original body",
    )
    assert meeting_work_version(rotated_meeting, [second, rotated_item]) == expected

    rotated_item.body_text = "amended body"
    assert meeting_work_version(rotated_meeting, [second, rotated_item]) != expected


class _NoAcquirePool:
    def acquire(self):
        raise AssertionError("repository attempted a nested pool acquisition")


class _ReadConnection:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return []

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return True


class _PopulatedSnapshotConnection(_ReadConnection):
    def __init__(self, matter_ids):
        super().__init__()
        self.matter_ids = matter_ids

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        now = datetime(2026, 2, 1)
        if "JOIN city_matters cm" in query:
            return [
                {
                    "id": matter_id,
                    "banana": "alphaCA",
                    "matter_id": None,
                    "matter_file": f"ORD-{index}",
                    "matter_type": "Ordinance",
                    "title": f"Matter {index}",
                    "sponsors": [],
                    "canonical_summary": "existing summary",
                    "canonical_topics": [],
                    "attachments": [],
                    "metadata": {"attachment_hash": "sv1:old"},
                    "first_seen": now,
                    "last_seen": now,
                    "appearance_count": 1,
                    "actual_item_count": 1,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "final_vote_date": None,
                    "quality_score": None,
                    "rating_count": 0,
                }
                for index, matter_id in enumerate(self.matter_ids, start=1)
            ]
        if "FROM matter_topics" in query or "FROM item_topics" in query:
            return []
        if "FROM items" in query:
            return [
                {
                    "id": f"item-{index}",
                    "meeting_id": "meeting-old",
                    "title": f"Matter {index}",
                    "sequence": index,
                    "attachments": [],
                    "attachment_hash": None,
                    "body_text": None,
                    "matter_id": matter_id,
                    "matter_file": f"ORD-{index}",
                    "matter_type": "Ordinance",
                    "agenda_number": str(index),
                    "sponsors": [],
                    "summary": "existing summary",
                    "topics": [],
                    "quality_score": None,
                    "rating_count": 0,
                    "filter_reason": None,
                }
                for index, matter_id in enumerate(self.matter_ids, start=1)
            ]
        if "FROM matter_appearances" in query:
            return [{"matter_id": matter_id} for matter_id in self.matter_ids]
        raise AssertionError(f"unexpected snapshot query: {query}")


@pytest.mark.asyncio
async def test_assigned_matter_reads_reuse_the_unit_of_work_connection():
    connection = _ReadConnection()
    matter_repository = MatterRepository(cast(Any, _NoAcquirePool()))
    item_repository = ItemRepository(cast(Any, _NoAcquirePool()))
    conn = cast(Connection, connection)

    assert await matter_repository.get_matter(MATTER_ID, conn=conn) is None
    assert await matter_repository.has_appearance(MATTER_ID, "meeting-1", conn=conn)
    assert await item_repository.get_all_items_for_matter(MATTER_ID, conn=conn) == []

    assert [call[0] for call in connection.calls] == [
        "fetchrow",
        "fetchval",
        "fetch",
    ]


@pytest.mark.asyncio
async def test_sync_snapshot_repository_reads_are_set_wise_and_lock_ordered():
    connection = _ReadConnection()
    matter_repository = MatterRepository(cast(Any, _NoAcquirePool()))
    item_repository = ItemRepository(cast(Any, _NoAcquirePool()))
    conn = cast(Connection, connection)
    other_id = cast(str, generate_matter_id("alphaCA", matter_file="ORD-2"))
    requested_ids = [other_id, MATTER_ID, other_id]
    expected_ids = sorted({MATTER_ID, other_id})

    assert (
        await matter_repository.get_matters_for_sync_snapshot(
            requested_ids,
            conn=conn,
        )
        == {}
    )
    assert (
        await item_repository.get_all_items_for_matters(
            requested_ids,
            conn=conn,
            lock_for_update=True,
        )
        == {}
    )
    assert (
        await matter_repository.get_existing_appearance_matter_ids(
            requested_ids,
            "meeting-1",
            conn=conn,
        )
        == set()
    )

    assert len(connection.calls) == 3
    matter_query = " ".join(connection.calls[0][1].split())
    item_query = " ".join(connection.calls[1][1].split())
    appearance_query = " ".join(connection.calls[2][1].split())
    assert "ORDER BY cm.id FOR UPDATE OF cm" in matter_query
    assert "pg_advisory_xact_lock" in matter_query
    assert "WITH requested AS MATERIALIZED" in matter_query
    assert "ORDER BY matter_id, meeting_id, sequence, id FOR UPDATE" in item_query
    assert "SELECT DISTINCT matter_id" in appearance_query
    assert all(call[2][0] == expected_ids for call in connection.calls)


@pytest.mark.asyncio
async def test_old_matter_link_discovery_is_ordered_and_nonlocking():
    other_id = cast(
        str, generate_matter_id("alphaCA", matter_file="ORD-2")
    )

    class LinkConnection(_ReadConnection):
        async def fetch(self, query, *args):
            self.calls.append(("fetch", query, args))
            return [
                {"id": "item-a", "matter_id": MATTER_ID},
                {"id": "item-b", "matter_id": other_id},
            ]

    connection = LinkConnection()
    repository = ItemRepository(cast(Any, _NoAcquirePool()))
    links = await repository.get_item_matter_links(
        "meeting-1",
        conn=cast(Connection, connection),
    )

    query = " ".join(connection.calls[0][1].split())
    assert links == {"item-a": MATTER_ID, "item-b": other_id}
    assert "WHERE meeting_id = $1" in query
    assert "ORDER BY id" in query
    assert "FOR UPDATE" not in query


@pytest.mark.asyncio
async def test_populated_sync_snapshot_reduces_sql_reads_from_5n_to_5():
    matter_ids = [
        cast(str, generate_matter_id("alphaCA", matter_file=f"ORD-{number}"))
        for number in range(1, 7)
    ]
    connection = _PopulatedSnapshotConnection(matter_ids)
    conn = cast(Connection, connection)
    matter_repository = MatterRepository(cast(Any, _NoAcquirePool()))
    item_repository = ItemRepository(cast(Any, _NoAcquirePool()))

    matters = await matter_repository.get_matters_for_sync_snapshot(
        matter_ids,
        conn=conn,
    )
    items = await item_repository.get_all_items_for_matters(
        matter_ids,
        conn=conn,
        lock_for_update=True,
    )
    appearances = await matter_repository.get_existing_appearance_matter_ids(
        matter_ids,
        "meeting-1",
        conn=conn,
    )

    assert len(matters) == len(items) == len(appearances) == len(matter_ids)
    legacy_sql_reads = 5 * len(matter_ids)
    snapshot_sql_reads = len(connection.calls)
    assert (legacy_sql_reads, snapshot_sql_reads) == (30, 5)


class _AtomicTransaction(AbstractAsyncContextManager):
    def __init__(self, connection):
        self.connection = connection
        self.snapshot = []

    async def __aenter__(self):
        self.snapshot = list(self.connection.records)
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.connection.records = self.snapshot
        return False


class _AtomicConnection:
    def __init__(self):
        self.records = []
        self.fetch_query = None

    def transaction(self):
        return _AtomicTransaction(self)

    async def execute(self, query, *args):
        assert "pipeline_outbox" in query
        self.records.append(("outbox", query, args))
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        self.fetch_query = " ".join(query.split())
        return None


class _AcquireConnection:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _SingleConnectionPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AcquireConnection(self.connection)


@pytest.mark.asyncio
async def test_outbox_event_rolls_back_with_domain_unit_of_work():
    connection = _AtomicConnection()
    repository = PipelineLifecycleRepository(cast(Any, _NoAcquirePool()))

    with pytest.raises(RuntimeError, match="force rollback"):
        async with connection.transaction():
            connection.records.append(("meeting", "meeting-1"))
            await repository.enqueue_queue_job(
                source_url="meeting://meeting-1",
                job_type="meeting",
                payload={"meeting_id": "meeting-1"},
                aggregate_id="meeting-1",
                meeting_id="meeting-1",
                banana="alphaCA",
                priority=150,
                work_version="mv1:abc",
                conn=cast(Connection, connection),
            )
            raise RuntimeError("force rollback")

    assert connection.records == []


@pytest.mark.asyncio
async def test_queue_outbox_slot_rearms_only_after_a_newer_desired_generation():
    connection = _AtomicConnection()
    repository = PipelineLifecycleRepository(cast(Any, _NoAcquirePool()))

    await repository.enqueue_outbox(
        event_key="queue.enqueue:meeting://meeting-1:mv1:abc",
        event_type="queue.enqueue",
        aggregate_type="meeting",
        aggregate_id="meeting-1",
        payload={"work_version": "mv1:abc"},
        conn=cast(Connection, connection),
    )

    query = " ".join(connection.records[0][1].split())
    assert connection.records[0][0] == "outbox"
    assert "'publishing', 'published', 'dead_letter'" in query
    assert "THEN pipeline_outbox.status ELSE 'pending' END" in query
    assert "THEN pipeline_outbox.payload ELSE EXCLUDED.payload END" in query
    assert "newer_queue.desired_generation > pipeline_outbox.work_generation" in query
    assert "newer_event.work_generation > pipeline_outbox.work_generation" in query
    assert "THEN EXCLUDED.work_generation" in query
    assert "published_at = CASE" in query
    assert "AND pipeline_outbox.status = 'publishing'" in query


@pytest.mark.asyncio
async def test_outbox_claim_preserves_version_order_per_aggregate():
    connection = _AtomicConnection()
    repository = PipelineLifecycleRepository(
        cast(Any, _SingleConnectionPool(connection))
    )

    assert await repository.claim_outbox() is None

    assert connection.fetch_query is not None
    assert "NOT EXISTS" in connection.fetch_query
    assert "earlier.aggregate_id = po.aggregate_id" in connection.fetch_query
    assert "earlier.work_generation < po.work_generation" in connection.fetch_query
    assert (
        "earlier.status NOT IN ('published', 'dead_letter')" in connection.fetch_query
    )


class _CaptureLifecycle:
    def __init__(self):
        self.calls = []

    async def enqueue_queue_job(self, **kwargs):
        self.calls.append(kwargs)


class _CaptureOutbox(PipelineLifecycleRepository):
    def __init__(self):
        self.calls = []

    async def enqueue_outbox(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_queue_publication_intent_is_version_keyed_and_consumer_ready():
    repository = _CaptureOutbox()
    connection = cast(Connection, object())

    await repository.enqueue_queue_job(
        source_url="matter://alphaCA_ord-1",
        job_type="matter",
        payload={"matter_id": "alphaCA_ord-1", "meeting_id": "meeting-1"},
        aggregate_id="alphaCA_ord-1",
        meeting_id="meeting-1",
        banana="alphaCA",
        priority=120,
        work_version="sv1:abc",
        conn=connection,
    )

    event = repository.calls[0]
    assert event["event_key"] == "queue.enqueue:matter://alphaCA_ord-1:sv1:abc"
    assert event["event_type"] == "queue.enqueue"
    assert event["aggregate_type"] == "matter"
    assert event["payload"]["work_version"] == "sv1:abc"
    assert event["conn"] is connection


@pytest.mark.asyncio
async def test_meeting_enqueue_uses_deterministic_version_and_uow_connection():
    lifecycle = _CaptureLifecycle()
    database = SimpleNamespace(pipeline_lifecycle=lifecycle)
    orchestrator = MeetingSyncOrchestrator(database)
    cast(Any, orchestrator).enqueue_decider = SimpleNamespace(
        should_enqueue=lambda *_args: (True, None),
        calculate_priority=lambda _date: 150,
    )
    connection = cast(Connection, object())
    meeting = _meeting()
    items = [_item("item-1", "meeting-1", 1, [])]
    chunk_audit = {
        "winning_rung": "text:auto",
        "runs": [{"profile": {"page_count": 1}}],
    }

    await orchestrator._enqueue_if_needed(
        cast(Any, meeting),
        meeting.date,
        cast(Any, items),
        conn=connection,
        chunk_audit=chunk_audit,
    )

    event = lifecycle.calls[0]
    expected_version = meeting_work_version(meeting, items)
    assert event["work_version"] == expected_version
    assert event["work_version"].startswith("mv1:")
    assert event["processing_metadata"] == {
        "chunk": {**chunk_audit, "work_version": expected_version}
    }
    assert "work_version" not in chunk_audit
    assert event["conn"] is connection


@pytest.mark.asyncio
async def test_sync_matter_tracking_defers_publication_and_reuses_connection():
    connection = cast(Connection, object())
    prior = _item(
        "item-old",
        "meeting-old",
        1,
        [_attachment("Original", "https://example.gov/original.pdf")],
    )
    current = _item(
        "item-new",
        "meeting-new",
        1,
        [_attachment("Amendment", "https://example.gov/amendment.pdf")],
    )
    existing = SimpleNamespace(
        metadata=MatterMetadata(attachment_hash="sv1:old"),
        canonical_summary="old summary",
    )
    calls = []

    class Matters:
        async def get_matters_for_sync_snapshot(self, matter_ids, *, conn):
            calls.append(("get_matters_for_sync_snapshot", conn))
            return {MATTER_ID: existing}

        async def get_existing_appearance_matter_ids(
            self, matter_ids, meeting_id, *, conn
        ):
            calls.append(("get_existing_appearance_matter_ids", conn))
            return set()

        async def update_matter_tracking(self, **kwargs):
            calls.append(("update_matter_tracking", kwargs["conn"]))

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            calls.append(("get_all_items_for_matters", conn))
            return {MATTER_ID: [prior]}

    database = SimpleNamespace(
        matters=Matters(),
        items=Items(),
        council_members=SimpleNamespace(),
    )
    orchestrator = MeetingSyncOrchestrator(database)
    meeting = _meeting(id="meeting-new")
    stats = await orchestrator._track_matters(
        cast(Any, meeting),
        [{"sequence": 1, "matter_type": "Ordinance", "votes": []}],
        cast(Any, [current]),
        affected_matter_ids={MATTER_ID},
        conn=connection,
    )

    assert stats["affected_matter_ids"] == {MATTER_ID}
    assert "pending_jobs" not in stats
    assert [call[0] for call in calls] == [
        "get_matters_for_sync_snapshot",
        "get_all_items_for_matters",
    ]
    assert all(call[1] is connection for call in calls)


@pytest.mark.asyncio
async def test_tracking_prepares_observed_votes_without_additive_relationship_writes():
    connection = cast(Connection, object())
    current = _item("item-new", "meeting-new", 1, [])

    class Matters:
        async def get_matters_for_sync_snapshot(self, matter_ids, *, conn):
            return {
                MATTER_ID: SimpleNamespace(
                    metadata=MatterMetadata(),
                    canonical_summary=None,
                )
            }

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            return {MATTER_ID: []}

    class CouncilMembers:
        async def link_sponsors_to_matter(self, **_kwargs):
            raise AssertionError("pre-reconcile additive sponsorship write")

        async def record_votes_for_matter(self, **_kwargs):
            raise AssertionError("pre-reconcile additive vote write")

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(
            matters=Matters(),
            items=Items(),
            council_members=CouncilMembers(),
        )
    )
    votes = [{"name": "Mayor Example", "vote": "yes"}]
    stats = await orchestrator._track_matters(
        cast(Any, _meeting(id="meeting-new")),
        [
            {
                "sequence": 1,
                "matter_type": "Ordinance",
                "sponsors": ["Mayor Example"],
                "votes": votes,
            }
        ],
        cast(Any, [current]),
        affected_matter_ids={MATTER_ID},
        conn=connection,
    )

    assert stats["observed_votes"] == {MATTER_ID: votes}
    assert stats["appearance_outcomes"]["item-new"]["matter_id"] == MATTER_ID

    empty_stats = await orchestrator._track_matters(
        cast(Any, _meeting(id="meeting-new")),
        [{"sequence": 1, "matter_type": "Ordinance", "votes": []}],
        cast(Any, [current]),
        affected_matter_ids={MATTER_ID},
        conn=connection,
    )
    assert empty_stats["observed_votes"] == {MATTER_ID: []}


@pytest.mark.asyncio
async def test_publication_versions_use_rows_retained_after_upserts():
    """Proposed mutable values must never leak into either desired version."""
    connection = cast(Connection, object())
    retained_meeting = _meeting(
        minutes_url="https://example.gov/retained-minutes.pdf",
        participation={"email": "retained@example.gov"},
    )
    proposed_meeting = _meeting(
        # These NULLs are rejected by MeetingRepository's COALESCE clauses.
        minutes_url=None,
        participation=None,
    )
    retained_current = _item(
        "item-current",
        "meeting-1",
        1,
        [_attachment("Retained", "https://example.gov/retained.pdf")],
        body_text="retained frozen body",
    )
    retained_current.title = "Retained frozen title"
    proposed_current = _item(
        "item-current",
        "meeting-1",
        1,
        [_attachment("Proposed", "https://example.gov/proposed.pdf")],
        body_text="proposed replacement body",
    )
    proposed_current.title = "Proposed replacement title"
    prior = _item(
        "item-prior",
        "meeting-prior",
        1,
        [_attachment("Original", "https://example.gov/original.pdf")],
    )
    existing_matter = SimpleNamespace(
        title="Existing matter title",
        metadata=MatterMetadata(attachment_hash="sv1:old"),
        canonical_summary="old summary",
    )
    read_order = []
    tracking_calls = []

    class Meetings:
        async def get_meeting(self, meeting_id, conn=None, *, lock_for_update=False):
            assert meeting_id == "meeting-1"
            assert lock_for_update is True
            read_order.append(("meeting", conn))
            return retained_meeting

    class Matters:
        async def get_matters_for_sync_snapshot(
            self, matter_ids, *, conn, **_kwargs
        ):
            assert matter_ids == [MATTER_ID]
            read_order.append(("matters", conn))
            return {MATTER_ID: existing_matter}

        async def get_authoritative_tracking_for_matters(
            self, matter_ids, *, conn
        ):
            assert matter_ids == [MATTER_ID]
            read_order.append(("tracking", conn))
            return {
                MATTER_ID: {
                    "appearance_count": 2,
                    "first_seen": datetime(2026, 1, 1),
                    "last_seen": datetime(2026, 2, 1),
                }
            }

        async def refresh_matter_tracking(self, **kwargs):
            tracking_calls.append(kwargs)

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            assert matter_ids == [MATTER_ID]
            assert lock_for_update is True
            read_order.append(("matter_items", conn))
            return {MATTER_ID: [prior, retained_current]}

        async def get_agenda_items(
            self,
            meeting_id,
            conn=None,
            *,
            lock_for_update=False,
        ):
            assert meeting_id == "meeting-1"
            assert lock_for_update is True
            read_order.append(("meeting_items", conn))
            return [retained_current]

        async def copy_summary_from_prior_appearance(self, **_kwargs):
            raise AssertionError("changed authoritative work must not copy")

    lifecycle = _CaptureLifecycle()
    database = SimpleNamespace(
        meetings=Meetings(),
        matters=Matters(),
        items=Items(),
        pipeline_lifecycle=lifecycle,
    )
    orchestrator = MeetingSyncOrchestrator(database)

    await orchestrator._publish_authoritative_work(
        meeting_id="meeting-1",
        affected_matter_ids={MATTER_ID},
        procedural_matter_ids=set(),
        conn=connection,
        publish_meeting=True,
    )

    authoritative_matter_version = matter_work_version([prior, retained_current])
    proposed_matter_version = matter_work_version([prior, proposed_current])
    authoritative_meeting_version = meeting_work_version(
        retained_meeting, [retained_current]
    )
    proposed_meeting_version = meeting_work_version(
        proposed_meeting, [proposed_current]
    )
    events = {event["job_type"]: event for event in lifecycle.calls}

    assert read_order == [
        ("meeting", connection),
        ("matters", connection),
        ("matter_items", connection),
        ("tracking", connection),
        ("meeting_items", connection),
    ]
    assert authoritative_matter_version != proposed_matter_version
    assert authoritative_meeting_version != proposed_meeting_version
    assert events["matter"]["work_version"] == authoritative_matter_version
    assert events["matter"]["work_version"] != proposed_matter_version
    assert events["meeting"]["work_version"] == authoritative_meeting_version
    assert events["meeting"]["work_version"] != proposed_meeting_version
    assert all(event["conn"] is connection for event in lifecycle.calls)
    assert tracking_calls[0]["attachments"] == list(
        aggregate_matter_attachments([prior, retained_current])
    )
    assert tracking_calls[0]["appearance_count"] == 2
    assert tracking_calls[0]["first_seen"] == datetime(2026, 1, 1)
    assert tracking_calls[0]["last_seen"] == datetime(2026, 2, 1)


@pytest.mark.asyncio
async def test_frozen_authoritative_item_suppresses_proposed_reprocessing():
    """A changed scrape cannot requeue values rejected by freeze-on-summary."""
    connection = cast(Connection, object())
    retained_meeting = _meeting(
        minutes_url="https://example.gov/retained-minutes.pdf",
        participation={"email": "retained@example.gov"},
    )
    retained_item = _item(
        "item-current",
        "meeting-1",
        1,
        [_attachment("Retained", "https://example.gov/retained.pdf")],
        body_text="retained body",
    )
    retained_item.title = "Retained title"
    retained_item.summary = "Frozen summary"
    proposed_item = _item(
        "item-current",
        "meeting-1",
        1,
        [_attachment("Proposed", "https://example.gov/proposed.pdf")],
        body_text="proposed body",
    )
    proposed_item.title = "Proposed title"
    retained_work = MatterWorkSnapshot.from_appearances([retained_item])
    proposed_work = MatterWorkSnapshot.from_appearances([proposed_item])
    existing_matter = SimpleNamespace(
        title=retained_item.title,
        metadata=MatterMetadata(
            attachment_hash=retained_work.attachment_version,
            work_version=retained_work.work_version,
        ),
        canonical_summary="Frozen summary",
    )
    tracking_calls = []
    copied_targets = []

    class Meetings:
        async def get_meeting(self, meeting_id, conn=None, *, lock_for_update=False):
            assert lock_for_update is True
            return retained_meeting

    class Matters:
        async def get_matters_for_sync_snapshot(
            self, matter_ids, *, conn, **_kwargs
        ):
            return {MATTER_ID: existing_matter}

        async def get_authoritative_tracking_for_matters(
            self, matter_ids, *, conn
        ):
            return {
                MATTER_ID: {
                    "appearance_count": 1,
                    "first_seen": retained_meeting.date,
                    "last_seen": retained_meeting.date,
                }
            }

        async def refresh_matter_tracking(self, **kwargs):
            tracking_calls.append(kwargs)

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            return {MATTER_ID: [retained_item]}

        async def copy_summary_from_prior_appearance(self, **kwargs):
            copied_targets.append(kwargs["target_item_id"])
            return False

        async def get_agenda_items(
            self,
            meeting_id,
            conn=None,
            *,
            lock_for_update=False,
        ):
            assert lock_for_update is True
            return [retained_item]

    lifecycle = _CaptureLifecycle()
    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(
            meetings=Meetings(),
            matters=Matters(),
            items=Items(),
            pipeline_lifecycle=lifecycle,
        )
    )

    await orchestrator._publish_authoritative_work(
        meeting_id="meeting-1",
        affected_matter_ids={MATTER_ID},
        procedural_matter_ids=set(),
        conn=connection,
        publish_meeting=True,
    )

    assert retained_work.work_version != proposed_work.work_version
    assert lifecycle.calls == []
    assert copied_targets == ["item-current"]
    assert tracking_calls[0]["attachment_hash"] == retained_work.attachment_version
    assert tracking_calls[0]["work_version"] == retained_work.work_version
    assert tracking_calls[0]["work_version"] != proposed_work.work_version


@pytest.mark.asyncio
async def test_matter_sync_snapshot_collapses_per_item_read_amplification():
    connection = cast(Connection, object())
    matter_ids = [
        cast(str, generate_matter_id("alphaCA", matter_file=f"ORD-{number}"))
        for number in range(1, 7)
    ]
    agenda_items = []
    items_data = []
    existing_by_id = {}
    prior_by_id = {}
    read_queries = []

    for sequence, matter_id in enumerate(matter_ids, start=1):
        current = _item(
            f"item-new-{sequence}",
            "meeting-new",
            sequence,
            [_attachment("Current", f"https://example.gov/{sequence}.pdf")],
        )
        current.matter_id = matter_id
        current.matter_file = f"ORD-{sequence}"
        agenda_items.append(current)
        items_data.append(
            {"sequence": sequence, "matter_type": "Ordinance", "votes": []}
        )
        existing_by_id[matter_id] = SimpleNamespace(
            metadata=MatterMetadata(attachment_hash="sv1:old"),
            canonical_summary="old summary",
        )
        prior = _item(
            f"item-old-{sequence}",
            "meeting-old",
            sequence,
            [_attachment("Prior", f"https://example.gov/old-{sequence}.pdf")],
        )
        prior.matter_id = matter_id
        prior_by_id[matter_id] = [prior]

    class Matters:
        async def get_matters_for_sync_snapshot(self, ids, *, conn):
            read_queries.append(("matters", tuple(ids), conn))
            return existing_by_id

        async def get_matter(self, *args, **kwargs):
            raise AssertionError("point matter read reintroduced")

        async def has_appearance(self, *args, **kwargs):
            raise AssertionError("point appearance read reintroduced")

    class Items:
        async def get_all_items_for_matters(
            self, ids, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            read_queries.append(("items", tuple(ids), conn))
            return prior_by_id

        async def get_all_items_for_matter(self, *args, **kwargs):
            raise AssertionError("point item read reintroduced")

    database = SimpleNamespace(
        matters=Matters(),
        items=Items(),
        council_members=SimpleNamespace(),
    )
    orchestrator = MeetingSyncOrchestrator(database)

    await orchestrator._track_matters(
        cast(Any, _meeting(id="meeting-new")),
        items_data,
        cast(Any, agenda_items),
        affected_matter_ids=set(matter_ids),
        conn=connection,
    )

    legacy_read_queries = 2 * len(matter_ids)
    snapshot_read_queries = len(read_queries)
    assert (legacy_read_queries, snapshot_read_queries) == (12, 2)
    assert [query[0] for query in read_queries] == [
        "matters",
        "items",
    ]
    assert all(query[1] == tuple(sorted(matter_ids)) for query in read_queries)
    assert all(query[2] is connection for query in read_queries)


def test_relink_affected_set_is_retained_old_plus_proposed_new():
    old_matter_id = cast(
        str, generate_matter_id("alphaCA", matter_file="ORD-OLD")
    )
    new_matter_id = cast(
        str, generate_matter_id("alphaCA", matter_file="ORD-NEW")
    )
    relinked = _item("item-relinked", "meeting-1", 1, [])
    relinked.matter_id = new_matter_id

    affected = MeetingSyncOrchestrator._affected_matter_ids(
        {"item-relinked": old_matter_id},
        cast(Any, [relinked]),
    )

    assert affected == {old_matter_id, new_matter_id}


@pytest.mark.asyncio
async def test_concurrent_relinks_lock_the_same_union_in_the_same_order():
    matter_ids = {
        cast(str, generate_matter_id("alphaCA", matter_file="ORD-A")),
        cast(str, generate_matter_id("alphaCA", matter_file="ORD-B")),
    }
    expected = tuple(sorted(matter_ids))
    events = []

    class Matters:
        async def get_matters_for_sync_snapshot(self, ids, *, conn):
            events.append((conn, "matters", tuple(ids)))
            await asyncio.sleep(0)
            return {}

    class Items:
        async def get_all_items_for_matters(
            self, ids, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            events.append((conn, "items", tuple(ids)))
            await asyncio.sleep(0)
            return {}

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(matters=Matters(), items=Items())
    )
    first_conn = cast(Connection, object())
    second_conn = cast(Connection, object())

    await asyncio.gather(
        orchestrator._load_matter_sync_snapshot(set(reversed(expected)), first_conn),
        orchestrator._load_matter_sync_snapshot(set(expected), second_conn),
    )

    for conn in (first_conn, second_conn):
        assert [event[1:] for event in events if event[0] is conn] == [
            ("matters", expected),
            ("items", expected),
        ]


class _AppearanceReconcileConnection:
    def __init__(self):
        self.calls = []
        self.run = 0

    async def execute(self, query, *args):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if normalized.startswith("DELETE"):
            self.run += 1
            return "DELETE 1" if self.run == 1 else "DELETE 0"
        return "INSERT 0 1" if self.run == 1 else "INSERT 0 0"


@pytest.mark.asyncio
async def test_relationship_reconcile_removes_stale_relink_and_is_idempotent():
    connection = _AppearanceReconcileConnection()
    repository = MatterRepository(cast(Any, _NoAcquirePool()))
    conn = cast(Connection, connection)
    appeared_at = datetime(2026, 2, 1)

    first = await repository.reconcile_meeting_appearances(
        meeting_id="meeting-1",
        appeared_at=appeared_at,
        committee="Council",
        committee_id="committee-1",
        conn=conn,
    )
    repeated = await repository.reconcile_meeting_appearances(
        meeting_id="meeting-1",
        appeared_at=appeared_at,
        committee="Council",
        committee_id="committee-1",
        conn=conn,
    )

    delete_query, delete_args = connection.calls[0]
    insert_query, insert_args = connection.calls[1]
    assert first == {"deleted": 1, "inserted": 1}
    assert repeated == {"deleted": 0, "inserted": 0}
    assert "i.matter_id = ma.matter_id" in delete_query
    assert "i.meeting_id = ma.meeting_id" in delete_query
    assert "ORDER BY i.matter_id, i.sequence, i.id" in insert_query
    assert "ON CONFLICT (matter_id, meeting_id, item_id) DO NOTHING" in insert_query
    assert delete_args == ("meeting-1",)
    assert insert_args == (
        "meeting-1",
        appeared_at,
        "Council",
        "committee-1",
    )


class _ProjectionRefreshConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1"


class _AuthoritativeTrackingConnection:
    def __init__(self, first_id, second_id):
        self.first_id = first_id
        self.second_id = second_id
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return [
            {
                "matter_id": self.first_id,
                "appearance_count": 0,
                "first_seen": None,
                "last_seen": None,
            },
            {
                "matter_id": self.second_id,
                "appearance_count": 2,
                "first_seen": datetime(2026, 1, 1),
                "last_seen": datetime(2026, 3, 1),
            },
        ]


@pytest.mark.asyncio
async def test_authoritative_tracking_is_exact_and_sorted_for_old_and_new():
    other_id = cast(
        str, generate_matter_id("alphaCA", matter_file="ORD-OTHER")
    )
    first_id, second_id = sorted({MATTER_ID, other_id})
    connection = _AuthoritativeTrackingConnection(first_id, second_id)
    repository = MatterRepository(cast(Any, _NoAcquirePool()))

    tracking = await repository.get_authoritative_tracking_for_matters(
        [second_id, first_id, second_id],
        conn=cast(Connection, connection),
    )

    query, args = connection.calls[0]
    assert "COUNT(i.id)::int AS appearance_count" in query
    assert "MIN(m.date) AS first_seen" in query
    assert "MAX(m.date) AS last_seen" in query
    assert "ORDER BY requested.matter_id" in query
    assert args == ([first_id, second_id],)
    assert tracking[first_id] == {
        "appearance_count": 0,
        "first_seen": None,
        "last_seen": None,
    }
    assert tracking[second_id] == {
        "appearance_count": 2,
        "first_seen": datetime(2026, 1, 1),
        "last_seen": datetime(2026, 3, 1),
    }


@pytest.mark.asyncio
async def test_zero_appearance_tracking_invalidates_the_stale_projection():
    connection = _ProjectionRefreshConnection()
    repository = MatterRepository(cast(Any, _NoAcquirePool()))

    await repository.refresh_matter_tracking(
        matter_id=MATTER_ID,
        attachments=[],
        appearance_count=0,
        first_seen=None,
        last_seen=None,
        sponsors=[],
        title="Stable identity title",
        attachment_hash=None,
        work_version=None,
        conn=cast(Connection, connection),
    )

    update_query, update_args = connection.calls[0]
    topic_query, topic_args = connection.calls[1]
    assert "WHEN $3::int = 0 THEN '{}'::jsonb" in update_query
    assert "canonical_summary = CASE WHEN $3::int = 0 THEN NULL" in update_query
    assert "canonical_topics = CASE WHEN $3::int = 0 THEN NULL" in update_query
    assert "WHEN $3::int = 0 THEN '[]'::jsonb ELSE $8::jsonb" in update_query
    assert "WHEN $3::int = 0 THEN title ELSE $9::text" in update_query
    assert update_args[2:5] == (0, None, None)
    assert topic_query == "DELETE FROM matter_topics WHERE matter_id = $1"
    assert topic_args == (MATTER_ID,)


@pytest.mark.asyncio
async def test_first_seen_vote_outcome_is_written_after_relationship_creation():
    events = []

    class Matters:
        async def reconcile_meeting_appearances(self, **kwargs):
            events.append(("reconciled", kwargs))
            return {"deleted": 0, "inserted": 1}

        async def update_appearance_outcome(self, **kwargs):
            assert events and events[0][0] == "reconciled"
            events.append(("outcome", kwargs))

    class CouncilMembers:
        async def reconcile_matter_sponsorships(self, **kwargs):
            assert events and events[0][0] == "reconciled"
            events.append(("sponsors", kwargs))

        async def reconcile_meeting_votes(self, **kwargs):
            assert events[-1][0] == "sponsors"
            events.append(("votes", kwargs))

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(
            matters=Matters(),
            council_members=CouncilMembers(),
        )
    )
    connection = cast(Connection, object())
    changes = await orchestrator._reconcile_matter_appearances(
        cast(Any, _meeting()),
        {
            "item-new": {
                "matter_id": MATTER_ID,
                "vote_outcome": "passed",
                "vote_tally": {"yes": 5, "no": 0},
            }
        },
        affected_matter_ids={MATTER_ID},
        observed_votes={
            MATTER_ID: [{"name": "Mayor Example", "vote": "yes"}]
        },
        conn=connection,
    )

    assert changes == {"deleted": 0, "inserted": 1}
    assert [event[0] for event in events] == [
        "reconciled",
        "sponsors",
        "votes",
        "outcome",
    ]
    assert events[2][1]["observed_votes"] == {
        MATTER_ID: [{"name": "Mayor Example", "vote": "yes"}]
    }
    assert events[3][1]["item_id"] == "item-new"
    assert events[3][1]["vote_outcome"] == "passed"
    assert events[3][1]["conn"] is connection


@pytest.mark.asyncio
async def test_relink_publication_refreshes_exact_old_new_tracking_and_versions():
    old_matter_id, new_matter_id = sorted(
        {
            cast(str, generate_matter_id("alphaCA", matter_file="ORD-A")),
            cast(str, generate_matter_id("alphaCA", matter_file="ORD-B")),
        }
    )
    meeting = _meeting()
    relinked = _item(
        "item-relinked",
        meeting.id,
        1,
        [_attachment("Ordinance", "https://example.gov/relinked.pdf")],
    )
    relinked.matter_id = new_matter_id
    relinked.sponsors = ["Councilmember Alpha", "councilmember alpha", "Mayor Beta"]
    existing = {
        matter_id: SimpleNamespace(
            title=f"Matter {matter_id}",
            metadata=MatterMetadata(attachment_hash="sv1:stale"),
            canonical_summary="stale summary",
            canonical_topics=["stale topic"],
        )
        for matter_id in (old_matter_id, new_matter_id)
    }
    refreshes = []
    publications = []
    published_keys = set()

    class Meetings:
        async def get_meeting(self, meeting_id, conn=None, *, lock_for_update=False):
            assert lock_for_update is True
            return meeting

    class Matters:
        async def get_matters_for_sync_snapshot(
            self,
            matter_ids,
            *,
            conn,
            include_unsummarized_orphans=False,
        ):
            assert matter_ids == [old_matter_id, new_matter_id]
            assert include_unsummarized_orphans is True
            return existing

        async def get_authoritative_tracking_for_matters(
            self, matter_ids, *, conn
        ):
            return {
                old_matter_id: {
                    "appearance_count": 0,
                    "first_seen": None,
                    "last_seen": None,
                },
                new_matter_id: {
                    "appearance_count": 1,
                    "first_seen": meeting.date,
                    "last_seen": meeting.date,
                },
            }

        async def refresh_matter_tracking(self, **kwargs):
            refreshes.append(kwargs)
            if kwargs["appearance_count"] == 0:
                orphan = existing[kwargs["matter_id"]]
                orphan.canonical_summary = None
                orphan.canonical_topics = None
                orphan.metadata = MatterMetadata()

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            return {old_matter_id: [], new_matter_id: [relinked]}

        async def get_agenda_items(
            self, meeting_id, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            return [relinked]

        async def copy_summary_from_prior_appearance(self, **_kwargs):
            raise AssertionError("relinked changed work must not copy")

    class Lifecycle:
        async def enqueue_queue_job(self, **kwargs):
            key = (kwargs["source_url"], kwargs["work_version"])
            if key not in published_keys:
                published_keys.add(key)
                publications.append(("enqueue", kwargs))

    class Queue:
        async def invalidate_desired_work(
            self, source_url, job_type, payload, **kwargs
        ):
            key = (source_url, kwargs["work_version"])
            if key in published_keys:
                return False
            published_keys.add(key)
            publications.append(
                (
                    "tombstone",
                    {
                        "source_url": source_url,
                        "job_type": job_type,
                        "payload": payload,
                        **kwargs,
                    },
                )
            )
            return True

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(
            meetings=Meetings(),
            matters=Matters(),
            items=Items(),
            pipeline_lifecycle=Lifecycle(),
            queue=Queue(),
        )
    )
    connection = cast(Connection, object())
    for _ in range(2):
        await orchestrator._publish_authoritative_work(
            meeting_id=meeting.id,
            affected_matter_ids={new_matter_id, old_matter_id},
            procedural_matter_ids=set(),
            conn=connection,
            publish_meeting=False,
        )

    empty_executable_version = matter_work_version([])
    empty_version = matter_no_work_version(
        empty_executable_version,
        "no_appearances",
    )
    new_version = matter_work_version([relinked])
    assert [(kind, details["source_url"]) for kind, details in publications] == [
        ("tombstone", f"matter://{old_matter_id}"),
        ("enqueue", f"matter://{new_matter_id}"),
    ]
    assert publications[0][1]["work_version"] == empty_version
    assert publications[1][1]["work_version"] == new_version
    assert publications[0][1]["payload"] == {
        "matter_id": old_matter_id,
        "no_work_reason": "no_appearances",
    }
    assert existing[old_matter_id].canonical_summary is None
    assert existing[old_matter_id].canonical_topics is None
    assert existing[old_matter_id].metadata == MatterMetadata()

    assert len(refreshes) == 4
    old_refreshes = [
        call for call in refreshes if call["matter_id"] == old_matter_id
    ]
    new_refreshes = [
        call for call in refreshes if call["matter_id"] == new_matter_id
    ]
    assert all(
        call["appearance_count"] == 0
        and call["first_seen"] is None
        and call["last_seen"] is None
        and call["attachments"] == []
        and call["sponsors"] == []
        for call in old_refreshes
    )
    assert all(
        call["appearance_count"] == 1
        and call["first_seen"] == meeting.date
        and call["last_seen"] == meeting.date
        and call["attachments"] == list(
            MatterWorkSnapshot.from_appearances([relinked]).attachments
        )
        and call["sponsors"] == ["Councilmember Alpha", "Mayor Beta"]
        and call["title"] == relinked.title
        for call in new_refreshes
    )


@pytest.mark.asyncio
async def test_no_work_policy_transitions_reopen_and_recur_in_order():
    meeting = _meeting()
    substantive = _item(
        "item-policy",
        meeting.id,
        1,
        [_attachment("Ordinance", "https://example.gov/policy.pdf")],
    )
    no_substantive = _item("item-policy", meeting.id, 1, [])
    state: dict[str, Any] = {
        "appearances": [substantive],
        "current_version": None,
    }
    actions = []
    existing = SimpleNamespace(
        title="Matter policy",
        metadata=MatterMetadata(attachment_hash="sv1:stale"),
        canonical_summary=None,
    )

    class Meetings:
        async def get_meeting(self, meeting_id, conn=None, *, lock_for_update=False):
            assert lock_for_update is True
            return meeting

    class Matters:
        async def get_matters_for_sync_snapshot(
            self,
            matter_ids,
            *,
            conn,
            include_unsummarized_orphans=False,
        ):
            assert include_unsummarized_orphans is True
            return {MATTER_ID: existing}

        async def get_authoritative_tracking_for_matters(
            self, matter_ids, *, conn
        ):
            count = len(state["appearances"])
            return {
                MATTER_ID: {
                    "appearance_count": count,
                    "first_seen": meeting.date if count else None,
                    "last_seen": meeting.date if count else None,
                }
            }

        async def refresh_matter_tracking(self, **_kwargs):
            return None

    class Items:
        async def get_all_items_for_matters(
            self, matter_ids, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            return {MATTER_ID: list(state["appearances"])}

        async def get_agenda_items(
            self, meeting_id, conn=None, *, lock_for_update=False
        ):
            assert lock_for_update is True
            return list(state["appearances"])

        async def copy_summary_from_prior_appearance(self, **_kwargs):
            raise AssertionError("policy transition must not copy")

    async def materialize(kind, source_url, work_version, payload):
        if state["current_version"] == work_version:
            return False
        state["current_version"] = work_version
        actions.append((kind, source_url, work_version, payload))
        return True

    class Lifecycle:
        async def enqueue_queue_job(self, **kwargs):
            return await materialize(
                "enqueue",
                kwargs["source_url"],
                kwargs["work_version"],
                kwargs["payload"],
            )

    class Queue:
        async def invalidate_desired_work(
            self, source_url, job_type, payload, **kwargs
        ):
            return await materialize(
                "tombstone",
                source_url,
                kwargs["work_version"],
                payload,
            )

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(
            meetings=Meetings(),
            matters=Matters(),
            items=Items(),
            pipeline_lifecycle=Lifecycle(),
            queue=Queue(),
        )
    )
    connection = cast(Connection, object())

    async def publish(*, procedural=False):
        await orchestrator._publish_authoritative_work(
            meeting_id=meeting.id,
            affected_matter_ids={MATTER_ID},
            procedural_matter_ids={MATTER_ID} if procedural else set(),
            conn=connection,
            publish_meeting=False,
        )

    await publish(procedural=True)
    await publish(procedural=True)  # identical policy is idempotent
    await publish()  # identical content becomes executable
    await publish(procedural=True)  # same procedural reason recurs after work
    state["appearances"] = []
    await publish()
    state["appearances"] = [no_substantive]
    await publish()
    await publish()  # identical no-substantive policy is idempotent
    state["appearances"] = []
    await publish()  # prior no-appearance reason recurs after another reason

    substantive_version = matter_work_version([substantive])
    empty_executable = matter_work_version([])
    no_substantive_executable = matter_work_version([no_substantive])
    expected = [
        (
            "tombstone",
            matter_no_work_version(substantive_version, "procedural"),
            "procedural",
        ),
        ("enqueue", substantive_version, None),
        (
            "tombstone",
            matter_no_work_version(substantive_version, "procedural"),
            "procedural",
        ),
        (
            "tombstone",
            matter_no_work_version(empty_executable, "no_appearances"),
            "no_appearances",
        ),
        (
            "tombstone",
            matter_no_work_version(
                no_substantive_executable,
                "no_substantive_work",
            ),
            "no_substantive_work",
        ),
        (
            "tombstone",
            matter_no_work_version(empty_executable, "no_appearances"),
            "no_appearances",
        ),
    ]
    assert [
        (kind, version, payload.get("no_work_reason"))
        for kind, _source, version, payload in actions
    ] == expected
