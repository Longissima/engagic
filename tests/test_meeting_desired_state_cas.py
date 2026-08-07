"""Meeting writes fence same-version queue re-owners at the commit boundary."""

from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from exceptions import DatabaseError
from database.models import ParticipationInfo
from pipeline.job_runner import TerminalJobError
from pipeline.models import MeetingJob, QueueJob
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from pipeline.processor import Processor, _merge_participation_info
from pipeline.protocols import NullMetrics
from pipeline.utils import meeting_work_version


MEETING_ID = "meeting-cas"
OLD_TOKEN = "00000000-0000-0000-0000-000000000041"
NEW_TOKEN = "00000000-0000-0000-0000-000000000043"


def _meeting():
    return SimpleNamespace(
        id=MEETING_ID,
        banana="alphaCA",
        title="Council Meeting",
        date=None,
        agenda_url="https://example.gov/agenda.pdf",
        agenda_sources=None,
        packet_url=None,
        minutes_url=None,
        participation=None,
    )


def _item(*, summary=None):
    return SimpleNamespace(
        id="item-cas",
        meeting_id=MEETING_ID,
        sequence=1,
        title="Public hearing",
        body_text="Substantive record",
        matter_id=None,
        matter_file=None,
        matter_type=None,
        agenda_number="1",
        sponsors=[],
        filter_reason=None,
        attachments=[],
        summary=summary,
        topics=[],
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


def _processor_with_state(desired_state, *, summary=None, items=None):
    events: list[str] = []
    connection = cast(Any, object())
    meeting = _meeting()
    item = _item(summary=summary)
    current_items = items or [item]

    class Meetings:
        def transaction(self):
            return _Transaction(events, connection)

        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                MEETING_ID,
                connection,
                True,
            )
            events.append("lock meeting")
            return meeting

        async def update_meeting_summary(self, *, meeting_id, conn, **fields):
            assert (meeting_id, conn) == (MEETING_ID, connection)
            assert fields["topics"] == ["current"]
            events.append("update meeting")

        async def update_processing_status(self, meeting_id, status, *, conn):
            assert (meeting_id, status, conn) == (
                MEETING_ID,
                "pending",
                connection,
            )
            events.append("mark meeting pending")

    class Items:
        async def get_agenda_items(self, meeting_id, *, conn, lock_for_update):
            assert (meeting_id, conn, lock_for_update) == (
                MEETING_ID,
                connection,
                True,
            )
            events.append("lock items")
            return current_items

        async def update_agenda_item(self, *, item_id, conn, **fields):
            assert (item_id, conn) == (item.id, connection)
            assert fields["summary"] == "first summary"
            events.append("update item")

        async def update_filter_reason(self, item_id, reason, *, conn):
            assert (item_id, reason, conn) == (item.id, "procedural", connection)
            events.append("update filter")

    class Queue:
        async def lock_desired_state(self, source_url, *, conn):
            assert (source_url, conn) == (f"meeting://{MEETING_ID}", connection)
            events.append("lock queue desired")
            return desired_state

    processor = Processor.__new__(Processor)
    processor.db = cast(
        Any,
        SimpleNamespace(meetings=Meetings(), items=Items(), queue=Queue()),
    )
    return processor, meeting, item, events


@pytest.mark.asyncio
async def test_meeting_processing_requires_claim_before_any_database_load():
    processor = Processor.__new__(Processor)
    processor.db = cast(Any, SimpleNamespace())

    with pytest.raises(RuntimeError, match="requires an owned queue claim"):
        await processor.process_meeting(
            cast(Any, _meeting()),
            expected_work_version="mv1:untrusted-direct-call",
        )


