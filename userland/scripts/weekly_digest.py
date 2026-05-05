"""
Weekly Digest Script

Runs every Sunday at 9am. Sends users a digest of personalized keyword
headlines linking to specific agenda items, or a CTA to configure keywords.

Note: "Alert" in the codebase = Weekly Digest Subscription (not real-time alerts)

Usage:
    python3 -m userland.scripts.weekly_digest

Cron:
    0 9 * * 0 cd /opt/engagic && uv run python -m userland.scripts.weekly_digest
"""

import asyncio
import os
import re
import sys
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from google import genai
from google.genai import types

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from config import config, get_logger
from database.db_postgres import Database
from userland.auth.jwt import generate_unsubscribe_token, init_jwt
from userland.email.emailer import EmailService

logger = get_logger(__name__)


def generate_anchor_id(item: Dict[str, Any]) -> str:
    """
    Generate item anchor ID matching frontend logic.

    Priority: agenda_number > matter_file > item_id.
    """
    if item.get('agenda_number'):
        normalized = item['agenda_number'].lower()
        normalized = re.sub(r'[^a-z0-9]', '-', normalized)
        normalized = re.sub(r'-+', '-', normalized)
        normalized = normalized.strip('-')
        return f"item-{normalized}"

    if item.get('matter_file'):
        normalized = item['matter_file'].lower()
        normalized = re.sub(r'[^a-z0-9-]', '-', normalized)
        return normalized

    item_id = item.get('item_id', '')
    if '_' in item_id:
        sequence = item_id.split('_')[-1]
        return f"item-{sequence}"
    return f"item-{item_id}"


async def get_city_name(db: Database, city_banana: str) -> str:
    """Get formatted city name from banana (e.g., 'paloaltoCA' -> 'Palo Alto, CA')"""
    city = await db.jurisdictions.get_city(city_banana)
    if city:
        return f"{city.name}, {city.state}"
    return city_banana


async def get_upcoming_meetings(db: Database, city_banana: str, days_ahead: int = 7) -> List[Dict[str, Any]]:
    """Get upcoming meetings for a city. Filters out cancelled/postponed."""
    today = datetime.now().date()
    end_date = today + timedelta(days=days_ahead)

    meetings = await db.meetings.get_upcoming_meetings(
        banana=city_banana,
        start_date=today,
        end_date=end_date,
        limit=50
    )

    return [
        {
            'id': m.id,
            'banana': m.banana,
            'title': m.title,
            'date': str(m.date),
            'agenda_url': m.agenda_url,
            'packet_url': m.packet_url,
            'status': m.status
        }
        for m in meetings
    ]


async def find_keyword_matches(
    db: Database,
    city_banana: str,
    keywords: List[str],
    days_ahead: int = 7
) -> List[Dict[str, Any]]:
    """
    Find items in upcoming meetings that mention user's keywords.
    Filters out cancelled/postponed. Deduplicates by item_id.
    """
    if not keywords:
        return []

    today = datetime.now().date()
    end_date = today + timedelta(days=days_ahead)

    all_matches = []

    for keyword in keywords:
        rows = await db.items.search_upcoming_by_keyword(
            banana=city_banana,
            keyword=keyword,
            start_date=today,
            end_date=end_date
        )

        for row in rows:
            all_matches.append({
                'keyword': keyword,
                'item_id': row['item_id'],
                'meeting_id': row['meeting_id'],
                'item_title': row['item_title'],
                'item_summary': row['summary'] or "",
                'meeting_title': row['meeting_title'],
                'meeting_date': str(row['date']),
                'agenda_url': row['agenda_url'],
                'banana': row['banana'],
                'agenda_number': row['agenda_number'],
                'matter_file': row['matter_file'],
                'sponsor_count': len(row['sponsors']) if row.get('sponsors') else 0,
            })

    # Deduplicate by item_id, aggregate matched keywords
    deduplicated = {}
    for match in all_matches:
        item_id = match['item_id']
        if item_id not in deduplicated:
            deduplicated[item_id] = match.copy()
            deduplicated[item_id]['matched_keywords'] = [match['keyword']]
        else:
            if match['keyword'] not in deduplicated[item_id]['matched_keywords']:
                deduplicated[item_id]['matched_keywords'].append(match['keyword'])

    return list(deduplicated.values())


