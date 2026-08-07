"""
Pipeline Utilities - Shared helper functions

Contains utilities used across pipeline modules for matters-first processing.
"""

import hashlib
import json
import re
from dataclasses import dataclass

import requests
from datetime import datetime
from typing import List, Dict, Any, Iterable, Literal, Optional, TypeAlias
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from config import get_logger

logger = get_logger(__name__).bind(component="engagic")

# Version tag prefixed onto attachment hashes ("sv1:<hex>"). Bump whenever the
# hash inputs change (identity rules, substantive filter, pair shape) so a
# stored hash can never silently compare equal against a differently-computed
# one. The matter enqueue decider keeps a legacy (untagged) comparison path so
# pre-sv1 stored hashes from stable-URL vendors don't trigger a reprocess
# wave; stored values upgrade to the current format on the next
# confirmed-unchanged sync or successful matter job.
ATTACHMENT_HASH_VERSION = "sv1"
MATTER_WORK_VERSION = "mw1"
MATTER_NO_WORK_VERSION = "mnw1"
MEETING_WORK_VERSION = "mv1"

MatterNoWorkReason: TypeAlias = Literal[
    "procedural",
    "no_appearances",
    "no_substantive_work",
]
_MATTER_NO_WORK_REASONS = frozenset(
    {"procedural", "no_appearances", "no_substantive_work"}
)

# Query keys that mark a signed URL (Azure SAS / S3 presigned). When any of
# these is present the whole query string is an auth envelope that rotates on
# every scrape (CivicClerk re-signs SAS tokens per API request), so identity
# is the bare scheme://host/path. URLs without these markers keep their query
# verbatim -- for Legistar/Granicus-style vendors the query params ARE the
# identity (View.ashx?ID=...&GUID=..., MetaViewer.php?meta_id=...).
_SIGNED_URL_MARKERS = frozenset({"sig", "x-amz-signature", "signature", "awsaccesskeyid"})