@pytest.mark.asyncio
async def test_both_queue_lanes_forward_the_exact_meeting_claim():
    meeting = _meeting()
    work_version = meeting_work_version(meeting, [])
    calls = []

    class Meetings:
        async def get_meeting(self, meeting_id):
            assert meeting_id == MEETING_ID
            return meeting

    processor = Processor.__new__(Processor)
    processor.db = cast(Any, SimpleNamespace(meetings=Meetings()))
    processor.metrics = NullMetrics()

    async def process(_meeting, use_batch=False, **kwargs):
        calls.append((use_batch, kwargs))
        return {}

    cast(Any, processor).process_meeting = process
    job = QueueJob(
        id=7,
        job_type="meeting",
        payload=MeetingJob(MEETING_ID),
        banana="alphaCA",
        priority=1,
        status="processing",
        work_version=work_version,
        claim_token=OLD_TOKEN,
    )

    await processor._execute_streaming_job(job)
    await processor._execute_batch_queue_job(job)

    assert calls == [
        (
            False,
            {
                "expected_work_version": work_version,
                "expected_claim_token": OLD_TOKEN,
            },
        ),
        (
            True,
            {
                "expected_work_version": work_version,
                "expected_claim_token": OLD_TOKEN,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_same_version_reowner_rejects_old_item_summary():
    meeting = _meeting()
    item = _item()
    work_version = meeting_work_version(meeting, [item])
    processor, _meeting_row, _item_row, events = _processor_with_state(
        {
            "status": "processing",
            "work_version": work_version,
            "desired_generation": 43,
            "claim_token": NEW_TOKEN,
        }
    )

    with pytest.raises(TerminalJobError, match="queue claim was superseded"):
        await processor._persist_meeting_item_results(
            MEETING_ID,
            work_version,
            [{"item_id": "item-cas", "summary": "first summary", "topics": []}],
            expected_desired_version=work_version,
            expected_claim_token=OLD_TOKEN,
        )

    assert events == [
        "begin",
        "lock meeting",
        "lock items",
        "lock queue desired",
        "rollback",
    ]


@pytest.mark.asyncio
async def test_same_version_reowner_rejects_old_meeting_projection():
    meeting = _meeting()
    item = _item()
    work_version = meeting_work_version(meeting, [item])
    processor, _meeting_row, _item_row, events = _processor_with_state(
        {
            "status": "processing",
            "work_version": work_version,
            "desired_generation": 43,
            "claim_token": NEW_TOKEN,
        }
    )

    with pytest.raises(TerminalJobError, match="queue claim was superseded"):
        await processor._update_meeting_projection(
            MEETING_ID,
            work_version,
            expected_desired_version=work_version,
            expected_claim_token=OLD_TOKEN,
            topics=["current"],
        )

    assert "update meeting" not in events
    assert events[-1] == "rollback"


@pytest.mark.asyncio
async def test_mixed_item_result_cannot_stamp_meeting_completed():
    meeting = _meeting()
    summarized = _item(summary="done")
    missing = _item()
    missing.id = "item-missing"
    missing.sequence = 2
    rows = [summarized, missing]
    work_version = meeting_work_version(meeting, rows)
    processor, _meeting_row, _item_row, events = _processor_with_state(
        {
            "status": "processing",
            "work_version": work_version,
            "desired_generation": 44,
            "claim_token": NEW_TOKEN,
        },
        items=rows,
    )

    completed = await processor._update_meeting_projection(
        MEETING_ID,
        work_version,
        expected_desired_version=work_version,
        expected_claim_token=NEW_TOKEN,
        require_complete_items=True,
        topics=["current"],
    )

    assert completed is False
    assert "update meeting" not in events
    assert events[-2:] == ["mark meeting pending", "commit"]


@pytest.mark.asyncio
async def test_mixed_streaming_result_reports_persisted_success_while_pending():
    meeting = _meeting()
    first = _item()
    second = _item()
    second.id = "item-missing"
    second.sequence = 2
    projection: list[dict] = []

    class Jurisdictions:
        async def get_city(self, _banana):
            return None

    processor = Processor.__new__(Processor)
    processor.db = cast(Any, SimpleNamespace(jurisdictions=Jurisdictions()))
    processor.analyzer = cast(Any, object())

    async def extract(*_args, **_kwargs):
        return {}

    async def filter_items(_items):
        return [], [first, second], []

    async def persist(*_args, **_kwargs):
        return set()

    async def build_cache(*_args, **_kwargs):
        return {}, {}, set()

    def build_requests(*_args, **_kwargs):
        return (
            [{"item_id": first.id}, {"item_id": second.id}],
            {first.id: first, second.id: second},
            [],
        )

    async def process_incrementally(*_args, **_kwargs):
        return (
            [
                {
                    "sequence": first.sequence,
                    "title": first.title,
                    "summary": "persisted",
                    "topics": [],
                }
            ],
            [second.title],
        )

    async def assert_version(*_args, **_kwargs):
        return None

    async def update_projection(**kwargs):
        projection.append(kwargs)
        return False

    cast(Any, processor)._extract_participation_info = extract
    cast(Any, processor)._filter_processed_items = filter_items
    cast(Any, processor)._persist_meeting_item_results = persist
    cast(Any, processor)._build_document_cache = build_cache
    cast(Any, processor)._build_batch_requests = build_requests
    cast(Any, processor)._process_batch_incrementally = process_incrementally
    cast(Any, processor)._assert_meeting_work_version = assert_version
    cast(Any, processor)._update_meeting_projection = update_projection

    result = await processor._process_meeting_with_items(
        cast(Any, meeting),
        [first, second],
        expected_work_version="mv1:test",
        expected_desired_version="mv1:test",
        expected_claim_token=NEW_TOKEN,
    )

    assert result == {
        "items_processed": 1,
        "items_new": 1,
        "items_skipped": 0,
        "items_failed": 1,
    }
    assert projection[0]["require_complete_items"] is True


@pytest.mark.asyncio
async def test_current_owner_writes_filter_but_preserves_frozen_summary():
    meeting = _meeting()
    item = _item(summary="frozen summary")
    work_version = meeting_work_version(meeting, [item])
    desired_state = {
        "status": "processing",
        "work_version": work_version,
        "desired_generation": 44,
        "claim_token": NEW_TOKEN,
    }
    processor, _meeting_row, _item_row, events = _processor_with_state(
        desired_state,
        summary="frozen summary",
    )

    applied = await processor._persist_meeting_item_results(
        MEETING_ID,
        work_version,
        [{"item_id": "item-cas", "summary": "first summary", "topics": []}],
        expected_desired_version=work_version,
        expected_claim_token=NEW_TOKEN,
    )
    assert applied == set()
    assert "update item" not in events

    applied = await processor._persist_meeting_item_results(
        MEETING_ID,
        work_version,
        [{"item_id": "item-cas", "filter_reason": "procedural"}],
        expected_desired_version=work_version,
        expected_claim_token=NEW_TOKEN,
    )
    assert applied == {"item-cas"}
    assert events[-2:] == ["update filter", "commit"]


@pytest.mark.asyncio
async def test_bare_item_gets_explicit_no_content_disposition():
    processor = Processor.__new__(Processor)
    bare = _item()
    bare.body_text = None

    processed, pending, filter_writes = await processor._filter_processed_items(
        [bare]
    )

    assert processed == []
    assert pending == []
    assert filter_writes == [
        {"item_id": "item-cas", "filter_reason": "no_content"}
    ]


def test_participation_merge_preserves_stored_fields_when_result_omits_them():
    stored = ParticipationInfo(email="clerk@example.gov", phone="+16505550100")

    merged = _merge_participation_info(stored, {"virtual_url": "https://zoom.us/j/1"})
    omitted = _merge_participation_info(stored, None)

    assert merged is not None
    assert merged.email == "clerk@example.gov"
    assert merged.virtual_url == "https://zoom.us/j/1"
    assert omitted == stored


@pytest.mark.asyncio
async def test_itemless_meeting_completes_as_owned_no_content():
    meeting = _meeting()
    meeting.agenda_url = None
    work_version = meeting_work_version(meeting, [])
    projection = []

    class Meetings:
        async def get_meeting(self, meeting_id):
            return meeting

    class Items:
        async def get_agenda_items(self, meeting_id):
            return []

    processor = Processor.__new__(Processor)
    processor.db = cast(
        Any,
        SimpleNamespace(meetings=Meetings(), items=Items()),
    )
    processor.analyzer = cast(Any, object())

    async def manufacture(*args, **kwargs):
        return 0

    async def update(**kwargs):
        projection.append(kwargs)
        return True

    cast(Any, processor)._manufacture_items = manufacture
    cast(Any, processor)._update_meeting_projection = update

    result = await processor.process_meeting(
        cast(Any, meeting),
        expected_work_version=work_version,
        expected_claim_token=OLD_TOKEN,
    )

    assert result["items_failed"] == 0
    assert projection[0]["processing_method"] == "no_content"
    assert projection[0]["require_complete_items"] is True
    assert projection[0]["expected_claim_token"] == OLD_TOKEN


@pytest.mark.asyncio
async def test_bare_items_route_to_owned_no_content_finalizer():
    meeting = _meeting()
    meeting.agenda_url = None
    bare = _item()
    bare.body_text = None
    work_version = meeting_work_version(meeting, [bare])
    calls = []

    class Items:
        async def get_agenda_items(self, meeting_id):
            return [bare]

    processor = Processor.__new__(Processor)
    processor.db = cast(Any, SimpleNamespace(items=Items()))
    processor.analyzer = cast(Any, object())

    async def process_items(*args, **kwargs):
        calls.append(kwargs)
        return {
            "items_processed": 0,
            "items_new": 0,
            "items_skipped": 1,
            "items_failed": 0,
        }

    cast(Any, processor)._process_meeting_with_items = process_items

    result = await processor.process_meeting(
        cast(Any, meeting),
        expected_work_version=work_version,
        expected_claim_token=OLD_TOKEN,
    )

    assert result["items_skipped"] == 1
    assert calls == [
        {
            "use_batch": False,
            "expected_work_version": work_version,
            "expected_desired_version": work_version,
            "expected_claim_token": OLD_TOKEN,
        }
    ]


@pytest.mark.asyncio
async def test_packet_completion_requires_summary_and_preserves_participation():
    meeting = _meeting()
    meeting.agenda_url = None
    meeting.packet_url = "https://example.gov/packet.pdf"
    meeting.participation = ParticipationInfo(email="clerk@example.gov")
    work_version = meeting_work_version(meeting, [])
    response = {"success": True, "summary": "Packet summary"}
    projections = []

    class Meetings:
        async def get_meeting(self, meeting_id):
            return meeting

    class Items:
        async def get_agenda_items(self, meeting_id):
            return []

    class Analyzer:
        async def process_agenda_with_cache_async(self, meeting_data):
            return response

    processor = Processor.__new__(Processor)
    processor.db = cast(
        Any,
        SimpleNamespace(meetings=Meetings(), items=Items()),
    )
    processor.analyzer = cast(Any, Analyzer())

    async def manufacture(*args, **kwargs):
        return 0

    async def update(**kwargs):
        projections.append(kwargs)
        return True

    cast(Any, processor)._manufacture_items = manufacture
    cast(Any, processor)._update_meeting_projection = update

    result = await processor.process_meeting(
        cast(Any, meeting),
        expected_work_version=work_version,
        expected_claim_token=OLD_TOKEN,
    )

    assert result["items_new"] == 1
    assert projections[0]["processing_time"] == 0.0
    assert projections[0]["participation"].email == "clerk@example.gov"

    response.clear()
    response.update({"success": True})
    projections.clear()
    result = await processor.process_meeting(
        cast(Any, meeting),
        expected_work_version=work_version,
        expected_claim_token=OLD_TOKEN,
    )

    assert result["items_failed"] == 1
    assert projections == []


@pytest.mark.asyncio
async def test_manufactured_shape_rolls_back_when_claim_was_reowned():
    events: list[str] = []
    meeting = _meeting()
    item = _item()
    work_version = meeting_work_version(meeting, [])

    class Connection:
        def transaction(self):
            return _Transaction(events, self)

    connection = Connection()

    class Acquire(AbstractAsyncContextManager):
        async def __aenter__(self):
            return connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Meetings:
        async def get_meeting(self, meeting_id, *, conn, lock_for_update):
            events.append("lock meeting")
            return meeting

    class Items:
        @staticmethod
        def dedupe_items_by_matter(items):
            return items

        async def get_item_matter_links(self, meeting_id, *, conn):
            return {}

        async def store_agenda_items(self, meeting_id, items, *, conn):
            events.append("store items")
            return len(items)

    class Queue:
        async def lock_desired_state(self, source_url, *, conn):
            events.append("lock queue desired")
            return {
                "status": "processing",
                "work_version": work_version,
                "claim_token": NEW_TOKEN,
            }

    orchestrator = MeetingSyncOrchestrator.__new__(MeetingSyncOrchestrator)
    orchestrator.db = cast(
        Any,
        SimpleNamespace(
            pool=Pool(),
            meetings=Meetings(),
            items=Items(),
            queue=Queue(),
        ),
    )

    async def process_items(*args, **kwargs):
        return [item]

    async def track(*args, **kwargs):
        return {
            "appearance_outcomes": {},
            "affected_matter_ids": set(),
            "procedural_matter_ids": set(),
            "tracked": 0,
        }

    async def reconcile(*args, **kwargs):
        return {}

    async def publish(*args, **kwargs):
        events.append("publish matter work")
        return meeting

    test_orchestrator = cast(Any, orchestrator)
    test_orchestrator._process_agenda_items = process_items
    test_orchestrator._track_matters = track
    test_orchestrator._reconcile_matter_appearances = reconcile
    test_orchestrator._publish_authoritative_work = publish

    with pytest.raises(DatabaseError, match="queue claim was superseded"):
        await orchestrator.attach_items(
            cast(Any, meeting),
            [{"title": "Public hearing"}],
            expected_desired_version=work_version,
            expected_claim_token=OLD_TOKEN,
        )

    assert events == [
        "begin",
        "lock meeting",
        "store items",
        "publish matter work",
        "lock queue desired",
        "rollback",
    ]
