"""
ID Generation - Deterministic identifier generation for civic entities

Single source of truth for ALL ID generation. No adapter generates final IDs.

Entity ID Patterns:
- Meeting ID: {banana}_{8-char-md5} - e.g., "chicagoIL_a3f2c1d4"
- Matter ID: {banana}_{16-char-sha256} - e.g., "nashvilleTN_7a8f3b2c1d9e4f5a"
- Item ID: {meeting_id}_{suffix} - e.g., "chicagoIL_a3f2c1d4_ord2024-123"
- Council Member ID: {banana}_cm_{16-char-sha256}
- Committee ID: {banana}_comm_{16-char-sha256}

Design Philosophy:
- IDs are deterministic: same inputs always produce same ID
- IDs are unique: hash collision probability is negligible
- IDs are hierarchical: items namespaced under meetings
- Original data preserved: store vendor IDs in separate columns

Item ID Fallback Hierarchy:
1. vendor_item_id - Vendor's stable identifier (EventItemId, legifile number, etc.)
2. sequence - Position-based fallback for vendors without stable item IDs

Matter ID Fallback Hierarchy:
1. matter_file - Public legislative file number (Legistar, LA-style PrimeGov)
2. matter_id - Backend vendor identifier if stable
3. title (normalized) - For cities without stable vendor IDs (Palo Alto-style PrimeGov)
"""

import hashlib
import re
from datetime import datetime
from typing import Optional


def _strip_reading_prefixes(text: str) -> str:
    """Strip reading prefixes from ordinance/resolution titles

    Handles variations: "FIRST READ:", "FIRST READING:", "REINTRODUCED FIRST READING:", etc.
    This allows different readings of the same ordinance to be identified as the same matter.

    Args:
        text: Title text that may contain reading prefix

    Returns:
        Text with reading prefix removed

    Examples:
        >>> _strip_reading_prefixes("FIRST READING: Ordinance 2025-123")
        'Ordinance 2025-123'

        >>> _strip_reading_prefixes("REINTRODUCED SECOND READ: Resolution")
        'Resolution'
    """
    reading_prefixes = [
        r'^FIRST\s+READ(?:ING)?:\s*',
        r'^SECOND\s+READ(?:ING)?:\s*',
        r'^THIRD\s+READ(?:ING)?:\s*',
        r'^FINAL\s+READ(?:ING)?:\s*',
        r'^REINTRODUCED\s+(?:FIRST\s+)?(?:SECOND\s+)?READ(?:ING)?:\s*',
    ]

    result = text
    for pattern in reading_prefixes:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)

    return result


def normalize_title_for_matter_id(title: str) -> Optional[str]:
    """Normalize title for matter identification (title-based fallback)

    Used when cities lack stable vendor IDs (e.g., Palo Alto-style PrimeGov).
    Strips reading prefixes, normalizes whitespace/case, excludes generic titles.

    Args:
        title: Raw agenda item title

    Returns:
        Normalized title string for hashing, or None if title should NOT be deduplicated

    Examples:
        >>> normalize_title_for_matter_id("FIRST READING: Ordinance 2025-123...")
        'ordinance 2025-123...'

        >>> normalize_title_for_matter_id("Public Comment")
        None  # Generic title, always unique per meeting

        >>> normalize_title_for_matter_id("Approval of Budget Amendments...")
        'approval of budget amendments...'

    Exclusion Rules:
        - Generic titles (<30 chars or in exclusion list) return None
        - These items are always processed individually (no deduplication)
        - Examples: "Public Comment", "Staff Comments", "VTA"

    Normalization Rules:
        - Strip reading prefixes (FIRST/SECOND/THIRD/FINAL READING, REINTRODUCED)
        - Collapse whitespace to single spaces
        - Convert to lowercase
        - Preserve special characters (parentheses, dashes, etc.)

    Design:
        - Conservative: When in doubt, exclude (false negatives > false positives)
        - City-agnostic: Works across PrimeGov implementations
        - Robust: Handles inconsistent prefix formatting

    Confidence: 8/10
    - Works well for substantive titles (ordinances, resolutions, contracts)
    - May need city-specific tuning for exclusion list
    - 30-char threshold is empirically derived from Palo Alto data
    """
    if not title or not title.strip():
        return None

    # Normalize whitespace early (helps with length check)
    normalized = re.sub(r'\s+', ' ', title.strip())

    # Exclude very short titles (likely generic/procedural)
    if len(normalized) < 30:
        return None

    # Generic title exclusion list (case-insensitive exact match)
    # Based on Palo Alto data analysis (10.3% of items)
    generic_titles = {
        "vta",
        "caltrain",
        "city staff",
        "public comment",
        "public letters",
        "staff comments",
        "future business",
        "review of minutes",
        "city and district reports",
        "open forum",
        "closed session",
        "oral communications",
    }

    if normalized.lower() in generic_titles:
        return None

    # Strip reading prefixes (ordinance lifecycle tracking)
    normalized = _strip_reading_prefixes(normalized)

    # Final normalization
    normalized = re.sub(r'\s+', ' ', normalized.strip().lower())

    # After stripping prefix, check length again (edge case: "FIRST READING: VTA")
    if len(normalized) < 30:
        return None

    return normalized


