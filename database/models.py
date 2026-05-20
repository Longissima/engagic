"""
Database Models for Engagic

Pydantic dataclasses with runtime validation for core entities.
"""


from typing import Optional, List, Dict
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from pydantic.dataclasses import dataclass
from dataclasses import asdict

from config import get_logger

logger = get_logger(__name__).bind(component="engagic")


# --- JSONB Pydantic Models (for typed serialization/deserialization) ---


class EmailContext(BaseModel):
    """Email with inferred purpose"""
    model_config = ConfigDict(extra="forbid")
    address: str
    purpose: str = "general contact"


class StreamingUrl(BaseModel):
    """Streaming platform URL"""
    model_config = ConfigDict(extra="forbid")
    url: Optional[str] = None
    platform: str
    channel: Optional[str] = None  # For cable TV


class ParticipationInfo(BaseModel):
    """
    Typed JSONB for meetings.participation field.

    Extracted from PDF text before AI summarization.
    Provides structured contact info for civic engagement.
    """
    model_config = ConfigDict(extra="ignore")  # Allow future fields without breaking

    email: Optional[str] = None  # Primary contact email
    emails: Optional[List[EmailContext]] = None  # All emails with context
    phone: Optional[str] = None  # Normalized phone (+1XXXXXXXXXX)
    virtual_url: Optional[str] = None  # Zoom/Teams/Meet URL
    meeting_id: Optional[str] = None  # Zoom meeting ID
    is_hybrid: Optional[bool] = None  # In-person + virtual
    is_virtual_only: Optional[bool] = None  # Virtual only
    streaming_urls: Optional[List[StreamingUrl]] = None  # YouTube, cable, etc.
    public_comment_deadline: Optional[str] = None  # ISO datetime for comment deadline
    instructions: Optional[str] = None  # Human-readable participation instructions


class CityParticipation(BaseModel):
    """
    Typed JSONB for jurisdictions.participation field.

    Jurisdiction-level participation config for centralized testimony processes.
    When set, replaces meeting-level participation for testimony/contact info.
    Cities like NYC have a single testimony portal for all committees.
    """
    model_config = ConfigDict(extra="ignore")  # Allow future fields without breaking

    testimony_url: Optional[str] = None    # Portal to submit testimony (council.nyc.gov/testify/)
    testimony_email: Optional[str] = None  # Official testimony email (testimony@council.nyc.gov)
    process_url: Optional[str] = None      # Link to "how to testify" instructions


class MatterMetadata(BaseModel):
    """
    Typed JSONB for city_matters.metadata field.

    Contains attachment_hash for change detection and deduplication.
    """
    model_config = ConfigDict(extra="forbid")
    attachment_hash: Optional[str] = None


class AttachmentInfo(BaseModel):
    """
    Typed JSONB for attachments arrays (items.attachments, city_matters.attachments).

    Mirrors vendors.schemas.AttachmentSchema but used for DB fields.

    URL semantics: `url` is whatever the vendor handed us at scrape time --
    for CivicClerk and similar Azure-backed vendors, that's a SAS-signed blob URL
    with a finite lifetime. Treat it as ephemeral. The vendor-specific identifiers
    below (cc_agenda_id, cc_attachment_id, history_id, meta_id) are the durable
    references; `pipeline.url_refresh` resolves them to fresh URLs at fetch time.
    """
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str
    type: str  # pdf, doc, spreadsheet, unknown
    portal_url: Optional[str] = None  # Stable viewer URL (CivicClerk portal) -- HTML, not a download
    history_id: Optional[str] = None  # PrimeGov-specific
    meta_id: Optional[str] = None  # Granicus-specific
    cc_agenda_id: Optional[int] = None  # CivicClerk Meetings/{id} -- input to URL refresh
    cc_attachment_id: Optional[int] = None  # CivicClerk attachment id within agenda


# --- Domain Dataclasses (with runtime validation) ---


