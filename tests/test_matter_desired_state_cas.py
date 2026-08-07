"""Matter domain-write fences include queue desired state and claim ownership."""

from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from database.id_generation import generate_matter_id
from pipeline.job_runner import TerminalJobError
from pipeline.processor import Processor
from pipeline.utils import MatterWorkSnapshot, matter_no_work_version


MATTER_ID = cast(
    str,
    generate_matter_id("alphaCA", matter_file="ORD-CAS"),
)


def _appearance():
    return SimpleNamespace(
        id="item-cas",
        meeting_id="meeting-cas",
        sequence=1,
        title="Matter CAS",
        matter_id=MATTER_ID,
        matter_file="ORD-CAS",
        matter_type="Ordinance",
        sponsors=[],
        attachments=[
            SimpleNamespace(
                name="Staff report",
                url="https://example.gov/cas.pdf",
                type="pdf",
            )
        ],
        summary=None,
        topics=None,
    )


class _Transaction(AbstractAsyncContextManager):
    def __init__(self, events, connection):
        self.events = events
        self.connection = connection

    async def __aenter__(self):
        self.events.append("begin")
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        self.events.append("rollback" if exc_type else "commit")
        return False


def _processor_with_state(desired_state):
    events = []
    connection = cast(Any, object())
    item = _appearance()
    matter = cast(
        Any,
        SimpleNamespace(
            id=MATTER_ID,
            attachments=[],
            metadata=None,
            appearance_count=1,
        ),
    )

    class Matters:
        def transaction(self):
            return _Transaction(events, connection)

        async def get_matter(self, matter_id, *, conn, lock_for_update):
            assert (matter_id, conn, lock_for_update) == (
                MATTER_ID,
                connection,
                True,
            )
            events.append("lock matter")
            return matter

        async def store_matter(self, stored, *, conn):
            assert conn is connection
            events.append("store projection")

    class Items:
        async def get_all_items_for_matter(
            self, matter_id, *, conn, lock_for_update
        ):
            assert (matter_id, conn, lock_for_update) == (
                MATTER_ID,
                connection,
                True,
            )
            events.append("lock items")
            return [item]

        async def bulk_fill_null_item_summaries(self, **kwargs):
            assert kwargs["conn"] is connection
            events.append("fill snapshots")
            return 1

    class Queue:
        async def lock_desired_state(self, source_url, *, conn):
            assert source_url == f"matter://{MATTER_ID}"
            assert conn is connection
            events.append("lock queue desired")
            return desired_state

    processor = Processor.__new__(Processor)
    processor.db = cast(
        Any,
        SimpleNamespace(
            matters=Matters(),
            items=Items(),
            queue=Queue(),
        ),
    )
    processor.analyzer = None
    return processor, matter, item, events


@pytest.mark.asyncio
async def test_matter_processing_requires_claim_before_any_database_load():
    processor = Processor.__new__(Processor)
    processor.db = cast(Any, SimpleNamespace())

    with pytest.raises(RuntimeError, match="requires an owned queue claim"):
        await processor.process_matter(
            MATTER_ID,
            expected_work_version="mw1:untrusted-direct-call",
        )


@pytest.mark.asyncio
async def test_single_item_helper_has_no_unfenced_filter_write():
    class Items:
        async def update_filter_reason(self, *args, **kwargs):
            raise AssertionError("filter disposition belongs to the caller's CAS")

    processor = Processor.__new__(Processor)
    processor.analyzer = cast(Any, object())
    processor.db = cast(Any, SimpleNamespace(items=Items()))
    procedural = SimpleNamespace(
        id="procedural-item",
        title="Call to Order",
        attachments=[],
        body_text=None,
    )

    assert await processor._process_single_item(procedural) is None


@pytest.mark.asyncio
async def test_legacy_claimed_descriptor_reaches_domain_write_under_mw1_cas():
    item = _appearance()
    work = MatterWorkSnapshot.from_appearances([item])
    legacy_descriptor = work.legacy_attachment_version
    claim_token = "00000000-0000-0000-0000-000000000041"
    processor, matter, _item, events = _processor_with_state(
        {
            "status": "processing",
            "work_version": legacy_descriptor,
            "desired_generation": 41,
            "claim_token": claim_token,
        }
    )

    items, filled = await processor._persist_matter_projection(
        matter,
        expected_work_version=work.work_version,
        expected_desired_version=legacy_descriptor,
        expected_claim_token=claim_token,
        summary="Canonical summary",
        topics=["policy"],
    )

    assert [value.id for value in items] == ["item-cas"]
    assert filled == 1
    assert matter.metadata.work_version == work.work_version
    assert events == [
        "begin",
        "lock matter",
        "lock items",
        "lock queue desired",
        "store projection",
        "fill snapshots",
        "commit",
    ]


@pytest.mark.asyncio
async def test_same_content_tombstone_rejects_stale_domain_write():
    item = _appearance()
    work = MatterWorkSnapshot.from_appearances([item])
    executable_descriptor = work.work_version
    tombstone = matter_no_work_version(executable_descriptor, "procedural")
    processor, matter, _item, events = _processor_with_state(
        {
            "status": "completed",
            "work_version": tombstone,
            "desired_generation": 42,
            "claim_token": None,
        }
    )

    with pytest.raises(TerminalJobError, match="desired work was superseded"):
        await processor._persist_matter_projection(
            matter,
            expected_work_version=executable_descriptor,
            expected_desired_version=executable_descriptor,
            expected_claim_token="00000000-0000-0000-0000-000000000041",
            summary="Must not commit",
            topics=["stale"],
        )

    assert events == [
        "begin",
        "lock matter",
        "lock items",
        "lock queue desired",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_reactivated_same_version_requires_current_claim_owner():
    item = _appearance()
    work = MatterWorkSnapshot.from_appearances([item])
    processor, matter, _item, events = _processor_with_state(
        {
            "status": "processing",
            "work_version": work.work_version,
            "desired_generation": 43,
            "claim_token": "00000000-0000-0000-0000-000000000043",
        }
    )

    with pytest.raises(TerminalJobError, match="queue claim was superseded"):
        await processor._persist_matter_projection(
            matter,
            expected_work_version=work.work_version,
            expected_desired_version=work.work_version,
            expected_claim_token="00000000-0000-0000-0000-000000000041",
            summary="Old owner",
            topics=[],
        )

    assert "store projection" not in events
