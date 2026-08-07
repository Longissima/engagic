"""Input validation, sanitization, and existence checks for API routes."""

import re

from fastapi import HTTPException
from fastapi import Query

from config import config
from server.utils.constants import (
    RATABLE_ENTITY_TYPES,
    VALID_ISSUE_TYPES,
    WATCHABLE_ENTITY_TYPES,
)

# Hard ceiling for pagination limit across all API endpoints.
# Frontend never requests more than 50; anything above 100 is scraper behavior.
MAX_API_LIMIT = 100


def capped_limit(default: int = 50) -> int:
    """Return a Query dependency that clamps limit to MAX_API_LIMIT."""
    return Query(default=default, ge=1, le=MAX_API_LIMIT)


def clamp_limit(value: int) -> int:
    """Clamp a limit value to MAX_API_LIMIT."""
    return min(max(value, 1), MAX_API_LIMIT)

def sanitize_string(value: str) -> str:
    """Sanitize string input to prevent injection attacks"""
    if not value:
        return ""

    # Basic SQL injection prevention - reject obvious patterns
    sql_patterns = [
        r"';\s*DROP",
        r"';\s*DELETE",
        r"';\s*UPDATE",
        r"';\s*INSERT",
        r"--",
        r"/\*.*\*/",
        r"UNION\s+SELECT",
        r"OR\s+1\s*=\s*1",
    ]
    for pattern in sql_patterns:
        if re.search(pattern, value, re.IGNORECASE):
            raise ValueError("Invalid characters in input")

    # Remove potentially dangerous characters
    sanitized = re.sub(r'[<>"\';()&+]', "", value.strip())
    return sanitized[: config.MAX_QUERY_LENGTH]


def validate_entity_type(entity_type: str, allowed: set, param_name: str = "entity_type"):
    """Validate entity_type against allowed set, raise HTTPException if invalid."""
    if entity_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name}. Must be one of: {sorted(allowed)}"
        )


def validate_watchable_entity(entity_type: str):
    """Validate entity_type is watchable (matter, meeting, topic, city, council_member)."""
    validate_entity_type(entity_type, WATCHABLE_ENTITY_TYPES)


def validate_ratable_entity(entity_type: str):
    """Validate entity_type is ratable (item, meeting, matter)."""
    validate_entity_type(entity_type, RATABLE_ENTITY_TYPES)


def validate_issue_type(issue_type: str):
    """Validate issue_type (inaccurate, incomplete, misleading, offensive, other)."""
    validate_entity_type(issue_type, VALID_ISSUE_TYPES, "issue_type")


async def require_city(db, banana: str):
    """Get city or raise 404."""
    city = await db.get_city(banana=banana)
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    return city


async def require_meeting(db, meeting_id: str):
    """Get meeting or raise 404."""
    meeting = await db.meetings.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


async def require_matter(db, matter_id: str):
    """Get matter or raise 404."""
    matter = await db.get_matter(matter_id)
    if not matter:
        raise HTTPException(status_code=404, detail="Matter not found")
    return matter


async def require_council_member(db, member_id: str):
    """Get council member or raise 404."""
    member = await db.council_members.get_member_by_id(member_id)
    if not member:
        raise HTTPException(status_code=404, detail="Council member not found")
    return member


async def require_item(db, item_id: str):
    """Get agenda item or raise 404."""
    item = await db.items.get_agenda_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    return item
