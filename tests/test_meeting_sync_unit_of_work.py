"""Contracts for meeting-sync unit-of-work and deterministic work versions."""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncpg import Connection

from database.id_generation import generate_matter_id
from database.models import AttachmentInfo, MatterMetadata
from database.repositories_async.items import ItemRepository
from database.repositories_async.matters import MatterRepository
from database.repositories_async.pipeline_lifecycle import PipelineLifecycleRepository
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.utils import (
    aggregate_matter_attachments,
    hash_substantive_attachments,
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
    assert matter_work_version([second, first]) == expected

    rotated = _item(
        "item-1",
        "meeting-1",
        1,
        [_attachment("Ordinance", "https://blob.example.gov/ord.pdf?sig=rotated")],
    )
    assert matter_work_version([rotated, second]) == expected

    changed = _item(
        "item-3",
        "meeting-3",
        1,
        [_attachment("Amendment", "https://example.gov/amendment.pdf")],
    )
    assert matter_work_version([first, second, changed]) != expected


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


@pytest.mark.asyncio
async def test_assigned_matter_reads_reuse_the_unit_of_work_connection():
    connection = _ReadConnection()
    matter_repository = MatterRepository(cast(Any, _NoAcquirePool()))
    item_repository = ItemRepository(cast(Any, _NoAcquirePool()))
    conn = cast(Connection, connection)

    assert await matter_repository.get_matter(MATTER_ID, conn=conn) is None
    assert await matter_repository.has_appearance(
        MATTER_ID, "meeting-1", conn=conn
    )
    assert await item_repository.get_all_items_for_matter(
        MATTER_ID, conn=conn
    ) == []

    assert [call[0] for call in connection.calls] == [
        "fetchrow",
        "fetchval",
        "fetch",
    ]


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
        self.records.append(("outbox", args))
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
async def test_outbox_claim_preserves_version_order_per_aggregate():
    connection = _AtomicConnection()
    repository = PipelineLifecycleRepository(
        cast(Any, _SingleConnectionPool(connection))
    )

    assert await repository.claim_outbox() is None

    assert connection.fetch_query is not None
    assert "NOT EXISTS" in connection.fetch_query
    assert "earlier.aggregate_id = po.aggregate_id" in connection.fetch_query
    assert "earlier.id < po.id" in connection.fetch_query
    assert "earlier.status <> 'published'" in connection.fetch_query


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

    await orchestrator._enqueue_if_needed(
        cast(Any, meeting),
        meeting.date,
        cast(Any, items),
        conn=connection,
    )

    event = lifecycle.calls[0]
    assert event["work_version"] == meeting_work_version(meeting, items)
    assert event["work_version"].startswith("mv1:")
    assert event["conn"] is connection


@pytest.mark.asyncio
async def test_sync_matter_decision_uses_aggregate_version_and_same_connection():
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
        async def get_matter(self, matter_id, conn=None):
            calls.append(("get_matter", conn))
            return existing

        async def has_appearance(self, matter_id, meeting_id, conn=None):
            calls.append(("has_appearance", conn))
            return False

        async def update_matter_tracking(self, **kwargs):
            calls.append(("update_matter_tracking", kwargs["conn"]))

    class Items:
        async def get_all_items_for_matter(self, matter_id, conn=None):
            calls.append(("get_items", conn))
            return [prior]

    database = SimpleNamespace(
        matters=Matters(),
        items=Items(),
        council_members=SimpleNamespace(),
    )
    orchestrator = MeetingSyncOrchestrator(database)
    meeting = _meeting(id="meeting-new")
    expected = matter_work_version([prior, current])

    stats = await orchestrator._track_matters(
        cast(Any, meeting),
        [{"sequence": 1, "matter_type": "Ordinance", "votes": []}],
        cast(Any, [current]),
        conn=connection,
    )

    assert stats["pending_jobs"][0]["attachment_hash"] == expected
    assert all(call[1] is connection for call in calls)