def _extract_summary_section(text: str) -> str:
    """Strip a summary to just the ## Summary section, removing Citizen Impact, Confidence, etc."""
    # Find start of Summary section
    start = text.find("## Summary")
    if start == -1:
        return text.strip()
    # Content starts after the header line
    content_start = text.find("\n", start)
    if content_start == -1:
        return text[start:].strip()
    # Find next ## heading or end of text
    next_heading = text.find("\n##", content_start + 1)
    if next_heading == -1:
        return text[content_start:].strip()
    return text[content_start:next_heading].strip()


async def generate_headline(
    client: genai.Client,
    city_name: str,
    meeting_title: str,
    meeting_date: str,
    keyword: str,
    items_with_summaries: List[Dict[str, Any]],
) -> Optional[str]:
    """
    One Flash-Lite call per (meeting, keyword) pair: produce a single
    personalized sentence about what's being proposed that affects this interest.
    Returns None on any failure.
    """
    # Build context blocks with metadata
    blocks = []
    for item in items_with_summaries:
        summary_text = _extract_summary_section(item['item_summary'])
        meta = f"Title: {item['item_title']}"
        if item.get('sponsor_count'):
            meta += f"\nSponsors: {item['sponsor_count']}"
        blocks.append(f"{meta}\n{summary_text}")
    context = "\n---\n".join(blocks)

    # Format meeting time for the model
    date_obj = datetime.fromisoformat(meeting_date)
    day_name = date_obj.strftime("%A")
    time_str = date_obj.strftime("%I:%M %p").lstrip("0")

    prompt = (
        f"Items from {city_name} {meeting_title} on {day_name} at {time_str}:\n\n"
        f"{context}\n\n"
        f"Pick the two highest-impact items. Ignore procedural items.\n\n"
        f'A resident cares about "{keyword}". Write exactly two sentences, '
        f"one per item. Each sentence must include: (1) the day and time, "
        f"(2) the most concrete proposal with numbers, (3) sponsor count if "
        f"available. Never predict whether legislation will pass. State only "
        f"what is being proposed. No hedging words like 'may', 'aims to', "
        f"'seeks to', 'work toward'. 30 words max per sentence.\n\n"
        f"Respond with ONLY the two sentences. No titles, no labels, no preamble."
    )

    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=200,
            ),
        )
        if not resp.text or not resp.text.strip():
            return None
        headline = resp.text.strip().strip('"')
        words = headline.split()
        if len(words) > 70:
            headline = " ".join(words[:70]).rstrip(".,") + "."
        return headline
    except Exception as e:
        logger.warning("headline generation failed", meeting=meeting_title, keyword=keyword, error=str(e))
        return None


