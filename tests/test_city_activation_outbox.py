"""Contracts for atomic, durable first-city activation delivery."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncpg import Connection

from database.repositories_async.userland import UserlandRepository
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.outbox_dispatch import (
    CityActivationNotification,
    dispatch_outbox_event,
)


class _NoAcquirePool:
    def acquire(self):
        raise AssertionError("operation escaped its transaction connection")


class _Connection:
    def __init__(self, *, exists: bool = False, rows=None):
        self.exists = exists
        self.rows = rows or []
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", " ".join(query.split()), args))
        return "UPDATE 1"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", " ".join(query.split()), args))
        return self.exists

    async def fetch(self, query, *args):
        self.calls.append(("fetch", " ".join(query.split()), args))
        return self.rows


@pytest.mark.asyncio
async def test_city_activation_transition_is_advisory_locked_before_read():
    connection = _Connection(exists=False)

    claimed = await MeetingSyncOrchestrator._claim_city_activation(
        "alphaCA",
        cast(Connection, connection),
    )

    assert claimed is True
    assert [call[0] for call in connection.calls] == ["execute", "fetchval"]
    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    assert "SELECT EXISTS" in connection.calls[1][1]


@pytest.mark.asyncio
async def test_activation_recipient_lookup_reuses_unit_of_work_connection():
    connection = _Connection(
        rows=[{"user_id": "u1", "email": "u@example.test", "name": "Uma"}]
    )
    repository = UserlandRepository(cast(Any, _NoAcquirePool()))

    recipients = await repository.get_city_activation_recipients(
        "alphaCA",
        conn=cast(Connection, connection),
    )

    assert recipients == [
        {"user_id": "u1", "email": "u@example.test", "name": "Uma"}
    ]
    assert "JOIN userland.users" in connection.calls[0][1]


@pytest.mark.asyncio
async def test_first_meeting_records_one_durable_event_per_user():
    connection = cast(Connection, object())
    outbox_calls = []
    request_calls = []

    class Userland:
        async def get_city_activation_recipients(self, banana, *, conn):
            assert banana == "alphaCA"
            assert conn is connection
            return [
                {"user_id": "u1", "email": "u@example.test", "name": "Uma"}
            ]

        async def update_city_request_status(self, **kwargs):
            request_calls.append(kwargs)

    class Lifecycle:
        async def enqueue_outbox(self, **kwargs):
            outbox_calls.append(kwargs)

    orchestrator = MeetingSyncOrchestrator(
        SimpleNamespace(userland=Userland(), pipeline_lifecycle=Lifecycle())
    )
    city = SimpleNamespace(
        banana="alphaCA",
        name="Alpha",
        state="CA",
    )

    count = await orchestrator._enqueue_city_activation(
        cast(Any, city),
        connection,
    )

    assert count == 1
    assert outbox_calls[0]["event_key"] == (
        "notification.city_activated:alphaCA:u1"
    )
    assert outbox_calls[0]["event_type"] == "notification.city_activated"
    assert outbox_calls[0]["aggregate_id"] == "alphaCA:u1"
    assert outbox_calls[0]["conn"] is connection
    assert request_calls[0]["conn"] is connection


@pytest.mark.asyncio
async def test_typed_dispatch_retries_false_email_result(monkeypatch):
    calls = []

    async def fail_send(**kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(
        "userland.email.transactional.send_city_available_email",
        fail_send,
    )
    payload = {
        "banana": "alphaCA",
        "city_name": "Alpha",
        "state": "CA",
        "user_id": "u1",
        "email": "u@example.test",
        "user_name": "Uma",
    }

    with pytest.raises(RuntimeError, match="delivery failed"):
        await CityActivationNotification.from_payload(payload).publish()

    assert calls[0]["banana"] == "alphaCA"


@pytest.mark.asyncio
async def test_typed_dispatch_preserves_queue_contract():
    calls = []

    class Queue:
        async def enqueue_job(self, **kwargs):
            calls.append(kwargs)

    payload = {
        "source_url": "meeting://m1",
        "job_type": "meeting",
        "payload": {"meeting_id": "m1"},
    }
    await dispatch_outbox_event(
        SimpleNamespace(queue=Queue()),
        {
            "event_type": "queue.enqueue",
            "payload": payload,
            "work_generation": 17,
        },
    )

    assert calls == [{**payload, "desired_generation": 17}]


@pytest.mark.asyncio
async def test_queue_dispatch_rejects_missing_or_invalid_work_generation():
    class Queue:
        async def enqueue_job(self, **_kwargs):
            raise AssertionError("invalid intent must not reach the queue")

    event = {
        "event_type": "queue.enqueue",
        "payload": {
            "source_url": "meeting://m1",
            "job_type": "meeting",
            "payload": {"meeting_id": "m1"},
        },
    }

    with pytest.raises(ValueError, match="work_generation"):
        await dispatch_outbox_event(SimpleNamespace(queue=Queue()), event)

    with pytest.raises(ValueError, match="work_generation"):
        await dispatch_outbox_event(
            SimpleNamespace(queue=Queue()),
            {**event, "work_generation": True},
        )
