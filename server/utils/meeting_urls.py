"""Canonical backend meeting URL components."""

from datetime import date, datetime
from typing import TypeAlias


MeetingDate: TypeAlias = date | datetime | str | None


def generate_meeting_slug(meeting_id: str, meeting_date: MeetingDate) -> str:
    """Return the frontend-compatible ``date-id`` meeting slug.

    Database callers normally provide ``datetime`` values, while email and
    alert boundaries sometimes carry ISO strings. Invalid or absent dates use
    the same explicit ``undated`` prefix as the frontend.
    """
    date_slug = _canonical_date(meeting_date) or "undated"
    return f"{date_slug}-{meeting_id}"


def _canonical_date(value: MeetingDate) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None
    if " - " in candidate:
        candidate = candidate.split(" - ", 1)[0].strip()
    try:
        return datetime.fromisoformat(
            candidate.replace("Z", "+00:00")
        ).date().isoformat()
    except ValueError:
        return None
