"""Sync committee roster data from Legistar API

One-time script to populate committee_members table and enrich council_members
with title, district, and contact metadata.

Only syncs CURRENT memberships (EndDate >= today).
Only creates council_members for people with active City Council membership.

Usage:
    uv run scripts/sync_roster.py --city newyorkNY
    uv run scripts/sync_roster.py --all-legistar

API calls per city: 2 (Bodies, OfficeRecords with current filter)
"""

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Dict, Set

sys.path.insert(0, '/opt/engagic')

from database.db_postgres import Database
from vendors.adapters.legistar_adapter_async import AsyncLegistarAdapter
from vendors.session_manager_async import AsyncSessionManager
from config import get_logger, config

logger = get_logger(__name__)


async def sync_roster_for_city(db: Database, banana: str, city_slug: str, api_token: str = None) -> Dict[str, int]:
    """Sync roster data for a single Legistar city.

    Args:
        db: Database instance
        banana: City identifier (e.g., "newyorkNY")
        city_slug: Legistar slug (e.g., "nyc")
        api_token: Optional API token for Legistar

    Returns:
        Dict with counts: committees, council_members, memberships
    """
    stats = {
        "committees_created": 0,
        "council_members_created": 0,
        "council_members_updated": 0,
        "memberships_created": 0,
    }

    adapter = AsyncLegistarAdapter(city_slug, api_token=api_token)

    try:
        roster_data = await adapter.fetch_roster_data()
    except Exception as e:
        logger.error("failed to fetch roster data", banana=banana, error=str(e))
        raise

    bodies = roster_data.get("bodies", [])
    office_records = roster_data.get("office_records", [])

    if not office_records:
        logger.warning("no current office records found", banana=banana)
        return stats

    # Build lookup map for bodies (BodyId -> our committee_id)
    body_id_to_committee: Dict[int, str] = {}
    body_id_to_name: Dict[int, str] = {}

    # Track which body is the primary legislative body (City Council)
    city_council_body_id = None

    # Step 1: Create committees from Bodies
    for body in bodies:
        body_id = body.get("BodyId")
        body_name = (body.get("BodyName") or "").strip()
        body_type = (body.get("BodyTypeName") or "").strip()

        if not body_id or not body_name:
            continue

        # Identify the primary legislative body
        # Match by type or name since different cities use different conventions
        body_type_lower = body_type.lower() if body_type else ""
        body_name_lower = body_name.lower() if body_name else ""

        is_primary = (
            body_type == "Primary Legislative Body"
            or body_type_lower == "city council"
            or body_name_lower in ("city council", "town council", "council")
        )

        if is_primary and city_council_body_id is None:
            city_council_body_id = body_id

        description = (body.get("BodyDescription") or "").strip() or None

        committee = await db.committees.find_or_create_committee(
            banana=banana,
            name=body_name,
            description=description,
        )

        body_id_to_committee[body_id] = committee.id
        body_id_to_name[body_id] = body_name
        stats["committees_created"] += 1

    logger.info("synced committees", banana=banana, count=len(body_id_to_committee))

    # Step 2: Identify actual council members (those with City Council membership)
    # and extract their info from OfficeRecords
    council_member_person_ids: Set[int] = set()
    person_id_to_info: Dict[int, Dict] = {}

    for record in office_records:
        person_id = record.get("OfficeRecordPersonId")
        body_id = record.get("OfficeRecordBodyId")

        if not person_id:
            continue

        # If this is a City Council membership, mark as actual council member
        if body_id == city_council_body_id:
            council_member_person_ids.add(person_id)

            # Extract the best info for this person from City Council record
            person_id_to_info[person_id] = {
                "name": (record.get("OfficeRecordFullName") or "").strip(),
                "title": (record.get("OfficeRecordTitle") or "").strip(),
                "district": (record.get("OfficeRecordExtraText") or "").strip(),
                "email": (record.get("OfficeRecordEmail") or "").strip(),
            }

    logger.info(
        "identified council members",
        banana=banana,
        count=len(council_member_person_ids),
        city_council_body_id=city_council_body_id,
    )

    if not council_member_person_ids:
        logger.warning("no City Council body found or no members", banana=banana)
        # Still continue - some cities might not have a "Primary Legislative Body"

    # Step 3: Create council members for identified persons
    person_id_to_member: Dict[int, str] = {}

    for person_id, info in person_id_to_info.items():
        name = info["name"]
        if not name:
            continue

        # Find or create the council member
        member = await db.council_members.find_or_create_member(
            banana=banana,
            name=name,
        )

        person_id_to_member[person_id] = member.id

        # Update with title, district, email
        metadata = {}
        if info["email"]:
            metadata["email"] = info["email"]

        updated = await db.council_members.update_member_metadata(
            member_id=member.id,
            title=info["title"] if info["title"] else None,
            district=info["district"] if info["district"] else None,
            metadata=metadata if metadata else None,
        )

        if updated:
            stats["council_members_updated"] += 1
        else:
            stats["council_members_created"] += 1

    logger.info("synced council members", banana=banana, count=len(person_id_to_member))

    # Step 4: Create committee memberships from OfficeRecords
    # Only for persons who are actual council members
    for record in office_records:
        person_id = record.get("OfficeRecordPersonId")
        body_id = record.get("OfficeRecordBodyId")

        # Skip if not an actual council member
        if person_id not in person_id_to_member:
            continue

        # Skip City Council itself (that's not a committee assignment)
        if body_id == city_council_body_id:
            continue

        # Skip if body not in our lookup
        if body_id not in body_id_to_committee:
            continue

        member_id = person_id_to_member[person_id]
        committee_id = body_id_to_committee[body_id]

        # Parse dates
        start_date_str = record.get("OfficeRecordStartDate")
        joined_at = None
        if start_date_str:
            try:
                joined_at = datetime.fromisoformat(start_date_str.replace("Z", "+00:00").replace("T00:00:00", ""))
            except (ValueError, TypeError):
                pass

        # Get role from title
        role = record.get("OfficeRecordTitle") or "Member"

        # Add member to committee
        created = await db.committees.add_member_to_committee(
            committee_id=committee_id,
            council_member_id=member_id,
            role=role,
            joined_at=joined_at,
        )

        if created:
            stats["memberships_created"] += 1

    logger.info(
        "synced committee memberships",
        banana=banana,
        memberships=stats["memberships_created"],
    )

    return stats