@dataclass
class Jurisdiction:
    """Jurisdiction entity - city, county, utility board, transit authority, etc."""

    banana: str  # Primary key: paloaltoCA, alameda-countyCA, bartCA
    name: str  # Palo Alto, Alameda County, BART
    state: str  # CA
    vendor: str  # primegov, legistar, granicus, etc.
    slug: str  # cityofpaloalto (vendor-specific)
    extra_vendors: Optional[List[Dict[str, str]]] = None  # Additive [{vendor, slug}] for multi-vendor jurisdictions; NULL for 99% of rows
    type: Optional[str] = None  # city, county, school_district, utility, transit, water_district, etc.
    county_banana: Optional[str] = None  # FK to parent county jurisdiction
    status: str = "active"
    population: Optional[int] = None  # Census population
    participation: Optional[CityParticipation] = None
    zipcodes: Optional[List[str]] = None  # Associated ZIP codes
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        if self.participation:
            data["participation"] = self.participation.model_dump(exclude_none=True)
        return data


@dataclass
class Meeting:
    """Meeting entity with optional summary

    URL Architecture (ONE OR THE OTHER):
    - agenda_url: HTML page to view (item-based meetings with extracted items)
    - packet_url: PDF file to download (monolithic meetings, fallback processing)
    """

    id: str  # Unique meeting ID
    banana: str  # Foreign key to City
    title: str
    date: Optional[datetime]
    agenda_url: Optional[str] = None  # HTML agenda page (item-based, primary)
    agenda_sources: Optional[List[Dict[str, str]]] = None  # [{type, url, label}] for provenance
    packet_url: Optional[
        str | List[str]
    ] = None  # PDF packet (monolithic, fallback)
    summary: Optional[str] = None
    participation: Optional[ParticipationInfo] = None  # Contact info: email, phone, virtual_url, etc.
    status: Optional[str] = (
        None  # cancelled, postponed, revised, rescheduled, or None for normal
    )
    topics: Optional[List[str]] = None  # Aggregated topics from agenda items
    processing_status: str = "pending"  # pending, processing, completed, failed
    processing_method: Optional[str] = (
        None  # tier1_pypdf2_gemini, multiple_pdfs_N_combined
    )
    processing_time: Optional[float] = None
    committee_id: Optional[str] = None  # FK to committees (a meeting is an occurrence of a committee)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        """Validate meeting data after initialization"""
        # Validate processing_status enum (Pydantic doesn't enforce sets automatically)
        valid_statuses = {"pending", "processing", "completed", "failed"}
        if self.processing_status not in valid_statuses:
            from exceptions import ValidationError
            raise ValidationError(
                f"Invalid processing_status: {self.processing_status}. Must be one of: {valid_statuses}",
                field="processing_status",
                value=self.processing_status
            )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.date:
            data["date"] = self.date.isoformat()
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()

        # Map status → meeting_status for frontend compatibility
        if "status" in data:
            data["meeting_status"] = data.pop("status")

        return data



