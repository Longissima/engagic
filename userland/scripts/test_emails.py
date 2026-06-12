#!/usr/bin/env python3
"""
Email Integration Test Script

FULL END-TO-END TESTING with REAL database data.
Sends all email types to verify styling, formatting, and data flow.

Pulls real meetings and keyword matches from production database.

Usage:
    uv run userland/scripts/test_emails.py your@email.com
    ./deploy.sh test-emails
"""

import os
import sys
import secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from userland.email.transactional import send_magic_link
from userland.email.emailer import EmailService
from userland.scripts.weekly_digest import (
    build_digest_email,
    get_city_name,
    get_editorial_picks,
    get_upcoming_meetings,
)


async def send_test_emails(email: str):
    """
    Send all email types to test address.

    Args:
        email: Recipient email address
    """
    print(f"Sending test emails to: {email}\n")

    # 1. Magic link (signup)
    print("1. Magic Link - Signup")
    signup_token = secrets.token_urlsafe(32)
    result = await send_magic_link(
        email=email,
        token=signup_token,
        user_name="Test User",
        is_signup=True
    )
    print(f"   Status: {'Sent' if result else 'Failed'}\n")

    # 2. Magic link (login)
    print("2. Magic Link - Login")
    login_token = secrets.token_urlsafe(32)
    result = await send_magic_link(
        email=email,
        token=login_token,
        user_name="Test User",
        is_signup=False
    )
    print(f"   Status: {'Sent' if result else 'Failed'}\n")

    # 3. Weekly digest
    print("3. Weekly Digest")
    await send_test_digest(email)

    print("\nAll test emails sent.")
    print(f"Check inbox: {email}")


async def send_test_digest(email: str):
    """Send weekly digest with REAL data from database."""
    from database.db_postgres import Database
    import asyncpg

    # Use actual city with real data
    city_banana = "paloaltoCA"
    app_url = os.getenv("APP_URL", "https://engagic.org")

    # Initialize database connection
    pool = await asyncpg.create_pool(
        host=os.getenv("USERLAND_DB_HOST", "localhost"),
        port=int(os.getenv("USERLAND_DB_PORT", "5432")),
        database=os.getenv("USERLAND_DB_NAME", "engagic"),
        user=os.getenv("USERLAND_DB_USER", "engagic"),
        password=os.getenv("USERLAND_DB_PASSWORD", ""),
        min_size=1,
        max_size=5
    )
    db = Database(pool)

    try:
        # Get REAL data from database
        city_name = await get_city_name(db, city_banana)

        # Get REAL upcoming meetings (next 10 days)
        upcoming_meetings = await get_upcoming_meetings(db, city_banana, days_ahead=10)

        # Substantive item count across the window (mirrors the real digest)
        meeting_ids = [m['id'] for m in upcoming_meetings]
        substantive_count = 0
        if meeting_ids:
            async with db.pool.acquire() as conn:
                substantive_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM items
                    WHERE meeting_id = ANY($1) AND filter_reason IS NULL
                """, meeting_ids) or 0

        # REAL editorial picks: happening_items -> LLM -> bare titles,
        # whichever the environment supports
        editorial_picks = await get_editorial_picks(
            db, city_banana, city_name, upcoming_meetings
        )

        email_service = EmailService()
        meeting_count = len(upcoming_meetings)

        def render(keywords: list) -> str:
            return build_digest_email(
                city_name=city_name,
                city_banana=city_banana,
                keywords=keywords,
                headline_groups=[],
                meeting_count=meeting_count,
                upcoming_meetings=upcoming_meetings,
                substantive_item_count=substantive_count,
                app_url=app_url,
                unsubscribe_token="test-unsubscribe-token",
                editorial_picks=editorial_picks,
            )

        # Variant A: no keywords -> the editorial digest
        result_a = await email_service.send_email(
            to_email=email,
            subject=f"[test:editorial] This week in {city_name}",
            html_body=render([]),
        )

        # Variant B: keywords with no hits -> "No items matched" + editorial
        result_b = await email_service.send_email(
            to_email=email,
            subject=f"[test:no-hits] This week in {city_name}",
            html_body=render(["heliports"]),
        )

        print(f"   Status: editorial={'Sent' if result_a else 'Failed'}, "
              f"no-hits={'Sent' if result_b else 'Failed'}")
        print(f"   Real data: {len(editorial_picks)} editorial picks, "
              f"{meeting_count} upcoming meetings, {substantive_count} substantive items\n")
    finally:
        await pool.close()


if __name__ == "__main__":
    import asyncio

    if len(sys.argv) > 1:
        test_email = sys.argv[1]
    else:
        print("Usage: uv run userland/scripts/test_emails.py your@email.com")
        sys.exit(1)

    asyncio.run(send_test_emails(test_email))
