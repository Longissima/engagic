"""Enqueue Decider - Determines if meetings need processing"""

from datetime import datetime
from typing import Optional, List, TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import Meeting, AgendaItem, Matter

QUEUE_PRIORITY_BASE_SCORE = 150


class EnqueueDecider:
    """Decides if meetings should be enqueued for processing"""

    def should_enqueue(
        self,
        meeting: "Meeting",
        agenda_items: List["AgendaItem"],
        has_items: bool
    ) -> tuple[bool, Optional[str]]:
        """Determine if meeting should be enqueued for processing

        Returns (should_enqueue, skip_reason) tuple.
        """
        # Check for item-level summaries (golden path)
        # Items with filter_reason are intentionally skipped and will never get summaries
        if has_items and agenda_items:
            items_done = [item for item in agenda_items if item.summary or item.filter_reason]
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
    ) -> tuple[bool, Optional[str]]:
        if not has_attachments:
            return False, "no_attachments"

        if existing_matter is None:
            return True, None

        if not existing_matter.canonical_summary:
            return True, None

        stored_hash = existing_matter.metadata.attachment_hash if existing_matter.metadata else None
        if stored_hash == current_attachment_hash:
            return False, "attachments_unchanged"

        # Stored hashes written before version tagging carry no "sv1:" prefix.
        # Compare those against the legacy algorithm: a match means the
        # attachments are unchanged and only the hash format moved underneath
        # them. The caller persists the current-format hash on this signal,
        # retiring the legacy value without a reprocess.
        if (
            stored_hash
            and ":" not in stored_hash
            and current_attachment_hash_legacy is not None
            and stored_hash == current_attachment_hash_legacy
        ):
            return False, "attachments_unchanged"

        return True, None

    def calculate_priority(self, meeting_date: Optional[datetime]) -> int:
        if meeting_date:
            now = datetime.now(meeting_date.tzinfo) if meeting_date.tzinfo else datetime.now()
            days_distance = abs((meeting_date - now).days)
        else:
            days_distance = 999
        return max(-100, MATTER_PRIORITY_BASE_SCORE - days_distance)
