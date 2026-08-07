"""Executable PostgreSQL concurrency contracts for desired-work generations.

Set ``ENGAGIC_TEST_DATABASE_URL`` to an isolated disposable database. These
tests create and remove their own schema; they must never target production.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, cast

import asyncpg
import pytest

from database.id_generation import generate_matter_id
from database.repositories_async.pipeline_lifecycle import (
    PipelineLifecycleRepository,
)
from database.repositories_async.items import ItemRepository
from database.repositories_async.matters import MatterRepository
from database.repositories_async.queue import QueueRepository
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.outbox_dispatch import dispatch_outbox_event
from pipeline.utils import matter_no_work_version


TEST_DSN = os.getenv("ENGAGIC_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ENGAGIC_TEST_DATABASE_URL must name an isolated disposable database",
)


SCHEMA_SQL = """
CREATE SEQUENCE pipeline_work_generation_seq;

CREATE TABLE queue (
    id BIGSERIAL PRIMARY KEY,
    source_url TEXT NOT NULL UNIQUE,
    meeting_id TEXT,
    banana TEXT,
    job_type TEXT,
    payload JSONB,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    failed_at TIMESTAMP,
    error_message TEXT,
    processing_metadata JSONB,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_enqueued_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retry_at TIMESTAMP,
    ready_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    work_version TEXT,
    claim_token UUID,
    claimed_at TIMESTAMP,
    heartbeat_at TIMESTAMP,
    desired_generation BIGINT NOT NULL DEFAULT
        nextval('pipeline_work_generation_seq')
);

CREATE TABLE pipeline_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    claimed_at TIMESTAMP,
    lease_owner TEXT,
    lease_expires_at TIMESTAMP,
    claim_token UUID,
    work_generation BIGINT NOT NULL DEFAULT
        nextval('pipeline_work_generation_seq'),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP
);

CREATE TABLE meetings (
    id TEXT PRIMARY KEY,
    date TIMESTAMP
);

