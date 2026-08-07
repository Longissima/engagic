#!/usr/bin/env python3
"""
Migration script: Update existing meeting IDs to unified format.

Old formats varied by vendor:
- Legistar/PrimeGov/CivicClerk/Chicago/IQM2/Granicus: raw vendor ID
- Escribe: escribe_{uuid} or escribe_{hash}
- CivicPlus: civic_{id} or civic_{hash}
- NovusAgenda: numeric ID or hash
- Berkeley/Menlo Park: {vendor}_{YYYYMMDD}

New format: {banana}_{8-char-md5}
Hash input: {banana}:{vendor_id}:{date_iso_or_undated}:{title}

Usage:
    python scripts/migrate_meeting_ids.py --dry-run  # Preview changes
    python scripts/migrate_meeting_ids.py            # Execute migration
"""

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from database.id_generation import generate_meeting_id

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("migrate_meeting_ids")

# Database connection
DATABASE_URL = "postgresql://engagic:engagic@localhost:5432/engagic"


def extract_vendor_id(
    old_id: str,
    vendor: str,
    date: Optional[datetime],
    title: str,
) -> str:
    """Extract vendor_id from old meeting ID based on vendor type.

    For vendors with raw IDs (Legistar, PrimeGov, etc): old_id IS the vendor_id
    For vendors with prefixes (Escribe, CivicPlus): strip prefix
    For date-based (Berkeley, Menlo Park): synthesize from title
    """
    # Vendors that used raw vendor IDs
    raw_id_vendors = {"legistar", "primegov", "civicclerk", "iqm2", "granicus"}
    if vendor in raw_id_vendors:
        return old_id

    # Chicago uses UUIDs directly
    if vendor == "chicago":
        return old_id

    # NovusAgenda: might be numeric or hash, use as-is
    if vendor == "novusagenda":
        return old_id

    # Escribe: strip prefix
    if vendor == "escribe":
        if old_id.startswith("escribe_"):
            return old_id[8:]  # Strip "escribe_"
        return old_id

    # CivicPlus: strip prefix
    if vendor == "civicplus":
        if old_id.startswith("civic_"):
            return old_id[6:]  # Strip "civic_"
        return old_id

    # Berkeley/Menlo Park: old IDs are "berkeley_YYYYMMDD" or "menlopark_YYYYMMDD"
    # Extract the date portion as vendor_id (matches new adapter behavior)
    if vendor in {"berkeley", "menlopark"}:
        # Old format: "{vendor}_{YYYYMMDD}" -> extract date portion
        parts = old_id.split("_", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
            return parts[1]  # Return the date string as vendor_id
        # Fallback: use date from database if ID format unexpected
        if date:
            return date.strftime("%Y%m%d")
        # Last resort: hash the title
        return hashlib.md5(title.encode()).hexdigest()[:8]

    # Unknown vendor: use old_id as vendor_id
    logger.warning(f"Unknown vendor '{vendor}', using old_id as vendor_id")
    return old_id


async def get_meetings_with_cities(conn: asyncpg.Connection) -> list:
    """Get all meetings with their city info."""
    query = """
        SELECT m.id, m.banana, m.title, m.date, c.vendor
        FROM meetings m
        JOIN jurisdictions c ON m.banana = c.banana
        ORDER BY m.banana, m.date
    """
    return await conn.fetch(query)


async def update_meeting_id(
    conn: asyncpg.Connection,
    old_id: str,
    new_id: str,
    dry_run: bool
) -> bool:
    """Update meeting ID and all foreign key references."""
    if dry_run:
        return True

    async with conn.transaction():
        # Update all tables with meeting_id foreign keys (order matters for FK constraints)

        # 1. items (FK to meetings)
        await conn.execute(
            "UPDATE items SET meeting_id = $1 WHERE meeting_id = $2",
            new_id, old_id
        )

        # 2. meeting_topics
        await conn.execute(
            "UPDATE meeting_topics SET meeting_id = $1 WHERE meeting_id = $2",
            new_id, old_id
        )

        # 3. matter_appearances (FK to meetings)
        await conn.execute(
            "UPDATE matter_appearances SET meeting_id = $1 WHERE meeting_id = $2",
            new_id, old_id
        )

        # 4. queue (FK to meetings, nullable)
        await conn.execute(
            "UPDATE queue SET meeting_id = $1 WHERE meeting_id = $2",
            new_id, old_id
        )

        # 5. votes (FK to meetings)
        await conn.execute(
            "UPDATE votes SET meeting_id = $1 WHERE meeting_id = $2",
            new_id, old_id
        )

        # 6. tracked_item_meetings (engagement tracking, FK to meetings)
        await conn.execute(
            "UPDATE tracked_item_meetings SET meeting_id = $1 WHERE meeting_id = $2",
            new_id, old_id
        )

        # 7. Update the meeting itself (primary key)
        await conn.execute(
            "UPDATE meetings SET id = $1 WHERE id = $2",
            new_id, old_id
        )

    return True


async def migrate(dry_run: bool = True):
    """Run the migration."""
    conn = await asyncpg.connect(DATABASE_URL)

    try:
        meetings = await get_meetings_with_cities(conn)
        logger.info(f"Found {len(meetings)} meetings to migrate")

        migrated = 0
        skipped = 0
        errors = 0

        for row in meetings:
            old_id = row["id"]
            banana = row["banana"]
            title = row["title"] or "Unknown Meeting"
            date = row["date"]
            vendor = row["vendor"]

            # Extract vendor_id from old format
            vendor_id = extract_vendor_id(old_id, vendor, date, title)

            # Generate new ID
            try:
                new_id = generate_meeting_id(banana, vendor_id, date, title)
            except Exception as e:
                logger.error(f"Error generating ID for {old_id}: {e}")
                errors += 1
                continue

            if old_id == new_id:
                skipped += 1
                continue

            # Log the change
            if dry_run:
                logger.info(f"  {old_id} -> {new_id}")
            else:
                logger.debug(f"  {old_id} -> {new_id}")

            # Update
            try:
                await update_meeting_id(conn, old_id, new_id, dry_run)
                migrated += 1
            except Exception as e:
                logger.error(f"Failed to update {old_id}: {e}")
                errors += 1

        logger.info(f"Migration complete: {migrated} migrated, {skipped} skipped, {errors} errors")
        if dry_run:
            logger.info("This was a dry run. Run without --dry-run to apply changes.")

    finally:
        await conn.close()


def main():
    global DATABASE_URL

    parser = argparse.ArgumentParser(description="Migrate meeting IDs to unified format")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them"
    )
    parser.add_argument(
        "--database-url",
        default=DATABASE_URL,
        help="PostgreSQL connection URL"
    )
    args = parser.parse_args()

    DATABASE_URL = args.database_url

    asyncio.run(migrate(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