def attachment_identity(url: str) -> str:
    """Stable identity for an attachment URL across re-scrapes.

    Strips the query string iff it carries a recognized signature marker;
    otherwise returns the URL unchanged. Invariant under url_refresh, which
    only swaps the signature portion of signed URLs.
    """
    if not url or "?" not in url:
        return url
    try:
        parsed = urlsplit(url)
        keys = {k.lower() for k, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    except ValueError:
        return url
    if keys & _SIGNED_URL_MARKERS:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return url


def filter_document_version_urls(urls: List[str]) -> List[str]:
    """Keep only the newest ``... VerN`` URL for each versioned document."""
    url_groups: Dict[str, Dict[int, str]] = {}
    non_versioned = []
    version_pattern = re.compile(r"(.+?)\s+Ver(\d+)", re.IGNORECASE)

    for url in urls:
        filename = url.split("/")[-1] if url else ""
        match = version_pattern.search(filename)
        if match:
            base_name = match.group(1).strip()
            version_num = int(match.group(2))
            url_groups.setdefault(base_name, {})[version_num] = url
        else:
            non_versioned.append(url)

    filtered = non_versioned.copy()
    for versions in url_groups.values():
        filtered.append(versions[max(versions)])
    return filtered


def hash_attachments_fast(attachments: List[Any]) -> str:
    """
    Hash attachments using stable URL identity and name only (pure function, no I/O).

    This is the fast path for change detection. Signed-URL query strings are
    stripped via attachment_identity() so the hash is stable across re-scrapes
    and across pre/post-url_refresh states of the same attachments.

    Args:
        attachments: List of AttachmentInfo objects with 'url' and 'name' attrs

    Returns:
        Version-tagged digest "sv1:<sha256 hex>", or empty string if no attachments

    Confidence: 8/10 - Still misses same-URL content edits (no metadata in the
    fast path) and rotations where the path itself changes.
    """
    if not attachments:
        return ""
    pairs = [(attachment_identity(att.url or ""), att.name or "") for att in attachments]
    pairs.sort()
    content = json.dumps(pairs, sort_keys=True)
    return f"{ATTACHMENT_HASH_VERSION}:{hashlib.sha256(content.encode()).hexdigest()}"


def hash_attachments_fast_legacy(attachments: List[Any]) -> str:
    """Byte-exact pre-sv1 algorithm: verbatim (url, name) pairs, no version tag.

    Kept so the matter enqueue decider can recognize stored hashes written
    before signature-stripping landed. For stable-URL vendors a legacy match
    means "unchanged" (only the hash format moved underneath); signed-URL
    vendors never matched under this algorithm anyway, since the stored URL
    carried a rotating signature.
    """
    if not attachments:
        return ""
    pairs = [(att.url or "", att.name or "") for att in attachments]
    pairs.sort()
    content = json.dumps(pairs, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()


def hash_attachments_with_metadata(attachments: List[Any], timeout: int = 3) -> str:
    """
    Hash attachments including HTTP metadata (makes network requests).

    This is the slow path for better change detection. Makes HEAD requests
    to fetch content-length and last-modified headers, which helps detect
    content changes even when URLs stay the same.

    Args:
        attachments: List of AttachmentInfo objects with 'url' and 'name' attrs
        timeout: Timeout for HEAD requests in seconds

    Returns:
        SHA256 hex digest, or empty string if no attachments

    Confidence: 8/10 - Better detection but adds latency
    """
    if not attachments:
        return ""

    tuples = []
    for att in attachments:
        url = att.url or ""
        name = att.name or ""

        identity = attachment_identity(url)

        if not url:
            tuples.append((identity, name, "", ""))
            continue

        # Try to fetch metadata via HEAD request (against the real URL --
        # signed URLs only fetch while the signature is valid)
        try:
            metadata = _fetch_attachment_metadata(url, timeout)
            tuples.append((identity, name, metadata['content_length'], metadata['last_modified']))
        except requests.RequestException as e:
            # Fallback to identity-only if metadata fetch fails
            logger.warning("failed to fetch metadata", url=url, error=str(e))
            tuples.append((identity, name, "", ""))

    tuples.sort()
    content = json.dumps(tuples, sort_keys=True)
    return f"{ATTACHMENT_HASH_VERSION}:{hashlib.sha256(content.encode()).hexdigest()}"


def hash_attachments(
    attachments: List[Any],
    include_metadata: bool = False,
    timeout: int = 3
) -> str:
    """Wrapper for backwards compatibility. Prefer the specific functions."""
    if include_metadata:
        return hash_attachments_with_metadata(attachments, timeout)
    return hash_attachments_fast(attachments)


def hash_substantive_attachments(
    attachments: List[Any],
    include_metadata: bool = False,
    timeout: int = 3
) -> str:
    """
    Hash only substantive attachments, excluding ceremonial ones.

    Filters out public comments, speaker cards, correspondence, community impact
    statements, and similar low-signal attachments before hashing. Used for
    change-detection on matter appearances: two appearances with identical
    substantive attachments should produce the same hash even if one added
    speaker cards between meetings.

    Order-independent like hash_attachments (delegates after filtering).

    Confidence: 8/10 - filter coverage depends on is_public_comment_attachment
    being kept current as new ceremonial patterns emerge.

    WARNING: any change to is_public_comment_attachment (or to
    attachment_identity) changes what every matter hashes to. Hashes are
    version-tagged (ATTACHMENT_HASH_VERSION) precisely so this is explicit:
    bump the version when you change the inputs, and if a reprocess wave is
    unacceptable, teach MatterEnqueueDecider a fallback comparison for the
    outgoing format (see current_attachment_hash_legacy).
    """
    from pipeline.filters.item_filters import is_public_comment_attachment

    if not attachments:
        return ""

    substantive = [
        att for att in attachments
        if not is_public_comment_attachment(att.name or "")
    ]
    return hash_attachments(substantive, include_metadata=include_metadata, timeout=timeout)


def hash_substantive_attachments_legacy(attachments: List[Any]) -> str:
    """Legacy-format counterpart of hash_substantive_attachments (fast path only).

    Computes what hash_substantive_attachments would have produced before
    version tagging and signature-stripping. Used by MatterEnqueueDecider to
    compare against stored hashes written before sv1.
    """
    from pipeline.filters.item_filters import is_public_comment_attachment

    if not attachments:
        return ""

    substantive = [
        att for att in attachments
        if not is_public_comment_attachment(att.name or "")
    ]
    return hash_attachments_fast_legacy(substantive)


def aggregate_matter_attachments(appearances: Iterable[Any]) -> List[Any]:
    """Reproduce the processor's authoritative matter attachment set.

    Appearances are ordered deterministically, then attachments are deduplicated
    by stable source identity. This intentionally matches ``Processor.process_matter``
    before both paths call ``hash_substantive_attachments``. Keeping the
    aggregation contract here prevents sync from comparing one appearance with
    a canonical summary produced from every appearance.
    """
    ordered = sorted(
        appearances,
        key=lambda item: (
            str(getattr(item, "meeting_id", "") or ""),
            int(getattr(item, "sequence", 0) or 0),
            str(getattr(item, "id", "") or ""),
        ),
    )
    attachments: List[Any] = []
    seen_identities: set[str] = set()
    for item in ordered:
        for attachment in (getattr(item, "attachments", None) or []):
            url = getattr(attachment, "url", None)
            identity = attachment_identity(url or "")
            if not identity or identity in seen_identities:
                continue
            seen_identities.add(identity)
            attachments.append(attachment)
    return attachments


@dataclass(frozen=True, slots=True)
class MatterWorkSnapshot:
    """One pure authoritative view shared by sync, workers, and repair tools."""

    appearances: tuple[Any, ...]
    attachments: tuple[Any, ...]
    substantive_attachments: tuple[Any, ...]
    attachment_version: str
    work_version: str
    legacy_attachment_version: str
    normalized_titles: tuple[str, ...]

    @property
    def is_summarizable(self) -> bool:
        return bool(self.substantive_attachments)

    @classmethod
    def from_appearances(cls, appearances: Iterable[Any]) -> "MatterWorkSnapshot":
        from pipeline.filters.item_filters import is_public_comment_attachment

        ordered = tuple(
            sorted(
                appearances,
                key=lambda item: (
                    str(getattr(item, "meeting_id", "") or ""),
                    int(getattr(item, "sequence", 0) or 0),
                    str(getattr(item, "id", "") or ""),
                ),
            )
        )
        attachments = tuple(aggregate_matter_attachments(ordered))
        substantive = tuple(
            attachment
            for attachment in attachments
            if not is_public_comment_attachment(
                str(getattr(attachment, "name", "") or "")
            )
        )
        attachment_version = hash_attachments(list(substantive))
        legacy_attachment_version = hash_attachments_fast_legacy(list(substantive))
        titles = tuple(
            sorted(
                {
                    identity
                    for item in ordered
                    if (identity := matter_title_identity(
                        str(getattr(item, "title", "") or "")
                    ))
                }
            )
        )
        descriptor = {
            "attachment_version": attachment_version,
            "titles": titles,
        }
        encoded = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        work_version = (
            f"{MATTER_WORK_VERSION}:{hashlib.sha256(encoded.encode()).hexdigest()}"
        )
        return cls(
            appearances=ordered,
            attachments=attachments,
            substantive_attachments=substantive,
            attachment_version=attachment_version,
            work_version=work_version,
            legacy_attachment_version=legacy_attachment_version,
            normalized_titles=titles,
        )


def matter_attachment_version(appearances: Iterable[Any]) -> str:
    """Hash the substantive aggregate artifact set behind a projection."""
    return MatterWorkSnapshot.from_appearances(appearances).attachment_version


def matter_title_identity(title: Optional[str]) -> str:
    """Normalize cosmetic reading prefixes without hiding substantive edits."""
    stripped = re.sub(
        r"^(?:(?:REINTRODUCED\s+)?(?:FIRST|SECOND|THIRD|FINAL)\s+"
        r"READ(?:ING)?\s*:\s*)+",
        "",
        title or "",
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", stripped).strip().lower()


def matter_work_version(appearances: Iterable[Any]) -> str:
    """Hash every stable input that can influence a matter summarization."""
    return MatterWorkSnapshot.from_appearances(appearances).work_version


def matter_no_work_version(
    executable_work_version: str,
    reason: MatterNoWorkReason,
) -> str:
    """Return one bounded, deterministic terminal desired-work descriptor.

    A no-work policy decision is material desired state, but it must never
    compare equal to the executable snapshot it suppresses. The distinct
    ``mnw1`` namespace lets an identical procedural -> substantive snapshot
    reopen normally, while the bounded visible reason makes policy changes
    and recurrences deterministic and operationally legible.
    """
    if not isinstance(executable_work_version, str) or not (
        executable_work_version.startswith(f"{MATTER_WORK_VERSION}:")
    ):
        raise ValueError("executable_work_version must be a matter work version")
    if reason not in _MATTER_NO_WORK_REASONS:
        raise ValueError(f"unsupported matter no-work reason: {reason}")
    digest = hashlib.sha256(executable_work_version.encode()).hexdigest()
    return f"{MATTER_NO_WORK_VERSION}:{reason}:{digest}"


def matter_work_version_legacy(appearances: Iterable[Any]) -> str:
    """Pre-sv1 counterpart for upgrading unchanged legacy matter hashes."""
    return MatterWorkSnapshot.from_appearances(
        appearances
    ).legacy_attachment_version


def _stable_input(value: Any) -> Any:
    """Normalize nested meeting inputs for deterministic JSON hashing."""
    if hasattr(value, "model_dump"):
        return _stable_input(value.model_dump(exclude_none=True))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return attachment_identity(value) if value.startswith(("http://", "https://")) else value
    if isinstance(value, dict):
        return {str(key): _stable_input(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple, set)):
        normalized = [_stable_input(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def meeting_work_version(meeting: Any, agenda_items: Iterable[Any]) -> str:
    """Hash all stable inputs that can affect a meeting summarization job."""
    item_inputs = []
    for item in sorted(
        agenda_items,
        key=lambda value: (
            int(getattr(value, "sequence", 0) or 0),
            str(getattr(value, "id", "") or ""),
        ),
    ):
        attachments = [
            {
                "url": attachment_identity(getattr(attachment, "url", "") or ""),
                "name": getattr(attachment, "name", "") or "",
                "type": getattr(attachment, "type", "") or "",
            }
            for attachment in (getattr(item, "attachments", None) or [])
        ]
        attachments.sort(key=lambda value: (value["url"], value["name"], value["type"]))
        item_inputs.append(
            {
                "id": getattr(item, "id", None),
                "sequence": getattr(item, "sequence", None),
                "title": getattr(item, "title", None),
                "body_text": getattr(item, "body_text", None),
                "matter_id": getattr(item, "matter_id", None),
                "matter_file": getattr(item, "matter_file", None),
                "matter_type": getattr(item, "matter_type", None),
                "agenda_number": getattr(item, "agenda_number", None),
                "sponsors": _stable_input(getattr(item, "sponsors", None)),
                "filter_reason": getattr(item, "filter_reason", None),
                "attachments": attachments,
            }
        )

    canonical = {
        "meeting": {
            "id": getattr(meeting, "id", None),
            "title": getattr(meeting, "title", None),
            "date": _stable_input(getattr(meeting, "date", None)),
            "agenda_url": _stable_input(getattr(meeting, "agenda_url", None)),
            "agenda_sources": _stable_input(getattr(meeting, "agenda_sources", None)),
            "packet_url": _stable_input(getattr(meeting, "packet_url", None)),
            "minutes_url": _stable_input(getattr(meeting, "minutes_url", None)),
            "participation": _stable_input(getattr(meeting, "participation", None)),
        },
        "items": item_inputs,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return f"{MEETING_WORK_VERSION}:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _fetch_attachment_metadata(url: str, timeout: int = 3) -> Dict[str, str]:
    """
    Fetch content-length and last-modified headers via HEAD request.

    Args:
        url: Attachment URL
        timeout: Request timeout in seconds

    Returns:
        Dict with 'content_length' and 'last_modified' keys (strings)

    Raises:
        requests.RequestException: If HEAD request fails
    """
    response = requests.head(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()

    return {
        'content_length': response.headers.get('Content-Length', ''),
        'last_modified': response.headers.get('Last-Modified', '')
    }


def get_matter_key(matter_file: Optional[str], matter_id: Optional[str]) -> Optional[str]:
    """
    Get canonical matter key, preferring semantic ID over UUID.

    Args:
        matter_file: Public semantic ID (e.g., "25-1234", "BL2025-1098")
        matter_id: Backend UUID or numeric ID

    Returns:
        matter_file if present, else matter_id, else None

    Example:
        >>> get_matter_key("25-1234", "uuid-abc-123")
        '25-1234'
        >>> get_matter_key(None, "uuid-abc-123")
        'uuid-abc-123'
    """
    return matter_file or matter_id


def combine_date_time(date_str: Optional[str], time_str: Optional[str]) -> Optional[str]:
    """
    Combine separate date and time strings into ISO datetime.

    Generalizable utility for vendors that split date/time into separate fields
    (Legistar: EventDate + EventTime, etc.).

    Args:
        date_str: ISO date string (e.g., "2025-11-18T00:00:00" or "2025-11-18")
        time_str: Time string in various formats (e.g., "6:30 PM", "18:30:00", "6:30 PM EST")

    Returns:
        Combined ISO datetime string, or original date_str if time parsing fails

    Example:
        >>> combine_date_time("2025-11-18T00:00:00", "6:30 PM")
        '2025-11-18T18:30:00'
        >>> combine_date_time("2025-11-18", "18:30")
        '2025-11-18T18:30:00'
        >>> combine_date_time("2025-11-18", None)
        '2025-11-18'

    Confidence: 8/10
    - Handles common time formats (12h/24h, with/without seconds)
    - Falls back gracefully to date-only if time parsing fails
    - Timezone handling is basic (strips timezone info for consistency)
    """
    if not date_str:
        return None

    if not time_str:
        return date_str

    try:
        # Parse date (handle ISO datetime or date-only)
        if 'T' in date_str:
            date_obj = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        else:
            date_obj = datetime.fromisoformat(date_str)

        # Clean time string (remove timezone abbreviations like "EST", "PST")
        time_clean = time_str.strip()
        for tz in [' EST', ' PST', ' CST', ' MST', ' EDT', ' PDT', ' CDT', ' MDT']:
            time_clean = time_clean.replace(tz, '')

        # Try parsing time in common formats
        time_obj = None
        time_formats = [
            '%I:%M %p',        # 6:30 PM
            '%I:%M:%S %p',     # 6:30:00 PM
            '%H:%M',           # 18:30
            '%H:%M:%S',        # 18:30:00
        ]

        for fmt in time_formats:
            try:
                time_obj = datetime.strptime(time_clean, fmt)
                break
            except ValueError:
                continue

        if not time_obj:
            logger.debug("could not parse time - using date only", time_str=time_str)
            return date_str

        # Combine date and time
        combined = date_obj.replace(
            hour=time_obj.hour,
            minute=time_obj.minute,
            second=time_obj.second
        )

        # Return as ISO string (strip timezone for consistency)
        return combined.replace(tzinfo=None).isoformat()

    except Exception as e:
        logger.debug("error combining date/time", date_str=date_str, time_str=time_str, error=str(e))
        return date_str