def generate_matter_id(
    banana: str,
    matter_file: Optional[str] = None,
    matter_id: Optional[str] = None,
    title: Optional[str] = None
) -> Optional[str]:
    """Generate deterministic matter ID from inputs with strict fallback hierarchy

    Fallback hierarchy (most stable to least):
    1. matter_file ALONE - Public legislative file number (ignores matter_id)
    2. matter_id ALONE - Backend vendor identifier (only when no matter_file)
    3. title - Normalized title (Palo Alto-style PrimeGov fallback)

    IMPORTANT: When matter_file exists, matter_id is IGNORED. Vendors often
    create new backend IDs per agenda appearance while matter_file stays stable.

    Args:
        banana: City identifier (e.g., "sanfranciscoCA")
        matter_file: Official public identifier (e.g., "251041", "BL2025-1098")
        matter_id: Backend vendor identifier (e.g., UUID, numeric)
        title: Agenda item title (fallback for cities without stable IDs)

    Returns:
        Composite ID: {banana}_{hash} where hash is first 16 chars of SHA256
        Returns None if title is provided but excluded (generic item)

    Examples:
        >>> generate_matter_id("nashvilleTN", matter_file="BL2025-1098")
        'nashvilleTN_...'  # Uses matter_file only

        >>> generate_matter_id("losangelesCA", matter_file="25-0583", matter_id="abc-123")
        # Same as above with different matter_id - matter_id IGNORED

        >>> generate_matter_id("paloaltoCA", matter_id="fb36db52-...")
        'paloaltoCA_...'  # Uses matter_id (no matter_file available)

        >>> generate_matter_id("paloaltoCA", title="Ordinance 2025-123")
        'paloaltoCA_...'  # Uses normalized title

    Notes:
        - At least one of matter_file, matter_id, or title must be provided
        - matter_file takes absolute precedence (matter_id ignored when present)
        - Same inputs always produce same ID (determinism)
        - Generic titles return None (caller should generate unique ID)
    """
    # Strict hierarchy: matter_file > matter_id > title
    # Each level is independent - no mixing
    if matter_file:
        key = f"{banana}:file:{matter_file}"
    elif matter_id:
        key = f"{banana}:id:{matter_id}"
    elif title:
        # NEW: Title-based fallback for cities without stable vendor IDs
        normalized = normalize_title_for_matter_id(title)
        if normalized is None:
            # Generic title - caller should generate unique ID
            return None
        # Use distinct prefix to avoid collision with vendor IDs
        key = f"{banana}:title:{normalized}"
    else:
        raise ValueError(
            "At least one of matter_file, matter_id, or title must be provided"
        )

    # Hash the key
    hash_bytes = hashlib.sha256(key.encode('utf-8')).digest()
    hash_hex = hash_bytes.hex()[:16]  # Use first 16 hex chars (64 bits)

    # Return composite ID
    return f"{banana}_{hash_hex}"


def validate_matter_id(matter_id: str) -> bool:
    """Validate matter ID format

    Args:
        matter_id: Matter ID to validate

    Returns:
        True if valid format, False otherwise

    Valid format: {banana}_{16-char-hex}
    Example: "nashvilleTN_7a8f3b2c1d9e4f5a"
    """
    if not matter_id:
        return False

    parts = matter_id.split('_')
    if len(parts) != 2:
        return False

    banana, hash_part = parts

    # Banana should be alphanumeric
    if not banana.isalnum():
        return False

    # Hash should be 16 hex characters
    if len(hash_part) != 16:
        return False

    try:
        int(hash_part, 16)  # Verify it's valid hex
        return True
    except ValueError:
        return False


