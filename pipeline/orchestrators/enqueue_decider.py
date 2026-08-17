"""Enqueue Decider - Determines if meetings need processing"""

from datetime import datetime
from typing import Optional, List, TYPE_CHECKING

from pipeline.utils import matter_title_identity
from pipeline.filters import get_filter_decision
from vendors.adapters.parsers.morphology import is_bare_document

if TYPE_CHECKING:
    from database.models import Meeting, AgendaItem, Matter

QUEUE_PRIORITY_BASE_SCORE = 150


class EnqueueDecider:
    """Decides if meetings should be enqueued for processing"""

    def should_enqueue(
        self,
        meeting: "Meeting",
        agenda_items: List["AgendaItem"],
        has_items: bool,
        chunk_audit: Optional[dict] = None,
    ) -> tuple[bool, Optional[str]]:
        """Determine if meeting should be enqueued for processing

        Returns (should_enqueue, skip_reason) tuple.
        """
        # Check for item-level summaries (golden path)
        def filter_still_applies(item: "AgendaItem") -> bool:
            reason = getattr(item, "filter_reason", None)
            if not reason:
                return False
            decision = get_filter_decision(item.title)
            if decision and decision.reason == reason:
                return True
            if reason == "no_content":
                return not item.attachments and not item.body_text
            if reason == "bare_agenda" and chunk_audit:
                profiles = [
                    run.get("profile")
                    for run in chunk_audit.get("runs", [])
                    if isinstance(run.get("profile"), dict)
                ]
                return bool(profiles) and all(is_bare_document(p) for p in profiles)
            return False

        if has_items and agenda_items:
            items_done = [
                item for item in agenda_items
                if item.summary or filter_still_applies(item)
            ]
            if items_done and len(items_done) == len(agenda_items):
                return False, f"all {len(agenda_items)} items already processed"

        # Check for monolithic summary (fallback path)
        if meeting.summary:
            return False, "meeting already has summary (monolithic)"

        return True, None

    def calculate_priority(self, meeting_date: Optional[datetime]) -> int:
        """Calculate queue priority based on meeting date proximity (0-150)"""
        if meeting_date:
            now = datetime.now(meeting_date.tzinfo) if meeting_date.tzinfo else datetime.now()
            days_distance = abs((meeting_date - now).days)
        else:
            days_distance = 999
        return max(0, QUEUE_PRIORITY_BASE_SCORE - days_distance)


MATTER_PRIORITY_BASE_SCORE = 50

# Consecutive failed summarization attempts against one attachment set before
# the decider stops re-enqueueing. Without a bound, a matter that always fails
# (dead links, unextractable scans) re-enqueues at every sync and re-pays
# download + OCR + LLM attempts forever. An attachment change resets the
# budget (see MatterRepository.record_matter_outcome).
MATTER_MAX_ATTEMPTS = 3


class MatterEnqueueDecider:
    """Enqueue new matters with attachments, or existing matters with changed attachments.
    Priority lower than meetings (-100 to 50 vs 0-150).
    """

    def should_enqueue_matter(
        self,
        existing_matter: Optional["Matter"],
        current_attachment_hash: str,
        has_attachments: bool,
        current_attachment_hash_legacy: Optional[str] = None,
        current_work_version: Optional[str] = None,
        current_title: Optional[str] = None,
    ) -> tuple[bool, Optional[str]]:
        if not has_attachments:
            return False, "no_attachments"

        if existing_matter is None:
            return True, None

        md = existing_matter.metadata
        stored_hash = md.attachment_hash if md else None

        # Unchanged = stored hash matches this scrape under the current
        # algorithm, or under the legacy algorithm for pre-sv1 stored values
        # (format moved underneath unchanged attachments; the caller persists
        # the current format on this signal, retiring the legacy value).
        unchanged = stored_hash is not None and (
            stored_hash == current_attachment_hash
            or (
                ":" not in stored_hash
                and current_attachment_hash_legacy is not None
                and stored_hash == current_attachment_hash_legacy
            )
        )
        if not unchanged:
            # New or never-hashed content: always worth (re)processing.
            return True, None

        if md and md.work_version and current_work_version:
            if md.work_version != current_work_version:
                return True, None
        elif current_title:
            if matter_title_identity(existing_matter.title) != matter_title_identity(
                current_title
            ):
                return True, None

        if existing_matter.canonical_summary:
            return False, "attachments_unchanged"

        # No canonical summary, but processing already rendered a verdict on
        # exactly this attachment set: either a terminal disposition (will
        # never summarize, e.g. filtered title) or an exhausted retry budget.
        if md and md.disposition:
            return False, f"disposition_{md.disposition}"
        if md and md.attempts >= MATTER_MAX_ATTEMPTS:
            return False, "max_attempts"

        return True, None

    def calculate_priority(self, meeting_date: Optional[datetime]) -> int:
        if meeting_date:
            now = datetime.now(meeting_date.tzinfo) if meeting_date.tzinfo else datetime.now()
            days_distance = abs((meeting_date - now).days)
        else:
            days_distance = 999
        return max(-100, MATTER_PRIORITY_BASE_SCORE - days_distance)
