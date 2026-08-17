from types import SimpleNamespace

from pipeline.filters import (
    ATTACHMENT_FILTER_VERSION,
    ITEM_FILTER_VERSION,
    get_attachment_filter_decision,
    get_filter_decision,
)
from pipeline.orchestrators.meeting_sync import _source_audit
from pipeline.outcomes import JobOutcome, OutcomeStatus
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
