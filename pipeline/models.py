"""
Pipeline Job Models - Type-safe queue job definitions with Pydantic validation

Defines all job types that can be enqueued for processing.
Each job type has a specific payload with required fields.
Runtime validation catches type errors before queue insertion.
"""

from dataclasses import asdict, field
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic.dataclasses import dataclass


@dataclass
class MeetingJob:
    """Process a meeting (monolithic or item-level)

    Processor fetches meeting from DB to get URLs - only meeting_id needed here.
    """
    meeting_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MeetingJob":
        return cls(meeting_id=data["meeting_id"])


@dataclass
class MatterJob:
    """Process a matter across all its appearances (matters-first)

    When a matter appears in multiple meetings, this job:
    1. Runs only when MatterEnqueueDecider saw a substantive attachment
       hash change (unchanged-hash case is handled at sync-time via a
       prior-appearance copy onto the new item, no LLM call)
    2. Loads the current appearances from the database at claim time
    3. Calls the LLM on the aggregated attachment set across appearances
    4. Writes the fresh summary to city_matters.canonical_summary
    5. Fills items.summary for any appearance that has no
       snapshot yet (temporal snapshots already set stay frozen)

    New payloads contain only matter identity and intentionally do not snapshot
    a representative meeting or appearance item IDs. Both legacy fields remain
    processor compatibility shims; deserialization accepts the meeting hint but
    deliberately discards item snapshots so every release queries authoritative
    matter appearances.
    """
    matter_id: str  # Composite ID: {banana}_{matter_key}
    meeting_id: Optional[str] = None  # Legacy representative-meeting hint
    item_ids: List[str] = field(default_factory=list)  # Legacy payload only

    def to_dict(self) -> Dict[str, Any]:
        return {"matter_id": self.matter_id}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MatterJob":
        return cls(
            matter_id=data["matter_id"],
            meeting_id=data.get("meeting_id"),
            item_ids=[],
        )


JobPayload = Union[MeetingJob, MatterJob]
JobType = Literal["meeting", "matter"]


@dataclass
class QueueJob:
    """Typed queue job with discriminated union payload

    The job_type field determines which payload type is present.
    This enables exhaustive type checking and safe dispatch.
    """
    id: int
    job_type: JobType
    payload: JobPayload
    banana: str
    priority: int
    status: str
    retry_count: int = 0
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    work_version: Optional[str] = None
    last_enqueued_at: Optional[str] = None
    claim_token: Optional[str] = None
    claimed_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    ready_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary for database storage"""
        data = asdict(self)
        # payload is already a dict via asdict
        return data

    @classmethod
    def from_db_row(cls, row: Dict[str, Any]) -> "QueueJob":
        """Deserialize from database row

        Database stores:
        - job_type: "meeting" | "matter"
        - payload: decoded JSON object (asyncpg JSONB codec)
        """
        job_type = row["job_type"]
        payload_data = row["payload"]

        # Deserialize to correct payload type
        if job_type == "meeting":
            payload = MeetingJob.from_dict(payload_data)
        elif job_type == "matter":
            payload = MatterJob.from_dict(payload_data)
        else:
            raise ValueError(f"Unknown job_type: {job_type}")

        return cls(
            id=row["id"],
            job_type=job_type,
            payload=payload,
            banana=row["banana"],
            priority=row["priority"],
            status=row["status"],
            retry_count=row.get("retry_count", 0),
            error_message=row.get("error_message"),
            created_at=_timestamp_string(row.get("created_at")),
            started_at=_timestamp_string(row.get("started_at")),
            completed_at=_timestamp_string(row.get("completed_at")),
            work_version=row.get("work_version"),
            last_enqueued_at=_timestamp_string(row.get("last_enqueued_at")),
            claim_token=(
                str(row["claim_token"]) if row.get("claim_token") is not None else None
            ),
            claimed_at=_timestamp_string(row.get("claimed_at")),
            heartbeat_at=_timestamp_string(row.get("heartbeat_at")),
            ready_at=_timestamp_string(row.get("ready_at")),
        )


def _timestamp_string(value: Any) -> Optional[str]:
    if value is None or isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if isoformat else str(value)


def create_meeting_job(meeting_id: str, banana: str, priority: int = 0) -> Dict[str, Any]:
    """Helper to create meeting job data for enqueueing"""
    payload = MeetingJob(meeting_id=meeting_id)
    return {
        "job_type": "meeting",
        "payload": payload.to_dict(),
        "banana": banana,
        "priority": priority
    }


def create_matter_job(
    matter_id: str,
    meeting_id: Optional[str] = None,
    banana: Optional[str] = None,
    priority: int = 0,
    work_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an identity-only matter descriptor.

    ``meeting_id`` remains accepted so older callers do not break, but it is no
    longer serialized: matter processing resolves authoritative appearances by
    ``matter_id``.
    """
    del meeting_id
    if banana is None:
        raise ValueError("banana is required")
    payload = MatterJob(matter_id=matter_id)
    return {
        "job_type": "matter",
        "payload": payload.to_dict(),
        "banana": banana,
        "priority": priority,
        "work_version": work_version,
    }
