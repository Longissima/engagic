"""
Happening Today Email

Sends a daily email with today's important agenda items.
Runs at 9am PST (17:00 UTC) via cron.

Usage:
    uv run python -m userland.scripts.happening_email

Cron (9am PST / 17:00 UTC):
    0 17 * * * cd /opt/engagic && uv run python -m userland.scripts.happening_email >> /var/log/engagic/happening_email.log 2>&1
"""

import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from config import get_logger
from database.db_postgres import Database
from server.utils.meeting_urls import generate_meeting_slug
from userland.email.emailer import EmailService
from userland.email.templates import (
    email_wrapper_start,
    email_wrapper_end,
    header_section,
    footer_section,
    section_header,
    text_content,
)

logger = get_logger(__name__)

# Recipient from env (set in .llm_secrets)
RECIPIENT_EMAIL = os.getenv("ENGAGIC_HAPPENING_RECIPIENT", "")


def build_happening_email(items: list, today_str: str) -> str:
    """Build HTML email for happening today items."""

    html = email_wrapper_start(f"Happening Today - {today_str}")
    html += header_section(
        title="Happening Today",
        subtitle=today_str,
        meta=f"{len(items)} important items across local government meetings"
    )

    if not items:
        html += text_content(
            "No meetings with ranked items happening today. Check back tomorrow.",
            padding="32px 40px"
        )
    else:
        by_city = defaultdict(list)
        for item in items:
            by_city[item['banana']].append(item)

        for city_banana, city_items in by_city.items():
            city_name = city_items[0].get('city_name', city_banana)
            html += section_header(city_name, padding="32px 40px 16px 40px")

            for item in sorted(city_items, key=lambda x: x['rank']):
                meeting_date = item['meeting_date']
                if isinstance(meeting_date, datetime):
                    meeting_time = meeting_date.strftime("%I:%M %p")
                else:
                    meeting_time = str(meeting_date)
                meeting_slug = generate_meeting_slug(
                    item["meeting_id"], meeting_date
                )
                item_url = f"https://engagic.org/{city_banana}/{meeting_slug}#item-{item['item_id']}"

                html += f"""
                    <tr>
                        <td style="padding: 0 40px 20px 40px;">
                            <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="border-left: 4px solid #4f46e5; background-color: #f8fafc; border-radius: 6px;">
                                <tr>
                                    <td style="padding: 20px;">
                                        <p style="margin: 0 0 8px 0; font-size: 12px; color: #64748b; font-family: 'IBM Plex Mono', monospace;">
                                            #{item['rank']} - {item['meeting_title']} @ {meeting_time}
                                        </p>
                                        <p style="margin: 0 0 12px 0; font-size: 16px; font-weight: 600; color: #0f172a; line-height: 1.4; font-family: 'IBM Plex Mono', monospace;">
                                            {item['item_title'][:100]}{'...' if len(item.get('item_title', '')) > 100 else ''}
                                        </p>
                                        <p style="margin: 0 0 16px 0; font-size: 14px; color: #475569; line-height: 1.6; font-family: Georgia, serif;">
                                            {item['reason']}
                                        </p>
                                        <a href="{item_url}" style="color: #4f46e5; text-decoration: none; font-size: 13px; font-weight: 600; font-family: 'IBM Plex Mono', monospace;">
                                            View item →
                                        </a>
                                    </td>
                                </tr>
                            </table>
                        </td>
                    </tr>
"""

    html += footer_section(
        city_name="Engagic Happening Today",
        show_donation=False,
        dashboard_url="https://engagic.org",
        unsubscribe_url="mailto:hello@engagic.org?subject=Unsubscribe%20Happening"
    )
    html += email_wrapper_end()

    return html


async def send_happening_email():
    """Fetch today's happening items and send email."""

    if not RECIPIENT_EMAIL:
        logger.error("ENGAGIC_HAPPENING_RECIPIENT not set")
        return False

    logger.info("starting happening email")

    db = await Database.create()
    try:
        # Fetch today's happening items with city names
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    h.banana,
                    h.item_id,
                    h.meeting_id,
                    h.meeting_date,
                    h.rank,
                    h.reason,
                    i.title as item_title,
                    m.title as meeting_title,
                    c.name || ', ' || c.state as city_name
                FROM happening_items h
                JOIN items i ON h.item_id = i.id
                JOIN meetings m ON h.meeting_id = m.id
                JOIN jurisdictions c ON h.banana = c.banana
                WHERE h.meeting_date::date = CURRENT_DATE
                ORDER BY c.name, h.rank
            """)

        items = [dict(row) for row in rows]
        logger.info("found happening items for today", count=len(items))

        if not items:
            logger.info("no happening items today, sending anyway for visibility")

        # Build email
        today_str = datetime.now().strftime("%A, %B %d, %Y")
        html = build_happening_email(items, today_str)

        # Send
        email_service = EmailService()
        subject = f"Happening Today - {len(items)} items" if items else "Happening Today - No meetings"

        success = await email_service.send_email(
            to_email=RECIPIENT_EMAIL,
            subject=subject,
            html_body=html,
            from_address="Engagic <happening@engagic.org>"
        )

        if success:
            logger.info("happening email sent", to=RECIPIENT_EMAIL, items=len(items))
        else:
            logger.error("failed to send happening email")

        return success

    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(send_happening_email())