CREATE TABLE city_matters (
    id TEXT PRIMARY KEY,
    banana TEXT NOT NULL,
    matter_id TEXT,
    matter_file TEXT,
    matter_type TEXT,
    title TEXT NOT NULL,
    sponsors JSONB,
    canonical_summary TEXT,
    canonical_topics JSONB,
    attachments JSONB,
    metadata JSONB,
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    appearance_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    final_vote_date TIMESTAMP,
    quality_score REAL,
    rating_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE matter_topics (
    matter_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    PRIMARY KEY (matter_id, topic)
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    title TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    attachments JSONB,
    attachment_hash TEXT,
    body_text TEXT,
    matter_id TEXT,
    matter_file TEXT,
    matter_type TEXT,
    agenda_number TEXT,
    sponsors JSONB,
    summary TEXT,
    topics JSONB,
    quality_score REAL,
    rating_count INTEGER NOT NULL DEFAULT 0,
    filter_reason TEXT
);

CREATE TABLE item_topics (
    item_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    PRIMARY KEY (item_id, topic)
);

CREATE TABLE matter_appearances (
    id BIGSERIAL PRIMARY KEY,
    matter_id TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    appeared_at TIMESTAMP,
    committee TEXT,
    committee_id TEXT,
    sequence INTEGER,
    UNIQUE (matter_id, meeting_id, item_id)
);
"""


@asynccontextmanager
async def _isolated_schema() -> AsyncIterator[tuple[str, str]]:
    if TEST_DSN is None:  # pragma: no cover - guarded by pytestmark
        raise RuntimeError("isolated test database is not configured")
    schema = f"work_generation_{uuid.uuid4().hex}"
    setup = await asyncpg.connect(TEST_DSN)
    try:
        await setup.execute(f'CREATE SCHEMA "{schema}"')
        await setup.execute(f'SET search_path TO "{schema}"')
        await setup.execute(SCHEMA_SQL)
    finally:
        await setup.close()
    try:
        yield TEST_DSN, schema
    finally:
        cleanup = await asyncpg.connect(TEST_DSN)
        try:
            await cleanup.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await cleanup.close()


async def _connect(dsn: str, schema: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn, server_settings={"search_path": schema})
    await conn.set_type_codec(
        "jsonb",
        schema="pg_catalog",
        encoder=json.dumps,
        decoder=json.loads,
    )
    return conn


async def _enqueue(
    conn: asyncpg.Connection,
    *,
    version: str,
    generation: int,
) -> None:
    repository = QueueRepository(cast(Any, None))
    await repository.enqueue_job(
        source_url="meeting://m1",
        job_type="meeting",
        payload={"meeting_id": "m1"},
        work_version=version,
        desired_generation=generation,
        conn=conn,
    )


@pytest.mark.asyncio
async def test_no_work_tombstone_fences_claims_and_generation_order() -> None:
    async with _isolated_schema() as (dsn, schema):
        conn = await _connect(dsn, schema)
        repository = QueueRepository(cast(Any, None))
        empty_version = "mv1:empty"

        # A tombstone is material desired state even when no queue row exists.
        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version=empty_version,
                desired_generation=10,
                conn=conn,
            )

        inserted = await conn.fetchrow(
            """
            SELECT id, status, work_version, desired_generation, claim_token
            FROM queue WHERE source_url = 'meeting://m1'
            """
        )
        assert inserted is not None
        assert dict(inserted) == {
            "id": inserted["id"],
            "status": "completed",
            "work_version": empty_version,
            "desired_generation": 10,
            "claim_token": None,
        }

        # Same terminal version is idempotent and does not churn generation.
        before_noop = await conn.fetchrow(
            """
            SELECT desired_generation, processing_metadata, last_enqueued_at
            FROM queue WHERE source_url = 'meeting://m1'
            """
        )
        assert before_noop is not None
        async with conn.transaction():
            assert not await repository.invalidate_desired_work(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version=empty_version,
                desired_generation=20,
                conn=conn,
            )
        after_noop = await conn.fetchrow(
            """
            SELECT desired_generation, processing_metadata, last_enqueued_at
            FROM queue WHERE source_url = 'meeting://m1'
            """
        )
        assert after_noop is not None
        assert dict(after_noop) == dict(before_noop)
        assert after_noop["desired_generation"] == 10

        # An intervening real-work intent makes empty -> real -> empty a new
        # recurrence even though the materialized queue descriptor is still
        # the same tombstone.
        intervening_event = {
            "event_type": "queue.enqueue",
            "work_generation": 25,
            "payload": {
                "source_url": "meeting://m1",
                "job_type": "meeting",
                "payload": {"meeting_id": "m1"},
                "work_version": "mv1:intervening",
            },
        }
        await conn.execute(
            """
            INSERT INTO pipeline_outbox (
                event_key, event_type, aggregate_type, aggregate_id, payload,
                work_generation
            )
            VALUES (
                'queue.enqueue:meeting://m1:mv1:intervening',
                'queue.enqueue', 'meeting', 'm1', $1, 25
            )
            """,
            intervening_event["payload"],
        )
        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version=empty_version,
                desired_generation=26,
                conn=conn,
            )
        assert await conn.fetchval(
            "SELECT desired_generation FROM queue WHERE source_url = 'meeting://m1'"
        ) == 26

        class BoundQueue:
            async def enqueue_job(self, **kwargs):
                return await repository.enqueue_job(**kwargs, conn=conn)

        await dispatch_outbox_event(
            SimpleNamespace(queue=BoundQueue()), intervening_event
        )
        assert await conn.fetchval(
            "SELECT work_version FROM queue WHERE source_url = 'meeting://m1'"
        ) == empty_version

        # A later distinct real-work generation reopens the source normally.
        async with conn.transaction():
            assert await repository.enqueue_job(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version="mv1:real",
                desired_generation=30,
                conn=conn,
            )
        claim_token = "00000000-0000-0000-0000-000000000031"
        await conn.execute(
            """
            UPDATE queue
            SET status = 'processing', claim_token = $1::uuid,
                claimed_at = NOW(), heartbeat_at = NOW(), retry_count = 2,
                retry_at = NOW(), error_message = 'old failure',
                processing_metadata = '{"sticky": true}'::jsonb,
                last_enqueued_at = TIMESTAMP '2000-01-01'
            WHERE source_url = 'meeting://m1'
            """,
            claim_token,
        )

        # This older outbox publication may race delivery after invalidation.
        stale_event = {
            "event_type": "queue.enqueue",
            "work_generation": 35,
            "payload": {
                "source_url": "meeting://m1",
                "job_type": "meeting",
                "payload": {"meeting_id": "m1"},
                "work_version": "mv1:stale",
            },
        }
        await conn.execute(
            """
            INSERT INTO pipeline_outbox (
                event_key, event_type, aggregate_type, aggregate_id, payload,
                work_generation
            )
            VALUES (
                'queue.enqueue:meeting://m1:mv1:stale',
                'queue.enqueue', 'meeting', 'm1', $1, 35
            )
            """,
            stale_event["payload"],
        )

        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version=empty_version,
                desired_generation=40,
                conn=conn,
            )

        terminal = await conn.fetchrow(
            """
            SELECT id, status, work_version, desired_generation, retry_count,
                   retry_at, error_message, claim_token, claimed_at, heartbeat_at,
                   processing_metadata, last_enqueued_at
            FROM queue WHERE source_url = 'meeting://m1'
            """
        )
        assert terminal is not None
        assert terminal["status"] == "completed"
        assert terminal["work_version"] == empty_version
        assert terminal["desired_generation"] == 40
        assert terminal["retry_count"] == 0
        assert terminal["processing_metadata"] == {"sticky": True}
        assert terminal["last_enqueued_at"].year > 2000
        assert all(
            terminal[field] is None
            for field in (
                "retry_at",
                "error_message",
                "claim_token",
                "claimed_at",
                "heartbeat_at",
            )
        )

        # The invalidated claim no longer owns the row.
        stale_completion = await conn.fetchrow(
            """
            UPDATE queue SET status = 'completed'
            WHERE id = $1 AND status = 'processing'
              AND claim_token = $2::uuid
              AND work_version IS NOT DISTINCT FROM 'mv1:real'
            RETURNING id
            """,
            terminal["id"],
            claim_token,
        )
        assert stale_completion is None

        await dispatch_outbox_event(SimpleNamespace(queue=BoundQueue()), stale_event)
        assert await conn.fetchval(
            "SELECT work_version FROM queue WHERE source_url = 'meeting://m1'"
        ) == empty_version

        async with conn.transaction():
            assert await repository.enqueue_job(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version="mv1:later",
                desired_generation=50,
                conn=conn,
            )
        reopened = await conn.fetchrow(
            """
            SELECT status, work_version, desired_generation
            FROM queue WHERE source_url = 'meeting://m1'
            """
        )
        assert reopened is not None
        assert dict(reopened) == {
            "status": "pending",
            "work_version": "mv1:later",
            "desired_generation": 50,
        }
        await conn.close()


@pytest.mark.asyncio
async def test_matter_policy_tombstones_reopen_and_recur_by_version() -> None:
    async with _isolated_schema() as (dsn, schema):
        conn = await _connect(dsn, schema)
        repository = QueueRepository(cast(Any, None))
        source_url = "matter://alphaCA_policy"
        executable = "mw1:identical-content"
        procedural = matter_no_work_version(executable, "procedural")
        no_substantive = matter_no_work_version(
            executable,
            "no_substantive_work",
        )
        payload = {"matter_id": "alphaCA_policy"}

        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url=source_url,
                job_type="matter",
                payload={**payload, "no_work_reason": "procedural"},
                work_version=procedural,
                desired_generation=10,
                conn=conn,
            )
        async with conn.transaction():
            assert not await repository.invalidate_desired_work(
                source_url=source_url,
                job_type="matter",
                payload={**payload, "no_work_reason": "procedural"},
                work_version=procedural,
                desired_generation=20,
                conn=conn,
            )
        assert await conn.fetchval(
            "SELECT desired_generation FROM queue WHERE source_url = $1",
            source_url,
        ) == 10

        # Procedural -> substantive reopens identical content because the
        # executable descriptor is materially distinct from its tombstone.
        async with conn.transaction():
            assert await repository.enqueue_job(
                source_url=source_url,
                job_type="matter",
                payload=payload,
                work_version=executable,
                desired_generation=30,
                conn=conn,
            )
        await conn.execute(
            """
            UPDATE queue
            SET status = 'processing',
                claim_token = '00000000-0000-0000-0000-000000000031'::uuid,
                claimed_at = NOW(), heartbeat_at = NOW()
            WHERE source_url = $1
            """,
            source_url,
        )

        # Substantive -> procedural fences the active executable claim.
        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url=source_url,
                job_type="matter",
                payload={**payload, "no_work_reason": "procedural"},
                work_version=procedural,
                desired_generation=40,
                conn=conn,
            )
        fenced = await conn.fetchrow(
            """
            SELECT status, work_version, desired_generation, claim_token
            FROM queue WHERE source_url = $1
            """,
            source_url,
        )
        assert fenced is not None
        assert dict(fenced) == {
            "status": "completed",
            "work_version": procedural,
            "desired_generation": 40,
            "claim_token": None,
        }

        # A distinct no-work reason advances, and the prior reason can recur
        # later because each material policy state has its own descriptor.
        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url=source_url,
                job_type="matter",
                payload={**payload, "no_work_reason": "no_substantive_work"},
                work_version=no_substantive,
                desired_generation=50,
                conn=conn,
            )
        async with conn.transaction():
            assert await repository.invalidate_desired_work(
                source_url=source_url,
                job_type="matter",
                payload={**payload, "no_work_reason": "procedural"},
                work_version=procedural,
                desired_generation=60,
                conn=conn,
            )
        final = await conn.fetchrow(
            """
            SELECT status, work_version, desired_generation
            FROM queue WHERE source_url = $1
            """,
            source_url,
        )
        assert final is not None
        assert dict(final) == {
            "status": "completed",
            "work_version": procedural,
            "desired_generation": 60,
        }
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tombstone_starts_first", [True, False])
@pytest.mark.parametrize("tombstone_is_newer", [True, False])
async def test_tombstone_and_enqueue_share_source_serialization(
    tombstone_starts_first: bool,
    tombstone_is_newer: bool,
) -> None:
    async with _isolated_schema() as (dsn, schema):
        first = await _connect(dsn, schema)
        second = await _connect(dsn, schema)
        repository = QueueRepository(cast(Any, None))
        first_written = asyncio.Event()
        release_first = asyncio.Event()

        tombstone_generation = 20 if tombstone_is_newer else 10
        enqueue_generation = 10 if tombstone_is_newer else 20

        async def tombstone(conn: asyncpg.Connection) -> None:
            await repository.invalidate_desired_work(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version="mv1:empty",
                desired_generation=tombstone_generation,
                conn=conn,
            )

        async def enqueue(conn: asyncpg.Connection) -> None:
            await repository.enqueue_job(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version="mv1:real",
                desired_generation=enqueue_generation,
                conn=conn,
            )

        first_write = tombstone if tombstone_starts_first else enqueue
        second_write = enqueue if tombstone_starts_first else tombstone

        async def write_first() -> None:
            async with first.transaction():
                await first_write(first)
                first_written.set()
                await release_first.wait()

        async def write_second() -> None:
            await first_written.wait()
            async with second.transaction():
                await second_write(second)

        first_task = asyncio.create_task(write_first())
        second_task = asyncio.create_task(write_second())
        try:
            await asyncio.wait_for(first_written.wait(), timeout=5)
            await asyncio.sleep(0.05)
            assert not second_task.done()
            release_first.set()
            await asyncio.wait_for(
                asyncio.gather(first_task, second_task),
                timeout=5,
            )

            row = await first.fetchrow(
                """
                SELECT status, work_version, desired_generation
                FROM queue WHERE source_url = 'meeting://m1'
                """
            )
            assert row is not None
            assert dict(row) == (
                {
                    "status": "completed",
                    "work_version": "mv1:empty",
                    "desired_generation": 20,
                }
                if tombstone_is_newer
                else {
                    "status": "pending",
                    "work_version": "mv1:real",
                    "desired_generation": 20,
                }
            )
        finally:
            release_first.set()
            if not first_task.done():
                first_task.cancel()
            if not second_task.done():
                second_task.cancel()
            await asyncio.gather(first_task, second_task, return_exceptions=True)
            await first.close()
            await second.close()


@pytest.mark.asyncio
async def test_opposite_matter_relinks_serialize_without_deadlock() -> None:
    """A -> B and B -> A take the same aggregate union before item rows."""
    async with _isolated_schema() as (dsn, schema):
        setup = await _connect(dsn, schema)
        first = await _connect(dsn, schema)
        second = await _connect(dsn, schema)
        matters = MatterRepository(cast(Any, None))
        items = ItemRepository(cast(Any, None))
        orchestrator = MeetingSyncOrchestrator(
            SimpleNamespace(matters=matters, items=items)
        )
        matter_a = cast(
            str, generate_matter_id("alphaCA", matter_file="ORD-A")
        )
        matter_b = cast(
            str, generate_matter_id("alphaCA", matter_file="ORD-B")
        )

        await setup.executemany(
            "INSERT INTO meetings (id, date) VALUES ($1, TIMESTAMP '2026-08-07')",
            [("meeting-a",), ("meeting-b",)],
        )
        await setup.executemany(
            """
            INSERT INTO city_matters (
                id, banana, title, sponsors, attachments, metadata,
                appearance_count
            )
            VALUES ($1, 'alphaCA', $1, '[]'::jsonb, '[]'::jsonb,
                    '{}'::jsonb, 1)
            """,
            [(matter_a,), (matter_b,)],
        )
        await setup.executemany(
            """
            INSERT INTO items (
                id, meeting_id, title, sequence, attachments, matter_id,
                sponsors, topics
            )
            VALUES ($1, $2, $1, 1, '[]'::jsonb, $3,
                    '[]'::jsonb, '[]'::jsonb)
            """,
            [
                ("item-a", "meeting-a", matter_a),
                ("item-b", "meeting-b", matter_b),
            ],
        )
        await setup.executemany(
            """
            INSERT INTO matter_appearances (
                matter_id, meeting_id, item_id, appeared_at, sequence
            )
            VALUES ($1, $2, $3, TIMESTAMP '2026-08-07', 1)
            """,
            [
                (matter_a, "meeting-a", "item-a"),
                (matter_b, "meeting-b", "item-b"),
            ],
        )

        first_locked = asyncio.Event()
        release_first = asyncio.Event()
        second_reached_union = asyncio.Event()

        async def relink(
            conn: asyncpg.Connection,
            *,
            meeting_id: str,
            item_id: str,
            target_matter_id: str,
            hold: bool,
        ) -> None:
            async with conn.transaction():
                await conn.fetchrow(
                    "SELECT id FROM meetings WHERE id = $1 FOR UPDATE",
                    meeting_id,
                )
                old_links = await items.get_item_matter_links(
                    meeting_id,
                    conn=conn,
                )
                affected = orchestrator._affected_matter_ids(
                    old_links,
                    cast(Any, [SimpleNamespace(matter_id=target_matter_id)]),
                )
                if not hold:
                    second_reached_union.set()
                await orchestrator._load_matter_sync_snapshot(affected, conn)
                if hold:
                    first_locked.set()
                    await release_first.wait()
                await conn.execute(
                    "UPDATE items SET matter_id = $2 WHERE id = $1",
                    item_id,
                    target_matter_id,
                )
                await matters.reconcile_meeting_appearances(
                    meeting_id=meeting_id,
                    appeared_at=None,
                    committee=None,
                    committee_id=None,
                    conn=conn,
                )

        first_task = asyncio.create_task(
            relink(
                first,
                meeting_id="meeting-a",
                item_id="item-a",
                target_matter_id=matter_b,
                hold=True,
            )
        )
        second_task: asyncio.Task[None] | None = None
        first_lock_marker = asyncio.create_task(first_locked.wait())
        try:
            done, _ = await asyncio.wait(
                {first_task, first_lock_marker},
                timeout=5,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if first_task in done:
                await first_task
            assert first_lock_marker in done, "first relink did not acquire matter union"
            second_task = asyncio.create_task(
                relink(
                    second,
                    meeting_id="meeting-b",
                    item_id="item-b",
                    target_matter_id=matter_a,
                    hold=False,
                )
            )
            await asyncio.wait_for(second_reached_union.wait(), timeout=5)
            await asyncio.sleep(0.05)
            assert not second_task.done()
            release_first.set()
            await asyncio.wait_for(
                asyncio.gather(first_task, second_task),
                timeout=5,
            )

            retained_items = await setup.fetch(
                "SELECT id, matter_id FROM items ORDER BY id"
            )
            retained_appearances = await setup.fetch(
                """
                SELECT item_id, meeting_id, matter_id
                FROM matter_appearances
                ORDER BY item_id
                """
            )
            assert [dict(row) for row in retained_items] == [
                {"id": "item-a", "matter_id": matter_b},
                {"id": "item-b", "matter_id": matter_a},
            ]
            assert [dict(row) for row in retained_appearances] == [
                {
                    "item_id": "item-a",
                    "meeting_id": "meeting-a",
                    "matter_id": matter_b,
                },
                {
                    "item_id": "item-b",
                    "meeting_id": "meeting-b",
                    "matter_id": matter_a,
                },
            ]
        finally:
            release_first.set()
            if not first_task.done():
                first_task.cancel()
            if second_task is not None and not second_task.done():
                second_task.cancel()
            if not first_lock_marker.done():
                first_lock_marker.cancel()
            await asyncio.gather(
                first_task,
                first_lock_marker,
                *([second_task] if second_task is not None else []),
                return_exceptions=True,
            )
            await setup.close()
            await first.close()
            await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_starts_first", [True, False])
async def test_newer_generation_wins_both_transaction_wait_orders(
    newer_starts_first: bool,
) -> None:
    async with _isolated_schema() as (dsn, schema):
        first = await _connect(dsn, schema)
        second = await _connect(dsn, schema)
        first_written = asyncio.Event()
        release_first = asyncio.Event()

        first_version, first_generation = (
            ("mv1:new", 20) if newer_starts_first else ("mv1:old", 10)
        )
        second_version, second_generation = (
            ("mv1:old", 10) if newer_starts_first else ("mv1:new", 20)
        )

        async def write_first() -> None:
            async with first.transaction():
                await _enqueue(
                    first,
                    version=first_version,
                    generation=first_generation,
                )
                first_written.set()
                await release_first.wait()

        async def write_second() -> None:
            await first_written.wait()
            async with second.transaction():
                await _enqueue(
                    second,
                    version=second_version,
                    generation=second_generation,
                )

        first_task = asyncio.create_task(write_first())
        second_task = asyncio.create_task(write_second())
        started = asyncio.create_task(first_written.wait())
        done, _ = await asyncio.wait(
            {first_task, started},
            timeout=5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if first_task in done:
            await first_task
        assert started in done
        await asyncio.sleep(0.05)
        assert not second_task.done()
        release_first.set()
        await asyncio.gather(first_task, second_task)

        row = await first.fetchrow(
            "SELECT work_version, desired_generation FROM queue"
        )
        assert row is not None
        assert dict(row) == {
            "work_version": "mv1:new",
            "desired_generation": 20,
        }
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_dispatcher_and_reconciler_share_advisory_before_row_order() -> None:
    async with _isolated_schema() as (dsn, schema):
        setup = await _connect(dsn, schema)
        dispatcher_conn = await _connect(dsn, schema)
        reconciler_conn = await _connect(dsn, schema)
        repository = QueueRepository(cast(Any, None))
        dispatcher_has_source = asyncio.Event()
        release_dispatcher = asyncio.Event()
        reconciler_has_row = asyncio.Event()

        await _enqueue(setup, version="mv1:seed", generation=10)

        class BoundDispatcherQueue:
            async def enqueue_job(self, **kwargs):
                return await repository.enqueue_job(
                    **kwargs,
                    conn=dispatcher_conn,
                )

        event = {
            "event_type": "queue.enqueue",
            "work_generation": 20,
            "payload": {
                "source_url": "meeting://m1",
                "job_type": "meeting",
                "payload": {"meeting_id": "m1"},
                "work_version": "mv1:dispatched",
            },
        }

        async def dispatch() -> None:
            async with dispatcher_conn.transaction():
                await dispatcher_conn.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended('queue-intent:' || $1, 0)
                    )
                    """,
                    "meeting://m1",
                )
                dispatcher_has_source.set()
                await release_dispatcher.wait()
                await dispatch_outbox_event(
                    SimpleNamespace(queue=BoundDispatcherQueue()),
                    event,
                )

        async def reconcile() -> None:
            await dispatcher_has_source.wait()
            async with reconciler_conn.transaction():
                state = await repository.lock_desired_state(
                    "meeting://m1",
                    conn=reconciler_conn,
                )
                reconciler_has_row.set()
                assert state is not None
                assert state["work_version"] == "mv1:dispatched"
                await repository.enqueue_job(
                    source_url="meeting://m1",
                    job_type="meeting",
                    payload={"meeting_id": "m1"},
                    work_version="mv1:reconciled",
                    desired_generation=30,
                    conn=reconciler_conn,
                )

        dispatcher_task = asyncio.create_task(dispatch())
        reconciler_task = asyncio.create_task(reconcile())
        try:
            await asyncio.wait_for(dispatcher_has_source.wait(), timeout=5)
            await asyncio.sleep(0.05)
            assert not reconciler_has_row.is_set()
            release_dispatcher.set()
            await asyncio.wait_for(
                asyncio.gather(dispatcher_task, reconciler_task),
                timeout=5,
            )

            row = await setup.fetchrow(
                "SELECT work_version, desired_generation FROM queue"
            )
            assert row is not None
            assert dict(row) == {
                "work_version": "mv1:reconciled",
                "desired_generation": 30,
            }
        finally:
            release_dispatcher.set()
            if not dispatcher_task.done():
                dispatcher_task.cancel()
            if not reconciler_task.done():
                reconciler_task.cancel()
            await asyncio.gather(
                dispatcher_task,
                reconciler_task,
                return_exceptions=True,
            )
            await setup.close()
            await dispatcher_conn.close()
            await reconciler_conn.close()