def extract_banana_from_matter_id(matter_id: str) -> Optional[str]:
    """Extract banana from matter ID

    Args:
        matter_id: Matter ID (e.g., "nashvilleTN_7a8f3b2c1d9e4f5a")

    Returns:
        Banana string or None if invalid format
    """
    if not validate_matter_id(matter_id):
        return None

    return matter_id.split('_')[0]


def matter_ids_match(
    banana: str,
    matter_file_1: Optional[str],
    matter_id_1: Optional[str],
    matter_file_2: Optional[str],
    matter_id_2: Optional[str]
) -> bool:
    """Check if two sets of matter identifiers refer to same matter

    Useful for detecting duplicates when identifiers may differ slightly
    but represent the same legislative item.

    Args:
        banana: City identifier
        matter_file_1: First matter's public ID
        matter_id_1: First matter's backend ID
        matter_file_2: Second matter's public ID
        matter_id_2: Second matter's backend ID

    Returns:
        True if they generate the same composite ID
    """
    try:
        id1 = generate_matter_id(banana, matter_file_1, matter_id_1)
        id2 = generate_matter_id(banana, matter_file_2, matter_id_2)
        return id1 == id2
    except ValueError:
        return False


def generate_meeting_id(
    banana: str,
    vendor_id: str,
    date: Optional[datetime],
    title: str
) -> str:
    """Generate deterministic meeting ID from inputs.

    All adapters use this single function. No fallback hierarchy.

    Args:
        banana: City identifier (e.g., "paloaltoCA")
        vendor_id: Vendor's native meeting ID (EventId, UUID, extracted from URL, etc.)
        date: Authoritative meeting datetime, or ``None`` when undated
        title: Meeting title

    Returns:
        Composite ID: {banana}_{8-char-md5-hash}
        Example: "chicagoIL_a3f2c1d4"

    Examples:
        >>> from datetime import datetime
        >>> generate_meeting_id("chicagoIL", "12345", datetime(2025, 1, 15, 10, 0), "City Council")
        'chicagoIL_...'

    Confidence: 9/10 - Deterministic, collision-resistant for practical use
    """
    if not banana or not vendor_id or not title:
        raise ValueError("Required arguments: banana, vendor_id, title")

    # Never invent wall-clock input for an undated source meeting. A stable
    # sentinel keeps repeated syncs on one aggregate until the source publishes
    # a real date, at which point the explicit identity transition is visible.
    date_str = date.strftime("%Y%m%dT%H%M%S") if date else "undated"
    key = f"{banana}:{vendor_id}:{date_str}:{title}"
    hash_hex = hashlib.md5(key.encode()).hexdigest()[:8]
    return f"{banana}_{hash_hex}"


def validate_meeting_id(meeting_id: str) -> bool:
    """Validate meeting ID format.

    Valid format: {banana}_{8-char-hex}
    Example: "chicagoIL_a3f2c1d4"
    """
    if not meeting_id:
        return False

    parts = meeting_id.split('_')
    if len(parts) != 2:
        return False

    banana, hash_part = parts

    if not banana.isalnum():
        return False

    if len(hash_part) != 8:
        return False

    try:
        int(hash_part, 16)
        return True
    except ValueError:
        return False


def generate_item_id(
    meeting_id: str,
    sequence: int,
    vendor_item_id: Optional[str] = None
) -> str:
    """Generate deterministic item ID. Single source of truth for all adapters.

    All adapters now return raw vendor_item_id (or None). The orchestrator
    calls this function to generate the final item ID.

    Fallback hierarchy:
    1. vendor_item_id - Vendor's stable identifier (EventItemId, legifile number, etc.)
    2. sequence - Position in agenda (1-indexed)

    Args:
        meeting_id: Parent meeting ID (from generate_meeting_id)
        sequence: Item position in agenda (1-indexed, used if no vendor_item_id)
        vendor_item_id: Vendor's raw item identifier (preferred if stable)

    Returns:
        Composite ID: {meeting_id}_{normalized_vendor_id} or {meeting_id}_seq{NNN}_{hash}

    Examples:
        >>> generate_item_id("chicagoIL_a3f2c1d4", 1, "ORD2024-123")
        'chicagoIL_a3f2c1d4_ord2024-123'

        >>> generate_item_id("chicagoIL_a3f2c1d4", 3, None)
        'chicagoIL_a3f2c1d4_seq003_8a7b6c5d'

        >>> generate_item_id("chicagoIL_a3f2c1d4", 1, "12345")
        'chicagoIL_a3f2c1d4_12345'

    Design:
        - Deterministic: Same inputs always produce same ID
        - Prefixed: Items are namespaced under their meeting
        - Stable: Vendor IDs preferred when available (legislative file numbers)
        - Fallback: Sequence-based for vendors without stable item IDs

    Confidence: 8/10
    - Vendor IDs are stable when available (Legistar EventItemId, legifile numbers)
    - Sequence fallback assumes agenda order is stable between syncs
    - Some vendors may reorder items; sequence changes would orphan old records
    """
    if not meeting_id:
        raise ValueError("meeting_id is required")
    if sequence < 1:
        raise ValueError("sequence must be >= 1")

    if vendor_item_id and vendor_item_id.strip():
        # Normalize: strip whitespace, collapse internal spaces, lowercase
        normalized = re.sub(r'\s+', '', vendor_item_id.strip().lower())
        if normalized:
            return f"{meeting_id}_{normalized}"

    # Fallback: deterministic hash from meeting + sequence (used when vendor_item_id is None, empty, or whitespace-only)
    stable_key = f"{meeting_id}:seq:{sequence}"
    hash_suffix = hashlib.sha256(stable_key.encode()).hexdigest()[:8]
    return f"{meeting_id}_seq{sequence:03d}_{hash_suffix}"