async def main():
    parser = argparse.ArgumentParser(description="Sync committee roster from Legistar API")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--city", help="City banana to sync (e.g., newyorkNY)")
    group.add_argument("--all-legistar", action="store_true", help="Sync all Legistar cities")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be synced without making changes")

    args = parser.parse_args()

    db = await Database.create()

    try:
        if args.city:
            # Get city info
            city = await db.jurisdictions.get_city(args.city)
            if not city:
                print(f"City not found: {args.city}")
                return

            if city.vendor != "legistar":
                print(f"City {args.city} uses vendor '{city.vendor}', not legistar")
                return

            # Get API token if available
            api_token = None
            if city.slug == "nyc":
                api_token = config.NYC_LEGISTAR_TOKEN

            if args.dry_run:
                print(f"Would sync roster for {args.city} (slug: {city.slug})")
                return

            print(f"Syncing roster for {args.city}...")
            stats = await sync_roster_for_city(db, city.banana, city.slug, api_token)

            print("\nResults:")
            for key, value in stats.items():
                print(f"  {key}: {value}")

        elif args.all_legistar:
            # Get all Legistar cities
            rows = await db.pool.fetch("""
                SELECT banana, slug FROM jurisdictions WHERE vendor = 'legistar' ORDER BY banana
            """)

            print(f"Found {len(rows)} Legistar cities")

            if args.dry_run:
                for row in rows[:10]:
                    print(f"  Would sync: {row['banana']} ({row['slug']})")
                if len(rows) > 10:
                    print(f"  ... and {len(rows) - 10} more")
                return

            total_stats = {
                "committees_created": 0,
                "council_members_created": 0,
                "council_members_updated": 0,
                "memberships_created": 0,
            }

            for row in rows:
                banana = row["banana"]
                slug = row["slug"]

                # Get API token if available
                api_token = None
                if slug == "nyc":
                    api_token = config.NYC_LEGISTAR_TOKEN

                print(f"Syncing {banana}...")

                try:
                    stats = await sync_roster_for_city(db, banana, slug, api_token)
                    for key, value in stats.items():
                        if key in total_stats:
                            total_stats[key] += value
                except Exception as e:
                    print(f"  Error: {e}")
                    continue

            print("\nTotal results:")
            for key, value in total_stats.items():
                print(f"  {key}: {value}")

    finally:
        await db.close()
        await AsyncSessionManager.close_all()


if __name__ == "__main__":
    asyncio.run(main())