async def generate_city_headlines(
    keyword_matches: List[Dict[str, Any]],
    city_name: str,
) -> Dict[tuple, str]:
    """
    Generate headlines for all (meeting_id, keyword) pairs in a city.
    Returns {(meeting_id, keyword): headline_sentence}.
    Called once per city, shared across all users watching that city.
    """
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        return {}

    client = genai.Client(api_key=api_key)

    # Group by (meeting_id, keyword)
    groups: Dict[tuple, List[Dict[str, Any]]] = {}
    for match in keyword_matches:
        mid = match["meeting_id"]
        for kw in match.get("matched_keywords", []):
            groups.setdefault((mid, kw), []).append(match)

    # Build tasks for parallel execution
    keys = []
    tasks = []
    for (mid, kw), group in groups.items():
        first = group[0]
        items_with_summaries = [m for m in group if m.get("item_summary")]
        if not items_with_summaries:
            continue

        keys.append((mid, kw))
        tasks.append(generate_headline(
            client=client,
            city_name=city_name,
            meeting_title=first["meeting_title"],
            meeting_date=first["meeting_date"],
            keyword=kw,
            items_with_summaries=items_with_summaries,
        ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    cache: Dict[tuple, str] = {}
    for key, result in zip(keys, results):
        if isinstance(result, BaseException):
            logger.warning("headline generation failed", key=str(key), error=str(result))
        elif isinstance(result, str):
            cache[key] = result

    return cache


def _build_headline_groups(
    all_matches: List[Dict[str, Any]],
    headline_cache: Dict[tuple, str],
    user_keywords: set,
) -> List[Dict[str, Any]]:
    """
    Build per-meeting headline groups filtered to a specific user's keywords.
    Each group contains headlines and the specific items that triggered them.
    """
    # Group matches by (meeting_id, keyword), filtered to user's keywords
    # Key: (meeting_id, keyword) -> list of match dicts
    mk_groups: Dict[tuple, List[Dict[str, Any]]] = {}
    meeting_meta: Dict[str, Dict[str, Any]] = {}

    for match in all_matches:
        overlap = user_keywords & set(match['matched_keywords'])
        if not overlap:
            continue
        mid = match['meeting_id']
        if mid not in meeting_meta:
            meeting_meta[mid] = {
                'meeting_title': match['meeting_title'],
                'meeting_date': match['meeting_date'],
                'meeting_id': mid,
                'banana': match['banana'],
            }
        for kw in overlap:
            mk_groups.setdefault((mid, kw), []).append(match)

    # Build output grouped by meeting
    by_meeting: Dict[str, List[Dict[str, Any]]] = {}
    for (mid, kw), items in mk_groups.items():
        if mid not in by_meeting:
            by_meeting[mid] = []
        sentence = headline_cache.get((mid, kw))
        by_meeting[mid].append({
            'keyword': kw,
            'sentence': sentence,
            'items': items,
        })

    groups = []
    for mid, keyword_entries in by_meeting.items():
        meta = meeting_meta[mid]
        groups.append({
            **meta,
            'keyword_entries': sorted(keyword_entries, key=lambda e: e['keyword']),
        })

    groups.sort(key=lambda g: g['meeting_date'])
    return groups


def _format_date(date_str: str) -> str:
    return datetime.fromisoformat(date_str).strftime("%a, %b %d")


def _truncate_title(title: str, max_len: int = 65) -> str:
    """Truncate at word boundary."""
    if len(title) <= max_len:
        return title
    truncated = title[:max_len].rsplit(' ', 1)[0]
    return truncated.rstrip('.,;:') + "..."


def _meeting_url(app_url: str, banana: str, meeting_id: str, meeting_date: str) -> str:
    date_obj = datetime.fromisoformat(meeting_date)
    slug = f"{date_obj.strftime('%Y-%m-%d')}-{meeting_id}"
    return f"{app_url}/{banana}/{slug}"


def _build_day_summary(meetings: List[Dict[str, Any]]) -> str:
    """Group meetings by day of week, return compact summary like '11 Monday, 8 Tuesday'."""
    by_day: OrderedDict[str, int] = OrderedDict()
    for m in sorted(meetings, key=lambda x: x['date']):
        date_obj = datetime.fromisoformat(m['date'])
        day_label = date_obj.strftime("%A")
        by_day[day_label] = by_day.get(day_label, 0) + 1
    return ", ".join(f"{count} {day}" for day, count in by_day.items())


def build_digest_email(
    city_name: str,
    city_banana: str,
    keywords: List[str],
    headline_groups: List[Dict[str, Any]],
    meeting_count: int,
    upcoming_meetings: List[Dict[str, Any]],
    substantive_item_count: int,
    app_url: str,
    unsubscribe_token: str,
    is_donor: bool = False,
) -> str:
    """
    Build HTML email for weekly digest. Single font, content-first.
    """
    city_url = f"{app_url}/{city_banana}"
    unsubscribe_url = f"https://api.engagic.org/api/auth/unsubscribe?token={unsubscribe_token}"
    show_keyword_prefix = len(keywords) > 1

    # Shared styles
    font = "Georgia, 'Times New Roman', serif"
    mono = "'Courier New', Courier, monospace"
    indigo = "#4f46e5"
    gray = "#6b7280"
    dark = "#111827"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<title>This week in {city_name}</title>
<style>
:root {{ color-scheme: light dark; }}
@media (prefers-color-scheme: dark) {{
    body, table {{ background-color: #111827 !important; }}
    h1[style*="color: {dark}"],
    p[style*="color: {dark}"] {{ color: #f3f4f6 !important; }}
    p[style*="color: {gray}"] {{ color: #9ca3af !important; }}
    p[style*="color: #9ca3af"] {{ color: #6b7280 !important; }}
    div[style*="border-bottom: 2px solid {indigo}"] {{ border-color: {indigo} !important; }}
    div[style*="border-bottom: 1px solid #e5e7eb"] {{ border-color: #374151 !important; }}
    a[style*="color: {indigo}"] {{ color: #818cf8 !important; }}
}}
</style>
</head>
<body style="margin: 0; padding: 0; background-color: #f3f4f6; font-family: {font};">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background-color: #f3f4f6;">
<tr><td align="center" style="padding: 32px 16px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="560" style="max-width: 560px;">

    <!-- Header -->
    <tr><td style="padding: 0 0 24px 0;">
        <table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr>
            <td style="padding-right: 12px; vertical-align: middle;">
                <img src="https://engagic.org/icon-192.png" alt="" width="28" height="28" style="display: block; border-radius: 6px;" />
            </td>
            <td style="vertical-align: middle; font-family: {mono}; font-size: 14px; font-weight: 700; color: {indigo}; letter-spacing: 0.02em;">
                engagic
            </td>
        </tr></table>
    </td></tr>

    <!-- Title -->
    <tr><td style="padding: 0 0 8px 0;">
        <h1 style="margin: 0; font-size: 22px; font-weight: 700; color: {dark}; font-family: {font}; line-height: 1.3;">
            This week in {city_name}
        </h1>
    </td></tr>

    <tr><td style="padding: 0 0 28px 0;">
        <div style="border-bottom: 2px solid {indigo}; width: 40px;"></div>
    </td></tr>
"""

    # -- Headlines --
    if keywords and headline_groups:
        for group in headline_groups:
            m_url = _meeting_url(app_url, group['banana'], group['meeting_id'], group['meeting_date'])

            # Headlines
            for entry in group['keyword_entries']:
                prefix = f'<strong style="color: {indigo};">{entry["keyword"]}:</strong> ' if show_keyword_prefix else ""

                if entry['sentence']:
                    sentences = [s.strip() for s in entry['sentence'].strip().split('\n') if s.strip()]
                    for i, sentence in enumerate(sentences):
                        if not sentence.endswith('.'):
                            sentence += '.'
                        label = prefix if i == 0 else ""
                        html += f"""
    <tr><td style="padding: 0 0 16px 0;">
        <p style="margin: 0; font-size: 17px; color: {dark}; font-family: {font}; line-height: 1.55;">
            {label}{sentence}
        </p>
    </td></tr>
"""
                else:
                    html += f"""
    <tr><td style="padding: 0 0 16px 0;">
        <p style="margin: 0; font-size: 17px; color: {gray}; font-family: {font}; line-height: 1.55;">
            {prefix}Items matching this keyword found.
        </p>
    </td></tr>
"""

            # Item count + meeting link on one line
            item_count = len(set(
                item['item_id']
                for entry in group['keyword_entries']
                for item in entry['items']
            ))
            html += f"""
    <tr><td style="padding: 0 0 24px 0;">
        <p style="margin: 0; font-size: 14px; color: {gray}; font-family: {font};">
            <a href="{m_url}" style="color: {indigo}; text-decoration: none; font-weight: 600;">{item_count} agenda item{'s' if item_count != 1 else ''}</a>
            &nbsp;&middot;&nbsp; {group['meeting_title']} &nbsp;&middot;&nbsp; {_format_date(group['meeting_date'])}
        </p>
    </td></tr>
"""

        # Separator before next section or footer
        html += """
    <tr><td style="padding: 0 0 24px 0;">
        <div style="border-bottom: 1px solid #e5e7eb;"></div>
    </td></tr>
"""

    elif keywords:
        html += f"""
    <tr><td style="padding: 0 0 16px 0;">
        <p style="margin: 0; font-size: 16px; color: {gray}; font-family: {font}; line-height: 1.55;">
            No items matched your keywords this week.
        </p>
    </td></tr>
    <tr><td style="padding: 0 0 24px 0;">
        <p style="margin: 0; font-size: 14px; color: {gray}; font-family: {font};">
            {meeting_count} meeting{'s' if meeting_count != 1 else ''} scheduled &mdash;
            <a href="{city_url}" style="color: {indigo}; text-decoration: none; font-weight: 600;">browse on engagic.org</a>
        </p>
    </td></tr>
    <tr><td style="padding: 0 0 24px 0;">
        <div style="border-bottom: 1px solid #e5e7eb;"></div>
    </td></tr>
"""

    else:
        day_summary = _build_day_summary(upcoming_meetings)
        html += f"""
    <tr><td style="padding: 0 0 16px 0;">
        <p style="margin: 0; font-size: 17px; color: {dark}; font-family: {font}; line-height: 1.55;">
            {meeting_count} meeting{'s' if meeting_count != 1 else ''} coming up: {day_summary}
        </p>
    </td></tr>"""
        if substantive_item_count > 0:
            html += f"""
    <tr><td style="padding: 0 0 16px 0;">
        <p style="margin: 0; font-size: 14px; color: {gray}; font-family: {font}; line-height: 1.55;">
            Comprising {substantive_item_count} substantive agenda item{'s' if substantive_item_count != 1 else ''}.
        </p>
    </td></tr>"""
        html += f"""
    <tr><td style="padding: 0 0 24px 0;">
        <a href="{city_url}" style="color: {indigo}; text-decoration: none; font-size: 14px; font-weight: 600; font-family: {font};">Browse agendas &#8594;</a>
    </td></tr>
    <tr><td style="padding: 0 0 24px 0;">
        <div style="border-bottom: 1px solid #e5e7eb;"></div>
    </td></tr>
"""

    # Footer
    donation_line = ""
    if not is_donor:
        donation_line = f"""
        <br>Free and open-source. <a href="https://engagic.org/about/donate" style="color: {indigo}; text-decoration: none;">Support the project</a>."""

    keywords_link = ""
    if not keywords:
        keywords_link = f"""
            &nbsp;&middot;&nbsp;
            <a href="{app_url}/dashboard" style="color: #9ca3af; text-decoration: underline;">Add keywords</a>"""

    html += f"""
    <tr><td style="padding: 0 0 8px 0;">
        <p style="margin: 0; font-size: 12px; color: #9ca3af; font-family: {font}; line-height: 1.7;">
            Watching {city_name}.{donation_line}
            <br><a href="{app_url}/dashboard" style="color: #9ca3af; text-decoration: underline;">Manage</a>{keywords_link}
            &nbsp;&middot;&nbsp;
            <a href="{unsubscribe_url}" style="color: #9ca3af; text-decoration: underline;">Unsubscribe</a>
        </p>
    </td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

    return html


async def send_weekly_digest():
    """
    Main function: Send weekly digests to all active users.

    Three phases for city-level headline caching:
    1. Collect all alerts, group by city
    2. Per city: find all keyword matches, generate all headlines once
    3. Per user: filter to their keywords, build and send email
    """
    app_url = os.getenv('APP_URL', 'https://engagic.org')

    if config.USERLAND_JWT_SECRET:
        try:
            init_jwt(config.USERLAND_JWT_SECRET)
        except ValueError:
            pass  # Already initialized

    logger.info("starting weekly digest process")

    db = await Database.create()
    try:
        email_service = EmailService()

        active_alerts = await db.userland.get_active_alerts()
        logger.info("found active alerts", count=len(active_alerts))

        # Phase 1: Collect all work by city, resolve users upfront
        city_alerts: Dict[str, List[tuple]] = {}
        for alert in active_alerts:
            user = await db.userland.get_user(alert.user_id)
            if not user:
                logger.warning("user not found for alert", alert_id=alert.id)
                continue
            if not alert.cities or len(alert.cities) == 0:
                logger.warning("alert has no cities configured", alert_id=alert.id)
                continue
            banana = alert.cities[0]
            city_alerts.setdefault(banana, []).append((alert, user))

        # Phase 2: Per-city data collection and headline generation
        city_data: Dict[str, Dict] = {}
        for banana, alert_users in city_alerts.items():
            city_name = await get_city_name(db, banana)

            # Union of all keywords across users watching this city
            all_keywords: set = set()
            for alert, _user in alert_users:
                all_keywords.update(alert.criteria.get('keywords', []))

            all_matches = []
            if all_keywords:
                all_matches = await find_keyword_matches(
                    db, banana, list(all_keywords), days_ahead=10
                )

            upcoming = await get_upcoming_meetings(db, banana, days_ahead=10)
            meeting_ids = [m['id'] for m in upcoming]

            # Count substantive items (filter_reason IS NULL) across upcoming meetings
            substantive_count = 0
            if meeting_ids:
                async with db.pool.acquire() as conn:
                    row = await conn.fetchval("""
                        SELECT COUNT(*) FROM items
                        WHERE meeting_id = ANY($1) AND filter_reason IS NULL
                    """, meeting_ids)
                    substantive_count = row or 0

            headline_cache: Dict[tuple, str] = {}
            if all_matches:
                headline_cache = await generate_city_headlines(all_matches, city_name)
                logger.info("generated headlines",
                    city=banana,
                    pairs=len(headline_cache),
                    matches=len(all_matches))

            city_data[banana] = {
                'city_name': city_name,
                'all_matches': all_matches,
                'headline_cache': headline_cache,
                'meeting_count': len(upcoming),
                'upcoming_meetings': upcoming,
                'substantive_item_count': substantive_count,
            }

        # Phase 3: Build and send per-user emails
        sent_count = 0
        error_count = 0

        for banana, alert_users in city_alerts.items():
            data = city_data[banana]

            for alert, user in alert_users:
                try:
                    keywords = alert.criteria.get('keywords', [])
                    user_keywords = set(keywords)

                    logger.info("processing digest", email=user.email, city=banana)

                    headline_groups = _build_headline_groups(
                        data['all_matches'], data['headline_cache'], user_keywords
                    )

                    # Skip if truly nothing to show
                    if not headline_groups and data['meeting_count'] == 0 and not keywords:
                        logger.info("no content for user, skipping", email=user.email)
                        continue

                    unsubscribe_token = generate_unsubscribe_token(user.id)

                    html = build_digest_email(
                        city_name=data['city_name'],
                        city_banana=banana,
                        keywords=keywords,
                        headline_groups=headline_groups,
                        meeting_count=data['meeting_count'],
                        upcoming_meetings=data['upcoming_meetings'],
                        substantive_item_count=data['substantive_item_count'],
                        app_url=app_url,
                        unsubscribe_token=unsubscribe_token,
                        is_donor=user.is_donor,
                    )

                    subject = f"This week in {data['city_name']}"
                    if headline_groups:
                        n = sum(len(g['keyword_entries']) for g in headline_groups)
                        subject += f" -- {n} update{'s' if n != 1 else ''} for your keywords"

                    await email_service.send_email(
                        to_email=user.email,
                        subject=subject,
                        html_body=html,
                        from_address="Engagic Digest <digest@engagic.org>",
                    )

                    sent_count += 1
                    logger.info("sent digest", email=user.email)

                except Exception as e:
                    error_count += 1
                    logger.error("failed to send digest", alert_id=alert.id, error=str(e))
                    continue

        logger.info("weekly digest complete", sent_count=sent_count, error_count=error_count)
        return sent_count, error_count
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(send_weekly_digest())
