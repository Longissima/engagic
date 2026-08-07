"""
Pipeline Job Models - Type-safe queue job definitions with Pydantic validation

Defines all job types that can be enqueued for processing.
Each job type has a specific payload with required fields.
Runtime validation catches type errors before queue insertion.
"""

from pydantic.dataclasses import dataclass
from dataclasses import asdict, field
from typing import Literal, Union, List, Dict, Any, Optional
import json


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

    New payloads intentionally do not snapshot appearance item IDs. The
    item_ids field remains as a processor compatibility shim, but deserialization
    deliberately discards legacy snapshots so every release queries the
    authoritative matter appearances.
    """
    matter_id: str  # Composite ID: {banana}_{matter_key}
    meeting_id: str  # Representative meeting where matter appears
    item_ids: List[str] = field(default_factory=list)  # Legacy payload only

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "matter_id": self.matter_id,
            "meeting_id": self.meeting_id,
        }
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MatterJob":
        return cls(
            matter_id=data["matter_id"],
            meeting_id=data["meeting_id"],
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
        - payload: JSON string of payload data
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
            created_at=row.get("created_at"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            work_version=row.get("work_version"),
        )


def serialize_payload(payload: JobPayload) -> str:
    """Serialize payload to JSON string for database storage"""
    return json.dumps(payload.to_dict())


def create_meeting_job(meeting_id: str, banana: str, priority: int = 0) -> Dict[str, Any]:
    """Helper to create meeting job data for enqueueing"""
    payload = MeetingJob(meeting_id=meeting_id)
    return {
        "job_type": "meeting",
        "payload": serialize_payload(payload),
        "banana": banana,
        "priority": priority
    }


def create_matter_job(
    matter_id: str,
    meeting_id: str,
    banana: str,
    priority: int = 0,
    work_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Helper to create matter job data for enqueueing"""
    payload = MatterJob(
        matter_id=matter_id,
        meeting_id=meeting_id,
    )
    return {
        "job_type": "matter",
        "payload": serialize_payload(payload),
        "banana": banana,
        "priority": priority,
        "work_version": work_version,
    }
