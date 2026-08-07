"""
Pydantic schemas for adapter outputs - runtime validation at boundaries.

These schemas validate data from vendor adapters before it enters the database.
Catches type errors early instead of failing at SQLite INSERT time.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AttachmentSchema(BaseModel):
    """Attachment metadata from adapter"""
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    # PrimeGov's durable history endpoint does not expose a media type. The
    # downloader detects it later, so absence here is legitimate, not a broken
    # attachment contract.
    type: str = "unknown"  # pdf, doc, spreadsheet, html, document, unknown
    portal_url: Optional[str] = None  # Stable viewer URL (CivicClerk portal) -- HTML, not a download
    history_id: Optional[str] = None  # PrimeGov-specific identifier for downloading
    meta_id: Optional[str] = None  # Granicus-specific
    cc_agenda_id: Optional[int] = None  # CivicClerk: Meetings/{id} for URL refresh
    cc_attachment_id: Optional[int] = None  # CivicClerk: attachment id within agenda

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Ensure URL is non-empty string"""
        if not v or not v.strip():
            raise ValueError("Attachment URL cannot be empty")
        return v.strip()

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: Any) -> str:
        return str(v).strip() if v else "unknown"


class AgendaItemSchema(BaseModel):
    """Agenda item from adapter - validates before DB storage.

    Note: Adapters return vendor_item_id (raw vendor identifier).
    Orchestrator generates final item_id via generate_item_id().
    """
    model_config = ConfigDict(extra="allow")  # Allow adapter-specific extras

    vendor_item_id: Optional[str] = None  # Raw vendor identifier (optional - falls back to sequence)
    title: str
    sequence: int  # MUST be int, not string
    attachments: List[AttachmentSchema] = Field(default_factory=list)
    matter_id: Optional[str] = None  # Vendor's matter ID (not our generated ID)
    matter_file: Optional[str] = None
    matter_type: Optional[str] = None
    agenda_number: Optional[str] = None
    sponsors: Optional[List[str]] = None
    votes: Optional[List[Dict[str, Any]]] = None  # Vote records from adapter
    metadata: Optional[Dict[str, Any]] = None  # Vendor-specific metadata (action_name, section, etc.)

    @field_validator("vendor_item_id", "matter_id", "agenda_number", mode="before")
    @classmethod
    def normalize_identifiers(cls, v: Any) -> Any:
        # HTML data attributes and JSON APIs disagree on whether numeric IDs
        # are strings or integers. Canonical item identity is textual.
        if isinstance(v, int) and not isinstance(v, bool):
            return str(v)
        return v

    @field_validator("sequence")
    @classmethod
    def validate_sequence(cls, v: Any) -> int:
        """Ensure sequence is integer (catches string "0" from APIs)"""
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                raise ValueError(f"Sequence must be integer, got string: {v}")
        return int(v)

    @field_validator("attachments", mode="before")
    @classmethod
    def normalize_attachments(cls, v: Any) -> Any:
        # A few vendor APIs distinguish a missing collection with null. The
        # pipeline has always treated that identically to an empty collection.
        return [] if v is None else v

    @field_validator("title")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        """Ensure required strings are non-empty"""
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()


class MeetingSchema(BaseModel):
    """Meeting from adapter - validates before DB storage.

    Note: Adapters return vendor_id (raw vendor identifier).
    Orchestrator generates final meeting_id via generate_meeting_id().
    """
    model_config = ConfigDict(extra="allow")  # Allow adapter-specific extras

    vendor_id: str  # Raw vendor identifier (REQUIRED)
    title: str
    start: Optional[str] = None  # ISO string, or None when source is undated
    location: Optional[str] = None
    agenda_url: Optional[str] = None
    packet_url: Optional[str] = None
    minutes_url: Optional[str] = None  # Minutes doc/page; publishes post-meeting, fills on resync
    items: Optional[List[AgendaItemSchema]] = None
    participation: Optional[Dict[str, Any]] = None
    meeting_status: Optional[str] = None
    vendor_body_id: Optional[str] = None  # Vendor's committee/body ID (Legistar provides this)
    metadata: Optional[Dict[str, Any]] = None

    @field_validator("vendor_id", mode="before")
    @classmethod
    def normalize_vendor_id(cls, v: Any) -> Any:
        # Vendor APIs commonly use integer primary keys. Our canonical IDs are
        # textual, so normalize losslessly at the boundary.
        if isinstance(v, int) and not isinstance(v, bool):
            return str(v)
        return v

    @field_validator("start")
    @classmethod
    def validate_start_is_string(cls, v: Any) -> Optional[str]:
        """Accept an authoritative absence; otherwise require an ISO string."""
        if v is None:
            return None
        if isinstance(v, datetime):
            raise ValueError(
                "Meeting 'start' must be ISO string, not datetime object. "
                "Use meeting_date.isoformat() in adapter."
            )
        if not isinstance(v, str):
            raise ValueError(f"Meeting 'start' must be string, got {type(v)}")
        # Validate it's parseable as ISO datetime
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"Invalid ISO datetime string: {v}") from e
        return v

    @field_validator("vendor_id", "title")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        """Ensure required strings are non-empty"""
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()

    @field_validator("items")
    @classmethod
    def validate_items(cls, v: Optional[List[AgendaItemSchema]]) -> Optional[List[AgendaItemSchema]]:
        """Validate items list if present"""
        if v is not None and not isinstance(v, list):
            raise ValueError("Items must be a list")
        return v


def validate_meeting_output(meeting_dict: Dict[str, Any]) -> MeetingSchema:
    """
    Validate adapter meeting output against schema.

    Args:
        meeting_dict: Raw meeting dict from adapter

    Returns:
        Validated MeetingSchema

    Raises:
        ValidationError: If data doesn't match schema
    """
    return MeetingSchema(**meeting_dict)


def validate_item_output(item_dict: Dict[str, Any]) -> AgendaItemSchema:
    """
    Validate adapter item output against schema.

    Args:
        item_dict: Raw item dict from adapter

    Returns:
        Validated AgendaItemSchema

    Raises:
        ValidationError: If data doesn't match schema
    """
    return AgendaItemSchema(**item_dict)
