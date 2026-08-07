"""Pure planning primitives for historical matter queue reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from pipeline.orchestrators.enqueue_decider import MATTER_MAX_ATTEMPTS
from pipeline.utils import (
    MatterWorkSnapshot,
    matter_title_identity,
)


class ReconciliationAction(StrEnum):
    NONE = "none"
    ENQUEUE_VERSION = "enqueue_version"
    REACTIVATE_VERSION = "reactivate_version"


@dataclass(frozen=True, slots=True)
class MatterReconciliationPlan:
    matter_id: str
    desired_version: str
    action: ReconciliationAction
    reason: str
    representative_meeting_id: str | None


def plan_matter_reconciliation(
    *,
    matter_id: str,
    appearances: Iterable[Any],
    queue_row: Mapping[str, Any] | None,
    canonical_summary: str | None,
    canonical_attachment_hash: str | None,
    canonical_work_version: str | None = None,
    canonical_title: str | None = None,
    canonical_disposition: str | None = None,
    canonical_attempts: int = 0,
    max_attempts: int = MATTER_MAX_ATTEMPTS,
) -> MatterReconciliationPlan:
    items = list(appearances)
    work = MatterWorkSnapshot.from_appearances(items)
    desired = work.work_version
    desired_attachment = work.attachment_version
    representative = next(
        (str(item.meeting_id) for item in reversed(items) if item.meeting_id),
        None,
    )
    if not items:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.NONE,
            "no_appearances",
            representative,
        )
    if not work.is_summarizable:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.NONE,
            "no_substantive_attachments",
            representative,
        )
    artifact_current = canonical_attachment_hash is not None and (
        canonical_attachment_hash == desired_attachment
        or (
            ":" not in canonical_attachment_hash
            and canonical_attachment_hash == work.legacy_attachment_version
        )
    )
    work_current = canonical_work_version == desired
    if canonical_work_version is None and canonical_title and items:
        work_current = matter_title_identity(canonical_title) == matter_title_identity(
            str(items[0].title or "")
        )
    terminal_resolution = artifact_current and work_current and (
        bool(canonical_disposition) or canonical_attempts >= max_attempts
    )
    if not canonical_summary and terminal_resolution:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.NONE,
            (
                f"disposition_{canonical_disposition}"
                if canonical_disposition
                else "max_attempts"
            ),
            representative,
        )

    needs_projection = not canonical_summary or not artifact_current or not work_current
    needs_snapshots = any(not getattr(item, "summary", None) for item in items)

    if not needs_projection and not needs_snapshots:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.NONE,
            "projection_and_snapshots_current",
            representative,
        )

    if queue_row is None:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.ENQUEUE_VERSION,
            "missing_queue_row",
            representative,
        )

    queued_version = queue_row.get("work_version")
    if queued_version != desired:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.ENQUEUE_VERSION,
            "legacy_or_stale_version",
            representative,
        )

    if (needs_projection or needs_snapshots) and queue_row.get("status") in {
        "completed",
        "failed",
        "dead_letter",
    }:
        return MatterReconciliationPlan(
            matter_id,
            desired,
            ReconciliationAction.REACTIVATE_VERSION,
            "terminal_current_version_has_incomplete_projection",
            representative,
        )

    return MatterReconciliationPlan(
        matter_id,
        desired,
        ReconciliationAction.NONE,
        "current_version_active",
        representative,
    )