@dataclass
class Matter:
    """Matter entity - legislative items tracked across meetings

    Matters-First Architecture (Nov 2025):
    Matters are the fundamental unit - tracked across time, committees, and meetings.
    Each matter has a canonical summary that's reused when attachments don't change.

    Identity:
    - id: Unique composite key (banana_matter_key, e.g., "nashvilleTN_25-1234")
    - matter_file: Official public ID (25-1234, BL2025-1098)
    - matter_id: Backend vendor ID (UUID, numeric, etc.)

    Deduplication:
    - canonical_summary: Deduplicated summary stored once
    - canonical_topics: Topics extracted from canonical summary
    - metadata: Contains attachment_hash for change detection
    """

    id: str  # Composite key: {banana}_{matter_key}
    banana: str  # Foreign key to City
    matter_file: Optional[str] = None  # Official public identifier (25-1234, BL2025-1098)
    matter_id: Optional[str] = None  # Backend vendor identifier (UUID, numeric)
    matter_type: Optional[str] = None  # Ordinance, Resolution, etc.
    title: Optional[str] = None  # Matter title
    sponsors: Optional[List[str]] = None  # Sponsor names
    canonical_summary: Optional[str] = None  # Deduplicated summary
    canonical_topics: Optional[List[str]] = None  # Extracted topics
    first_seen: Optional[datetime] = None  # First appearance date
    last_seen: Optional[datetime] = None  # Most recent appearance date
    appearance_count: int = 1  # Number of appearances across meetings
    status: str = "active"  # Matter status
    attachments: Optional[List[AttachmentInfo]] = None  # Attachment metadata
    metadata: Optional[MatterMetadata] = None  # attachment_hash, etc.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    final_vote_date: Optional[datetime] = None  # Terminal vote date
    quality_score: Optional[float] = None  # Denormalized from ratings
    rating_count: int = 0  # Denormalized from ratings

    def __post_init__(self):
        """Validate matter data after initialization"""
        # Validate matter ID format (composite: banana_hash)
        from database.id_generation import validate_matter_id
        if not validate_matter_id(self.id):
            from exceptions import ValidationError
            raise ValidationError(
                f"Invalid matter ID format: {self.id}. Must use generate_matter_id()",
                field="id",
                value=self.id
            )

        # Validate banana format (lowercase alphanumeric + state uppercase)
        if not self.banana:
            from exceptions import ValidationError
            raise ValidationError(
                "Matter must have a banana (city identifier)",
                field="banana",
                value=self.banana
            )

        # Validate appearance_count is positive
        if self.appearance_count < 1:
            from exceptions import ValidationError
            raise ValidationError(
                "Matter appearance_count must be at least 1",
                field="appearance_count",
                value=self.appearance_count
            )

        # Validate at least one identifier present (matter_file, matter_id, or title)
        # Title-based matters are valid for cities without stable vendor IDs
        if not self.matter_file and not self.matter_id and not self.title:
            from exceptions import ValidationError
            raise ValidationError(
                "Matter must have at least one identifier (matter_file, matter_id, or title)",
                field="matter_file",
                value=None
            )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        return data

@dataclass
class CouncilMember:
    """Council member entity - elected officials tracked across matters

    Normalizes sponsor data from city_matters.sponsors JSONB arrays.
    ID includes city_banana to prevent cross-city collisions.
    """

    id: str  # Hash of (banana + normalized_name)
    banana: str  # Foreign key to City
    name: str  # Display name as extracted from vendor
    normalized_name: str  # Lowercase, trimmed for matching
    title: Optional[str] = None  # Role: Council Member, Mayor, Alderman
    district: Optional[str] = None  # Ward/district number
    status: str = "active"  # active, former, unknown
    first_seen: Optional[datetime] = None  # First activity (sponsor or vote)
    last_seen: Optional[datetime] = None  # Most recent activity
    sponsorship_count: int = 0  # Denormalized for quick stats
    vote_count: int = 0  # Denormalized count of votes cast
    metadata: Optional[dict] = None  # Vendor-specific fields
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        if self.first_seen:
            data["first_seen"] = self.first_seen.isoformat()
        if self.last_seen:
            data["last_seen"] = self.last_seen.isoformat()
        return data


@dataclass
class Vote:
    """Vote entity - individual voting record per member per matter per meeting

    Tracks how each council member voted on each matter at each meeting.
    Same matter may be voted on multiple times (readings, amendments).
    """

    id: Optional[int] = None  # BIGSERIAL primary key
    council_member_id: str = ""  # FK to council_members
    matter_id: str = ""  # FK to city_matters
    meeting_id: str = ""  # FK to meetings (critical: votes are per-meeting)
    vote: str = ""  # yes, no, abstain, absent, present, recused, not_voting
    vote_date: Optional[datetime] = None  # Date of vote (usually meeting date)
    sequence: Optional[int] = None  # Order in roll call
    metadata: Optional[dict] = None  # Vendor-specific (motion_id, etc.)
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.vote_date:
            data["vote_date"] = self.vote_date.isoformat()
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        return data