def validate_item_id(item_id: str) -> bool:
    """Validate item ID format.

    Item IDs are composites: {meeting_id}_{item_suffix}
    where meeting_id is {banana}_{8-char-hex}

    Valid formats:
        - chicagoIL_a3f2c1d4_ord2024-123 (vendor ID)
        - chicagoIL_a3f2c1d4_seq003_8a7b6c5d (sequence fallback)
        - chicagoIL_a3f2c1d4_12345 (numeric vendor ID)

    Args:
        item_id: Item ID to validate

    Returns:
        True if valid format, False otherwise
    """
    if not item_id:
        return False

    # Must have at least 3 parts: banana, meeting_hash, item_suffix
    parts = item_id.split('_')
    if len(parts) < 3:
        return False

    # First two parts should form a valid meeting ID
    banana = parts[0]
    meeting_hash = parts[1]

    if not banana.isalnum():
        return False

    if len(meeting_hash) != 8:
        return False

    try:
        int(meeting_hash, 16)
    except ValueError:
        return False

    # Remaining parts form the item suffix (must exist)
    item_suffix = '_'.join(parts[2:])
    return bool(item_suffix)


def extract_meeting_id_from_item_id(item_id: str) -> Optional[str]:
    """Extract parent meeting ID from item ID.

    Args:
        item_id: Item ID (e.g., "chicagoIL_a3f2c1d4_ord2024-123")

    Returns:
        Meeting ID or None if invalid format
    """
    if not validate_item_id(item_id):
        return None

    parts = item_id.split('_')
    return f"{parts[0]}_{parts[1]}"


def hash_meeting_id(meeting_id: str) -> str:
    """Generate deterministic hash from meeting ID for URL slugs.

    CRITICAL: Must match frontend hashMeetingId() in utils.ts!

    Algorithm: SHA-256 -> hex -> first 16 chars

    Args:
        meeting_id: Meeting ID (can contain dashes, UUIDs, etc.)

    Returns:
        16-character hex hash

    Confidence: 10/10 - Standard crypto hash
    """
    hash_bytes = hashlib.sha256(meeting_id.encode('utf-8')).digest()
    return hash_bytes.hex()[:16]


# Confidence level: 9/10
# This hashing scheme is deterministic and collision-resistant.
# SHA256 provides 2^128 combinations with 16 hex chars, far exceeding
# the number of matters we'll ever track (millions at most).
# The only edge case is if a city changes their matter ID scheme mid-year,
# but that would break their own systems too, so unlikely.


def normalize_sponsor_name(name: str) -> str:
    """Normalize sponsor name for council member matching

    Handles vendor variations like:
    - "John Smith" vs "JOHN SMITH" vs "Smith, John"
    - "Council Member Smith" vs "CM Smith" vs "Smith"
    - Leading/trailing whitespace, multiple spaces

    Args:
        name: Raw sponsor name from vendor data

    Returns:
        Lowercase, trimmed, collapsed whitespace name

    Examples:
        >>> normalize_sponsor_name("  John   Smith  ")
        'john smith'

        >>> normalize_sponsor_name("SMITH, JOHN")
        'smith, john'
    """
    if not name:
        return ""

    # Collapse whitespace and lowercase
    normalized = re.sub(r'\s+', ' ', name.strip().lower())

    return normalized


