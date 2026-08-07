"""
Keyword matching engine for userland alerts.

Uses repository pattern for database queries.
Dual-track matching: string-based (granular) + matter-based (deduplicated)
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List
from uuid import uuid4

from config import get_logger
from database.db_postgres import Database
from server.utils.meeting_urls import generate_meeting_slug
from userland.database.models import Alert, AlertMatch

logger = get_logger(__name__)


def _serialize_optional_date(value: Any) -> str | None:
    """Keep missing dates explicit instead of emitting the string ``None``."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)


async def match_alert(
    alert: Alert,
    db: Database,
    since_days: int = 1
) -> List[AlertMatch]:
    """
    Match an alert against recent summaries (string-based matching).

    Args:
        alert: Alert configuration with keywords
        db: Database instance (for both engagic and userland queries)
        since_days: Only match items from last N days

    Returns:
        List of AlertMatch objects
    """
    if not alert.active:
        logger.debug("alert inactive, skipping", alert_name=alert.name)
        return []

    keywords = alert.criteria.get("keywords", [])
    if not keywords:
        logger.info("alert has no keywords, skipping", alert_name=alert.name)
        return []

    cutoff_date = (datetime.now() - timedelta(days=since_days)).isoformat()
    matches = []
    seen_items = set()

    logger.info(
        "matching alert",
        alert_name=alert.name,
        keyword_count=len(keywords),
        city_count=len(alert.cities)
    )

    # Get existing matches once upfront
    existing_matches = await db.userland.get_matches(alert_id=alert.id, limit=1000)
    existing_item_ids = {m.item_id for m in existing_matches}

    for keyword in keywords:
        logger.debug("searching for keyword", keyword=keyword)

        for city_banana in alert.cities:
            # Use repository method instead of raw SQL
            rows = await db.items.search_by_keyword(
                banana=city_banana,
                keyword=keyword,
                since_date=cutoff_date,
                exclude_cancelled=True
            )

            for row in rows:
                item_id = row['id']
                meeting_id = row['meeting_id']

                # Deduplicate by item_id
                if item_id in seen_items:
                    continue
                seen_items.add(item_id)

                # Check if already notified
                if item_id in existing_item_ids:
                    logger.debug("already matched", item_id=item_id)
                    continue

                # Extract clean summary (skip thinking section)
                summary = row['summary'] or ""
                if "## Summary" in summary:
                    summary = summary.split("## Summary", 1)[1].split("##", 1)[0].strip()

                meeting_date = row['date']
                meeting_slug = generate_meeting_slug(meeting_id, meeting_date)
                url = f"https://engagic.org/{row['banana']}/{meeting_slug}?item=item-{item_id}"

                # Create match
                match = AlertMatch(
                    id=str(uuid4()),
                    alert_id=alert.id,
                    meeting_id=meeting_id,
                    item_id=item_id,
                    match_type="keyword",
                    confidence=1.0,
                    matched_criteria={
                        "keyword": keyword,
                        "city": f"{row['city_name']}, {row['state']}",
                        "date": _serialize_optional_date(row["date"]),
                        "meeting_title": row['meeting_title'],
                        "item_title": row['title'],
                        "context": summary[:300],
                        "url": url
                    }
                )

                matches.append(match)
                logger.debug("match found", city_name=row['city_name'], title=row['title'][:50])

    logger.info("alert matching complete", alert_name=alert.name, match_count=len(matches))
    return matches