@dataclass
class Committee:
    """Committee entity - legislative bodies (Planning Commission, Budget Committee, etc.)

    Tracks committee roster and enables committee-level vote analysis.
    ID includes city_banana to prevent cross-city collisions.
    """

    id: str  # Hash of (banana + normalized_name)
    banana: str  # Foreign key to City
    name: str  # Display name: "Planning Commission", "Budget Committee"
    normalized_name: str  # Lowercase for matching
    description: Optional[str] = None
    status: str = "active"  # active, inactive, unknown
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        return data


@dataclass
class CommitteeMember:
    """Committee member entity - tracks which council members serve on which committees

    Historical tracking via joined_at/left_at enables time-aware queries
    (e.g., "who was on Planning Commission when matter X was voted?").
    """

    id: Optional[int] = None  # BIGSERIAL primary key
    committee_id: str = ""  # FK to committees
    council_member_id: str = ""  # FK to council_members
    role: Optional[str] = None  # Chair, Vice-Chair, Member
    joined_at: Optional[datetime] = None  # When they joined this committee
    left_at: Optional[datetime] = None  # NULL = currently serving
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.joined_at:
            data["joined_at"] = self.joined_at.isoformat()
        if self.left_at:
            data["left_at"] = self.left_at.isoformat()
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        return data


@dataclass
class AgendaItem:
    """Agenda item entity - individual items within a meeting

    Matter Tracking (Nov 2025):
    Unified schema across all vendors (Legistar, PrimeGov, Granicus, etc.):
    - matter_id: Backend unique identifier (UUID, numeric ID, etc.)
    - matter_file: Official public-facing identifier (BL2025-1005, 25-1209, etc.)
    - matter_type: Flexible metadata (Ordinance, Resolution, CD 12, etc.)
    - agenda_number: Position on THIS specific agenda (1, K. 87, etc.)
    - sponsors: List of sponsor names (when available)

    Items can point to Matter objects via matter_file/matter_id.
    When a Matter has a canonical_summary, items inherit it.

    Not all vendors provide all fields - that's expected and fine.
    """

    id: str  # Vendor-specific item ID
    meeting_id: str  # Foreign key to Meeting
    title: str
    sequence: int  # Order in agenda
    attachments: Optional[List[AttachmentInfo]] = None  # Attachment metadata (name, url, type)
    attachment_hash: Optional[str] = None  # SHA-256 hash of attachments for change detection
    body_text: Optional[str] = None  # Coversheet/detail page text (when no PDF attachments)
    matter_id: Optional[str] = None  # Backend unique identifier
    matter_file: Optional[str] = None  # Official public identifier (BL2025-1005, 25-1209)
    matter_type: Optional[str] = None  # Flexible metadata (Ordinance, CD 12, etc.)
    agenda_number: Optional[str] = None  # Position on this agenda
    sponsors: Optional[List[str]] = None  # Sponsor names
    summary: Optional[str] = None
    topics: Optional[List[str]] = None  # Extracted topics
    matter: Optional["Matter"] = None  # Linked Matter object (loaded on demand)
    created_at: Optional[datetime] = None
    quality_score: Optional[float] = None  # Denormalized from ratings
    rating_count: int = 0  # Denormalized from ratings
    filter_reason: Optional[str] = None  # procedural, ceremonial, administrative, or None

    def __post_init__(self):
        """Validate agenda item data after initialization"""
        # Validate matter_id format if present (composite: banana_hash)
        if self.matter_id:
            from database.id_generation import validate_matter_id
            if not validate_matter_id(self.matter_id):
                from exceptions import ValidationError
                raise ValidationError(
                    f"Invalid matter_id format: {self.matter_id}",
                    field="matter_id",
                    value=self.matter_id
                )

        # Validate sequence is non-negative
        if self.sequence < 0:
            from exceptions import ValidationError
            raise ValidationError(
                "Agenda item sequence must be non-negative",
                field="sequence",
                value=self.sequence
            )

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization"""
        data = asdict(self)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()

        # Handle matter field: include if loaded, exclude if None
        if "matter" in data and data["matter"] is not None:
            # Matter was eagerly loaded - serialize it
            data["matter"] = self.matter.to_dict() if self.matter else None
        elif "matter" in data:
            # Matter is None - exclude from response
            del data["matter"]

        return data