def generate_council_member_id(banana: str, name: str) -> str:
    """Generate deterministic council member ID

    Uses same pattern as generate_matter_id() for consistency.
    ID includes city_banana to prevent cross-city collisions.

    Args:
        banana: City identifier (e.g., "chicagoIL")
        name: Council member name (will be normalized)

    Returns:
        Composite ID: {banana}_cm_{16-char-hex}

    Examples:
        >>> generate_council_member_id("chicagoIL", "John Smith")
        'chicagoIL_cm_a1b2c3d4e5f6g7h8'

        >>> generate_council_member_id("chicagoIL", "SMITH, JOHN")
        'chicagoIL_cm_a1b2c3d4e5f6g7h8'  # Different name, different hash
    """
    if not banana or not name:
        raise ValueError("Both banana and name are required")

    normalized = normalize_sponsor_name(name)
    if not normalized:
        raise ValueError("Name cannot be empty after normalization")

    key = f"{banana}:council_member:{normalized}"
    hash_bytes = hashlib.sha256(key.encode('utf-8')).digest()
    hash_hex = hash_bytes.hex()[:16]

    return f"{banana}_cm_{hash_hex}"


def validate_council_member_id(member_id: str) -> bool:
    """Validate council member ID format

    Args:
        member_id: Council member ID to validate

    Returns:
        True if valid format, False otherwise

    Valid format: {banana}_cm_{16-char-hex}
    Example: "chicagoIL_cm_7a8f3b2c1d9e4f5a"
    """
    if not member_id:
        return False

    parts = member_id.split('_')
    if len(parts) != 3:
        return False

    banana, prefix, hash_part = parts

    # Banana should be alphanumeric
    if not banana.isalnum():
        return False

    # Prefix must be "cm"
    if prefix != "cm":
        return False

    # Hash should be 16 hex characters
    if len(hash_part) != 16:
        return False

    try:
        int(hash_part, 16)
        return True
    except ValueError:
        return False


def normalize_committee_name(name: str) -> str:
    """Normalize committee name for consistent matching

    Args:
        name: Committee name (e.g., "Planning Commission", "budget committee")

    Returns:
        Normalized lowercase name with standardized whitespace

    Examples:
        >>> normalize_committee_name("  Planning Commission  ")
        'planning commission'
        >>> normalize_committee_name("BUDGET  COMMITTEE")
        'budget committee'
    """
    if not name:
        return ""

    # Strip and collapse whitespace
    normalized = " ".join(name.split())

    # Lowercase for consistent matching
    return normalized.lower()


def generate_committee_id(banana: str, name: str) -> str:
    """Generate deterministic committee ID

    Uses same pattern as generate_council_member_id() for consistency.
    ID includes city_banana to prevent cross-city collisions.

    Args:
        banana: City identifier (e.g., "chicagoIL")
        name: Committee name (will be normalized)

    Returns:
        Composite ID: {banana}_comm_{16-char-hex}

    Examples:
        >>> generate_committee_id("chicagoIL", "Planning Commission")
        'chicagoIL_comm_a1b2c3d4e5f6g7h8'

        >>> generate_committee_id("chicagoIL", "PLANNING COMMISSION")
        'chicagoIL_comm_a1b2c3d4e5f6g7h8'  # Same hash (normalized)
    """
    if not banana or not name:
        raise ValueError("Both banana and name are required")

    normalized = normalize_committee_name(name)
    if not normalized:
        raise ValueError("Name cannot be empty after normalization")

    key = f"{banana}:committee:{normalized}"
    hash_bytes = hashlib.sha256(key.encode('utf-8')).digest()
    hash_hex = hash_bytes.hex()[:16]

    return f"{banana}_comm_{hash_hex}"


def validate_committee_id(committee_id: str) -> bool:
    """Validate committee ID format

    Args:
        committee_id: Committee ID to validate

    Returns:
        True if valid format, False otherwise

    Valid format: {banana}_comm_{16-char-hex}
    Example: "chicagoIL_comm_7a8f3b2c1d9e4f5a"
    """
    if not committee_id:
        return False

    parts = committee_id.split('_')
    if len(parts) != 3:
        return False

    banana, prefix, hash_part = parts

    # Banana should be alphanumeric
    if not banana.isalnum():
        return False

    # Prefix must be "comm"
    if prefix != "comm":
        return False

    # Hash should be 16 hex characters
    if len(hash_part) != 16:
        return False

    try:
        int(hash_part, 16)
        return True
    except ValueError:
        return False