async def match_matters_for_alert(
    alert: Alert,
    db: Database,
    since_days: int = 1
) -> List[AlertMatch]:
    """
    Match alert against matters (deduplicated, matter-first).

    Benefits:
    - Deduplication: Same bill in 5 committees = 1 alert (not 5)
    - Timeline tracking: Show bill evolution across committees
    - Canonical summaries: High-quality deduplicated summaries

    Args:
        alert: Alert configuration with keywords
        db: Database instance
        since_days: Only match matters seen in last N days

    Returns:
        List of AlertMatch objects (one per matter, not per appearance)
    """
    if not alert.active:
        logger.debug("alert inactive, skipping matter match", alert_name=alert.name)
        return []

    keywords = alert.criteria.get("keywords", [])
    if not keywords:
        logger.info("alert has no keywords, skipping matter match", alert_name=alert.name)
        return []

    cutoff_date = (datetime.now() - timedelta(days=since_days)).isoformat()
    matches = []
    seen_matters = set()

    logger.info("matter matching for alert", alert_name=alert.name, keyword_count=len(keywords), city_count=len(alert.cities))

    for keyword in keywords:
        logger.debug("searching matters for keyword", keyword=keyword)

        # Use repository method instead of raw SQL
        rows = await db.matters.search_by_keyword(
            bananas=alert.cities,
            keyword=keyword,
            since_date=cutoff_date
        )

        for row in rows:
            matter_id = row['id']

            # Deduplicate by matter_id
            if matter_id in seen_matters:
                continue
            seen_matters.add(matter_id)

            # Check if already notified using repository method
            already_matched = await db.matters.check_existing_match(alert.id, matter_id)
            if already_matched:
                logger.debug("already matched matter", matter_id=matter_id)
                continue

            # Get matter timeline using repository method
            appearances = await db.matters.get_timeline(matter_id)

            # Build timeline
            timeline = [
                {
                    "date": _serialize_optional_date(app["appeared_at"]),
                    "committee": app["committee"],
                    "action": app["action"],
                    "meeting_title": app["meeting_title"]
                }
                for app in appearances
            ]

            # Use most recent appearance for URL
            latest = appearances[-1] if appearances else None
            url = None
            item_id = None
            meeting_id = None

            if latest:
                item_id = latest['item_id']
                meeting_id = latest['meeting_id']
                meeting_date = latest['appeared_at']
                meeting_slug = generate_meeting_slug(meeting_id, meeting_date)
                url = f"https://engagic.org/{row['banana']}/{meeting_slug}?item=item-{item_id}"

            # Create match
            match = AlertMatch(
                id=str(uuid4()),
                alert_id=alert.id,
                meeting_id=meeting_id or "",
                item_id=item_id,
                match_type="matter",
                confidence=1.0,
                matched_criteria={
                    "keyword": keyword,
                    "matter_id": matter_id,
                    "matter_file": row['matter_file'],
                    "matter_type": row['matter_type'],
                    "title": row['title'],
                    "city": f"{row['city_name']}, {row['state']}",
                    "canonical_summary": row['canonical_summary'],
                    "sponsors": row['sponsors'],
                    "topics": row['canonical_topics'],
                    "first_seen": _serialize_optional_date(row["first_seen"]),
                    "last_seen": _serialize_optional_date(row["last_seen"]),
                    "appearance_count": row['appearance_count'],
                    "timeline": timeline,
                    "url": url
                }
            )

            matches.append(match)
            logger.debug(
                "matter match found",
                city_name=row['city_name'],
                matter_file=row['matter_file'],
                appearance_count=row['appearance_count']
            )

    logger.info("matter matching complete", alert_name=alert.name, match_count=len(matches))
    return matches


async def match_all_alerts_dual_track(
    db: Database,
    since_days: int = 1
) -> Dict[str, Dict[str, List[AlertMatch]]]:
    """
    Match all active alerts using BOTH string and matter backends.

    Always runs both:
    - String matches: Granular item-level mentions
    - Matter matches: Deduplicated legislative tracking

    Args:
        db: Database instance (async PostgreSQL)
        since_days: Only match items/matters from last N days

    Returns:
        Dict with structure:
        {
            alert_id: {
                "string_matches": [AlertMatch, ...],
                "matter_matches": [AlertMatch, ...]
            }
        }
    """
    active_alerts = await db.userland.get_active_alerts()
    logger.info("processing active alerts dual-track", count=len(active_alerts))

    all_matches = {}

    for alert in active_alerts:
        # String backend (granular item-level)
        string_matches = await match_alert(alert, db=db, since_days=since_days)

        # Matter backend (deduplicated legislative)
        matter_matches = await match_matters_for_alert(alert, db=db, since_days=since_days)

        # Store all matches in database
        for match in string_matches + matter_matches:
            await db.userland.create_match(match)

        all_matches[alert.id] = {
            "string_matches": string_matches,
            "matter_matches": matter_matches
        }

    total_string = sum(len(m["string_matches"]) for m in all_matches.values())
    total_matter = sum(len(m["matter_matches"]) for m in all_matches.values())
    logger.info("dual-track matching complete", string_matches=total_string, matter_matches=total_matter)

    return all_matches
