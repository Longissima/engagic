from types import SimpleNamespace

import pytest

from pipeline.filters import (
    ATTACHMENT_FILTER_VERSION,
    ITEM_FILTER_VERSION,
    get_attachment_filter_decision,
    get_filter_decision,
)
from pipeline.orchestrators.meeting_sync import _source_audit
from pipeline.outcomes import JobOutcome, OutcomeStatus
from pipeline.processor import Processor
from pipeline.utils import MatterWorkSnapshot
from scripts.recompute_item_filters import desired_filter


def test_item_filter_decision_is_versioned_and_rule_specific():
    first = get_filter_decision("Roll Call")
    second = get_filter_decision("Roll Call")
    assert first == second
    assert first is not None
    assert first.reason == "procedural"
    assert first.version == ITEM_FILTER_VERSION
    assert first.rule_id.startswith("procedural:")


def test_item_filter_handles_dated_minutes_and_exact_sections():
    minutes = get_filter_decision(
        "Approval of March 30, 2026 Work Session Meeting minutes."
    )
    communications = get_filter_decision("COMMUNICATIONS")
    award = get_filter_decision("Presentation of Officer of the Quarter")
    assert minutes is not None and minutes.reason == "procedural"
    assert communications is not None and communications.reason == "procedural"
    assert award is not None and award.reason == "ceremonial"


def test_attachment_filter_audit_records_counts_without_dropping_attachments():
    attachments = [
        SimpleNamespace(name="Speaker Cards", url="https://example.test/comments.pdf"),
        SimpleNamespace(name="Staff Report", url="https://example.test/report.pdf"),
    ]
    item = SimpleNamespace(
        id="i1", meeting_id="m1", sequence=1, title="Item", attachments=attachments,
        body_text=None,
    )
    work = MatterWorkSnapshot.from_appearances([item])
    assert len(work.attachments) == 2
    assert len(work.substantive_attachments) == 1
    assert work.attachment_filter_audit["version"] == ATTACHMENT_FILTER_VERSION
    assert work.attachment_filter_audit["excluded"] == 1
    assert get_attachment_filter_decision("Speaker Cards") is not None


def test_source_audit_distinguishes_chunk_html_adapter_and_monolith_paths():
    assert _source_audit({"chunk_audit": {"parse_method": "v2_toc"}})[
        "source_path"
    ] == "chunked_pdf"
    assert _source_audit({"html_audit": {"pattern": "generated"}})[
        "source_path"
    ] == "html"
    assert _source_audit({"items": [{"title": "A"}]})["source_path"] == "adapter_items"
    assert _source_audit({"packet_url": "https://example.test/p.pdf"})[
        "source_path"
    ] == "monolith_document"


def test_replay_clears_stale_contentful_filter():
    row = {
        "title": "Purchase fire engine",
        "attachments": [{"name": "Staff Report"}],
        "body_text": None,
        "filter_reason": "procedural",
    }
    assert desired_filter(row) is None


def test_unit_failure_has_structured_type_and_reason():
    outcome = JobOutcome.from_stats({
        "items_processed": 0,
        "items_failed": 1,
        "failure_reason": "empty_summary",
    })
    assert outcome.status is OutcomeStatus.RETRYABLE_FAILURE
    assert outcome.error_type == "UnitFailure"
    assert outcome.error == "1 processing unit(s) failed: empty_summary"


@pytest.mark.asyncio
async def test_packet_diversion_requires_unsafe_chunk_slices():
    quality = {"seg_smell": "over_split", "item_count": 100, "garbage_titles": 4}

    async def get_chunk_quality(_meeting_id):
        return quality

    processor = Processor.__new__(Processor)
    processor.db = SimpleNamespace(
        queue=SimpleNamespace(get_chunk_quality=get_chunk_quality)
    )
    meeting = SimpleNamespace(id="m1", packet_url="https://example.test/packet.pdf")
    assert not await processor._diverts_to_packet(meeting)
    quality["garbage_titles"] = 25
    assert await processor._diverts_to_packet(meeting)
    quality.update(seg_smell="under_split", garbage_titles=0)
    assert await processor._diverts_to_packet(meeting)