@pytest.mark.asyncio
async def test_version_slot_rearms_for_a_b_a_recurrence() -> None:
    async with _isolated_schema() as (dsn, schema):
        conn = await _connect(dsn, schema)
        lifecycle = PipelineLifecycleRepository(cast(Any, None))
        queue = QueueRepository(cast(Any, None))

        async def publish_intent(version: str) -> int:
            await lifecycle.enqueue_queue_job(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                aggregate_id="m1",
                meeting_id="m1",
                banana="alphaCA",
                priority=100,
                work_version=version,
                conn=conn,
            )
            row = await conn.fetchrow(
                """
                SELECT work_generation
                FROM pipeline_outbox
                WHERE event_key = $1
                """,
                f"queue.enqueue:meeting://m1:{version}",
            )
            assert row is not None
            generation = int(row["work_generation"])
            await queue.enqueue_job(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                work_version=version,
                desired_generation=generation,
                conn=conn,
            )
            await conn.execute(
                """
                UPDATE pipeline_outbox
                SET status = 'published', published_at = NOW()
                WHERE event_key = $1
                """,
                f"queue.enqueue:meeting://m1:{version}",
            )
            return generation

        async with conn.transaction():
            first_a = await publish_intent("mv1:a")
        async with conn.transaction():
            generation_b = await publish_intent("mv1:b")
        async with conn.transaction():
            await lifecycle.enqueue_queue_job(
                source_url="meeting://m1",
                job_type="meeting",
                payload={"meeting_id": "m1"},
                aggregate_id="m1",
                meeting_id="m1",
                banana="alphaCA",
                priority=100,
                work_version="mv1:a",
                conn=conn,
            )

        recurrence = await conn.fetchrow(
            """
            SELECT status, work_generation, published_at
            FROM pipeline_outbox
            WHERE event_key = 'queue.enqueue:meeting://m1:mv1:a'
            """
        )
        assert recurrence is not None
        assert recurrence["status"] == "pending"
        assert recurrence["published_at"] is None
        assert int(recurrence["work_generation"]) > generation_b > first_a
        await conn.close()
