#!/usr/bin/env python3
"""
Database viewer and editor for the engagic PostgreSQL database
Clean interface for managing cities, meetings, agenda items, and queue

Adapted for async PostgreSQL with repository pattern
"""

import sys
import os
import asyncio
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from uszipcode import SearchEngine

from database.db_postgres import Database
from database.models import Jurisdiction
from scripts._jurisdiction_naming import to_banana_slug, derive_district_stem


# State abbreviation to FIPS code mapping
STATE_TO_FIPS = {
    'AL': '01', 'AK': '02', 'AZ': '04', 'AR': '05', 'CA': '06',
    'CO': '08', 'CT': '09', 'DE': '10', 'DC': '11', 'FL': '12',
    'GA': '13', 'HI': '15', 'ID': '16', 'IL': '17', 'IN': '18',
    'IA': '19', 'KS': '20', 'KY': '21', 'LA': '22', 'ME': '23',
    'MD': '24', 'MA': '25', 'MI': '26', 'MN': '27', 'MS': '28',
    'MO': '29', 'MT': '30', 'NE': '31', 'NV': '32', 'NH': '33',
    'NJ': '34', 'NM': '35', 'NY': '36', 'NC': '37', 'ND': '38',
    'OH': '39', 'OK': '40', 'OR': '41', 'PA': '42', 'RI': '44',
    'SC': '45', 'SD': '46', 'TN': '47', 'TX': '48', 'UT': '49',
    'VT': '50', 'VA': '51', 'WA': '53', 'WV': '54', 'WI': '55', 'WY': '56'
}


def lookup_zipcodes(city_name: str, state: str) -> list[str]:
    """Look up zipcodes for a city using uszipcode.

    Returns list of zipcode strings.
    # TODO: Replace with spatial ZCTA lookup (Census TIGER boundaries) -- uszipcode
    # is unreliable for cities that share zip codes with larger neighbors and crashes
    # on cities not in USPS preferred name list (e.g. Sunrise FL -> "Sanibel" error).
    """
    try:
        se = SearchEngine(
            simple_or_comprehensive=SearchEngine.SimpleOrComprehensiveArgEnum.comprehensive
        )
        results = se.query(city=city_name, state=state, returns=200)
        return [z.zipcode for z in results if z.zipcode]
    except Exception as e:
        print(f"   uszipcode lookup failed: {e}")
        print("   You can enter zipcodes manually below.")
        return []


async def lookup_census_population(db: Database, city_name: str, state: str) -> Optional[int]:
    """Look up population from census_places table (Census 2023 estimates).

    Returns population or None if not found.
    """
    fips = STATE_TO_FIPS.get(state.upper())
    if not fips:
        return None

    async with db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT population FROM census_places
            WHERE UPPER(name) = UPPER($1) AND statefp = $2
            AND population IS NOT NULL
            ORDER BY population DESC
            LIMIT 1
            """,
            city_name, fips
        )
        return row['population'] if row else None


async def lookup_census_geometry(db: Database, city_name: str, state: str) -> Optional[bytes]:
    """Look up geometry from census_places table.

    Tries multiple name variations to handle Census naming quirks:
    - Exact match
    - With hyphen (Winston Salem -> Winston-Salem)
    - Township/town suffix
    - St./Saint normalization
    """
    fips = STATE_TO_FIPS.get(state.upper())
    if not fips:
        return None

    name_upper = city_name.upper()

    # Build list of name variations to try
    variations = [name_upper]

    # Add hyphenated version (Winston Salem -> Winston-Salem)
    if ' ' in name_upper:
        variations.append(name_upper.replace(' ', '-'))

    # St. Paul -> Saint Paul and vice versa
    if name_upper.startswith('SAINT '):
        variations.append('ST. ' + name_upper[6:])
    elif name_upper.startswith('ST. '):
        variations.append('SAINT ' + name_upper[4:])

    # Township variations for MI, NJ, PA
    if state.upper() in ('MI', 'NJ', 'PA'):
        variations.append(f"{name_upper} TOWNSHIP")
        variations.append(f"{name_upper} CHARTER TOWNSHIP")
        if name_upper.endswith(' TOWNSHIP'):
            base = name_upper[:-9]
            variations.append(base)
            variations.append(f"{base} CHARTER TOWNSHIP")

    # Opa Locka -> Opa-locka
    if 'OPA ' in name_upper:
        variations.append(name_upper.replace('OPA ', 'OPA-').lower().title())

    async with db.pool.acquire() as conn:
        for variation in variations:
            row = await conn.fetchrow(
                """
                SELECT wkb_geometry FROM census_places
                WHERE UPPER(name) = $1 AND statefp = $2
                LIMIT 1
                """,
                variation.upper(), fips
            )
            if row:
                return row['wkb_geometry']

        # Fallback: fuzzy LIKE match
        row = await conn.fetchrow(
            """
            SELECT wkb_geometry FROM census_places
            WHERE UPPER(name) LIKE $1 AND statefp = $2
            LIMIT 1
            """,
            f"%{name_upper}%", fips
        )
        return row['wkb_geometry'] if row else None


class DatabaseViewer:
    def __init__(self):
        self.db = None

    async def initialize(self):
        """Initialize async database connection.
        Small pool with short idle timeout -- interactive tool sits at input() prompts.
        """
        self.db = await Database.create(min_size=1, max_size=5)

    async def close(self):
        """Close database connections"""
        if self.db:
            await self.db.close()

    async def show_cities_table(self, limit: int = 50):
        """Display cities table with zipcode counts"""
        cities = await self.db.cities.get_cities(status="active", limit=limit)

        print(f"\n=== JURISDICTIONS TABLE (showing {len(cities)}) ===")
        print(
            f"{'Banana':<20} {'Name':<20} {'State':<6} {'Type':<10} {'Slug':<20} {'Vendor':<12} {'Status':<8} {'ZIPs':<4}"
        )
        print("-" * 125)

        for city in cities:
            # Get zipcode count
            async with self.db.pool.acquire() as conn:
                zipcode_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM zipcodes WHERE banana = $1",
                    city.banana
                )

            print(
                f"{city.banana:<20} {city.name[:19]:<20} {city.state:<6} "
                f"{city.type:<10} {city.slug[:19]:<20} {city.vendor:<12} "
                f"{city.status:<8} {zipcode_count:<4}"
            )

    async def show_zipcodes_table(self, limit: int = 50):
        """Display zipcodes table with city information"""
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT z.zipcode, z.banana, z.is_primary, c.name, c.state
                FROM zipcodes z
                JOIN jurisdictions c ON z.banana = c.banana
                ORDER BY z.zipcode
                LIMIT $1
                """,
                limit
            )

        if not rows:
            print("No zipcodes found.")
            return

        print(f"\n=== ZIPCODES TABLE (showing {len(rows)}) ===")
        print(f"{'Zipcode':<8} {'Primary':<8} {'City':<20} {'State':<6} {'Banana':<20}")
        print("-" * 70)

        for row in rows:
            primary = "YES" if row['is_primary'] else "NO"
            print(
                f"{row['zipcode']:<8} {primary:<8} "
                f"{row['name'][:19]:<20} {row['state']:<6} "
                f"{row['banana'][:19]:<20}"
            )

    async def show_meetings_table(self, limit: int = 20, city_filter: Optional[str] = None):
        """Display meetings table with city information"""
        if city_filter:
            # Search for matching cities
            all_cities = await self.db.cities.get_cities(status="active", limit=1000)
            matching_bananas = [
                c.banana
                for c in all_cities
                if city_filter.lower() in c.name.lower()
                or city_filter.lower() in c.banana.lower()
            ]

            if not matching_bananas:
                print("No matching cities found.")
                return

            # Get meetings for matching cities
            meetings = []
            for banana in matching_bananas[:10]:  # Limit cities to avoid too many queries
                city_meetings = await self.db.meetings.get_meetings_for_city(
                    banana, limit=limit
                )
                meetings.extend(city_meetings)

            # Sort by date and limit
            meetings.sort(key=lambda m: m.date or datetime.min, reverse=True)
            meetings = meetings[:limit]
        else:
            # Get recent meetings across all cities
            meetings = await self.db.meetings.get_recent_meetings(limit=limit)

        if not meetings:
            print("No meetings found.")
            return

        print(f"\n=== MEETINGS TABLE (showing {len(meetings)}) ===")
        print(
            f"{'ID':<12} {'City':<20} {'Title':<35} {'Date':<12} {'Items':<6} {'Status':<10}"
        )
        print("-" * 105)

        for meeting in meetings:
            city = await self.db.cities.get_city(meeting.banana)
            city_display = (
                f"{city.name[:15]}, {city.state}" if city else meeting.banana[:19]
            )

            title = (meeting.title or 'Unknown')[:34]

            # Format date
            date_str = ""
            if meeting.date:
                if isinstance(meeting.date, str):
                    date_str = meeting.date[:10]
                else:
                    date_str = meeting.date.strftime("%Y-%m-%d")

            # Get item count
            items = await self.db.items.get_agenda_items(meeting.id)
            item_count = str(len(items)) if items else "0"

            status = (meeting.status or '-')[:9]

            print(
                f"{meeting.id[:11]:<12} {city_display[:19]:<20} {title:<35} "
                f"{date_str:<12} {item_count:<6} {status:<10}"
            )

    async def show_agenda_items_table(self, limit: int = 20):
        """Display recent agenda items across all meetings"""
        # Get recent meetings
        meetings = await self.db.meetings.get_recent_meetings(limit=50)

        all_items = []
        for meeting in meetings:
            items = await self.db.items.get_agenda_items(meeting.id)
            for item in items:
                all_items.append({'item': item, 'meeting': meeting})
                if len(all_items) >= limit:
                    break
            if len(all_items) >= limit:
                break

        if not all_items:
            print("No agenda items found.")
            return

        print(f"\n=== AGENDA ITEMS (showing {len(all_items)}) ===")
        print(f"{'Item ID':<15} {'Meeting':<30} {'Title':<40} {'Summary':<8}")
        print("-" * 100)

        for row in all_items:
            item = row['item']
            meeting = row['meeting']

            meeting_title = (meeting.title or 'Unknown')[:29]
            item_title = (item.title or 'Unknown')[:39]
            has_summary = "YES" if item.summary else "NO"

            print(
                f"{item.id[:14]:<15} {meeting_title:<30} {item_title:<40} {has_summary:<8}"
            )

    async def show_queue_table(self, limit: int = 50):
        """Display processing queue"""
        stats = await self.db.queue.get_queue_stats()

        print("\n=== PROCESSING QUEUE STATISTICS ===")
        print(f"Pending:     {stats.get('pending_count', 0)}")
        print(f"Processing:  {stats.get('processing_count', 0)}")
        print(f"Completed:   {stats.get('completed_count', 0)}")
        print(f"Failed:      {stats.get('failed_count', 0)}")

        # Show pending/processing/failed items
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, job_type, payload, status, priority, retry_count, created_at
                FROM queue
                WHERE status IN ('pending', 'processing', 'failed')
                ORDER BY priority DESC, created_at ASC
                LIMIT $1
                """,
                limit
            )

        if rows:
            print(f"\n=== QUEUE ITEMS (showing {len(rows)}) ===")
            print(
                f"{'ID':<8} {'Type':<12} {'Status':<12} {'Priority':<9} {'Retries':<8} {'City':<15}"
            )
            print("-" * 80)

            for row in rows:
                payload = row['payload']
                banana = payload.get('banana', '')[:14] if payload else ''
                job_type = row['job_type'][:11]

                print(
                    f"{row['id']:<8} {job_type:<12} {row['status']:<12} {row['priority']:<9} "
                    f"{row['retry_count']:<8} {banana:<15}"
                )

    async def add_city(self):
        """Interactive city addition with auto-populated zipcodes and population."""
        print("\n=== ADD NEW CITY ===")

        try:
            city_name = input("City name: ").strip()
            if not city_name:
                print("City name required")
                return False

            state = input("State (2-letter code): ").strip().upper()
            if len(state) != 2:
                print("State must be 2-letter code (e.g., CA)")
                return False

            # Auto-lookup zipcodes, population, and geometry from Census
            print(f"Looking up {city_name}, {state}...")
            auto_zipcodes = lookup_zipcodes(city_name, state)
            auto_population = await lookup_census_population(self.db, city_name, state)
            auto_geometry = await lookup_census_geometry(self.db, city_name, state)

            if auto_zipcodes:
                print(f"   Found {len(auto_zipcodes)} zipcodes (uszipcode)")
            else:
                print("   No zipcodes found in uszipcode database")

            if auto_population:
                print(f"   Population: {auto_population:,} (Census 2023)")
            else:
                print("   No Census population found")

            if auto_geometry:
                print("   Geometry: found (Census TIGER)")
            else:
                print("   No Census geometry found")

            slug = input("Slug (vendor-specific): ").strip()
            if not slug:
                print("Slug required")
                return False

            vendor = input("Vendor (granicus/primegov/legistar/iqm2/etc): ").strip()
            if not vendor:
                print("Vendor required")
                return False

            county_banana = input("County banana (optional, e.g. alamedacountyCA): ").strip() or None

            if county_banana:
                parent = await self.db.cities.get_city(county_banana)
                if not parent:
                    print(f"   No jurisdiction found with banana '{county_banana}' -- skipping county link")
                    county_banana = None
                else:
                    print(f"   Linking to: {parent.name}, {parent.state}")

            # Allow manual zipcode override
            zipcodes_input = input(
                f"Zipcodes (comma-separated, Enter to use {len(auto_zipcodes)} auto-detected): "
            ).strip()
            if zipcodes_input:
                zipcodes = [z.strip() for z in zipcodes_input.split(",")]
            else:
                zipcodes = auto_zipcodes

            # Allow manual population override
            pop_default = f"{auto_population:,}" if auto_population else "none"
            pop_input = input(f"Population (Enter to use {pop_default}): ").strip()
            if pop_input:
                population = int(pop_input)
            else:
                population = auto_population

            # Generate banana
            banana = to_banana_slug(city_name) + state.upper()

            city = Jurisdiction(
                banana=banana,
                name=city_name,
                state=state,
                vendor=vendor,
                slug=slug,
                type="city",
                county_banana=county_banana,
                status='active',
                population=population
            )

            await self.db.cities.upsert_city(city)

            # Add zipcodes
            if zipcodes:
                async with self.db.pool.acquire() as conn:
                    for i, zipcode in enumerate(zipcodes):
                        is_primary = i == 0  # First zipcode is primary
                        await conn.execute(
                            """
                            INSERT INTO zipcodes (banana, zipcode, is_primary)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (banana, zipcode) DO UPDATE SET is_primary = $3
                            """,
                            banana, zipcode, is_primary
                        )

            # Add geometry if found
            if auto_geometry:
                async with self.db.pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE jurisdictions SET geom = $1 WHERE banana = $2",
                        auto_geometry, banana
                    )

            print(f"Added city '{city_name}, {state}' with banana {banana}")
            if population:
                print(f"   Population: {population:,}")
            if zipcodes:
                print(f"   Added {len(zipcodes)} zipcodes")
            if auto_geometry:
                print("   Added geometry")
            return True

        except KeyboardInterrupt:
            print("\n\nCancelled")
            return False
        except Exception as e:
            print(f"Error adding city: {e}")
            return False

    async def add_county(self):
        """Interactive county addition."""
        print("\n=== ADD NEW COUNTY ===")

        try:
            county_name = input("County name (e.g. Alameda): ").strip()
            if not county_name:
                print("County name required")
                return False

            state = input("State (2-letter code): ").strip().upper()
            if len(state) != 2:
                print("State must be 2-letter code (e.g., CA)")
                return False

            slug = input("Slug (vendor-specific): ").strip()
            if not slug:
                print("Slug required")
                return False

            vendor = input("Vendor (granicus/primegov/legistar/iqm2/etc): ").strip()
            if not vendor:
                print("Vendor required")
                return False

            pop_input = input("Population (optional): ").strip()
            population = int(pop_input) if pop_input else None

            # Generate banana: alamedacountyCA
            banana = to_banana_slug(county_name) + "county" + state.upper()

            # Display name includes "County"
            display_name = f"{county_name} County"

            county = Jurisdiction(
                banana=banana,
                name=display_name,
                state=state,
                vendor=vendor,
                slug=slug,
                type="county",
                status="active",
                population=population,
            )

            await self.db.cities.upsert_city(county)

            print(f"Added county '{display_name}, {state}' with banana {banana}")
            if population:
                print(f"   Population: {population:,}")

            # Offer to link existing cities to this county
            link = input(f"\nLink existing cities in {display_name} to this county? (y/N): ").strip().lower()
            if link == 'y':
                all_cities = await self.db.cities.get_cities(state=state)
                state_cities = [c for c in all_cities if c.type == 'city']
                if state_cities:
                    print(f"\nCities in {state}:")
                    for i, c in enumerate(state_cities):
                        linked = f" -> {c.county_banana}" if c.county_banana else ""
                        print(f"  {i+1}. {c.name} ({c.banana}){linked}")
                    indices = input("\nEnter numbers to link (comma-separated, or 'all'): ").strip()
                    if indices:
                        if indices.lower() == 'all':
                            to_link = state_cities
                        else:
                            to_link = []
                            for idx in indices.split(','):
                                idx = idx.strip()
                                if idx.isdigit() and 1 <= int(idx) <= len(state_cities):
                                    to_link.append(state_cities[int(idx) - 1])

                        async with self.db.pool.acquire() as conn:
                            for c in to_link:
                                await conn.execute(
                                    "UPDATE jurisdictions SET county_banana = $1 WHERE banana = $2",
                                    banana, c.banana
                                )
                        print(f"Linked {len(to_link)} cities to {display_name}")

            return True

        except KeyboardInterrupt:
            print("\n\nCancelled")
            return False
        except Exception as e:
            print(f"Error adding county: {e}")
            return False

    async def add_school_district(self):
        """Interactive school district addition.

        School districts are jurisdictions in their own right -- they hold
        meetings, publish agendas, and have governing boards. They do NOT
        live in census_places (boundaries are in TIGER tl_*_unsd / _elsd /
        _scsd shapefiles), so we skip the auto-geometry/population lookup
        that add_city does. Users supply population manually if known.
        """
        print("\n=== ADD NEW SCHOOL DISTRICT ===")

        try:
            district_name = input("District name (e.g. 'Prosper Independent School District'): ").strip()
            if not district_name:
                print("District name required")
                return False

            state = input("State (2-letter code): ").strip().upper()
            if len(state) != 2:
                print("State must be 2-letter code (e.g., CA)")
                return False

            # Auto-derive a short stem; let user override if the heuristic over-strips.
            # Mirrors the county convention: display name stays full, banana stays short.
            auto_stem = derive_district_stem(district_name)
            stem_input = input(f"Banana stem [auto: '{auto_stem}'] (Enter to accept): ").strip()
            stem = to_banana_slug(stem_input) if stem_input else auto_stem
            if not stem:
                print("Stem cannot be empty")
                return False
            banana = stem + "sd" + state
            print(f"   Banana will be: {banana}")

            slug = input("Slug (vendor-specific): ").strip()
            if not slug:
                print("Slug required")
                return False

            vendor = input("Vendor (granicus/primegov/legistar/iqm2/etc): ").strip()
            if not vendor:
                print("Vendor required")
                return False

            county_banana = input("Parent county banana (optional, e.g. losangelescountyCA): ").strip() or None
            if county_banana:
                parent = await self.db.cities.get_city(county_banana)
                if not parent:
                    print(f"   No jurisdiction found with banana '{county_banana}' -- skipping county link")
                    county_banana = None
                else:
                    print(f"   Linking to: {parent.name}, {parent.state}")

            pop_input = input("Population (optional, district enrollment or service-area): ").strip()
            population = int(pop_input) if pop_input else None

            district = Jurisdiction(
                banana=banana,
                name=district_name,
                state=state,
                vendor=vendor,
                slug=slug,
                type="school_district",
                county_banana=county_banana,
                status="active",
                population=population,
            )

            await self.db.cities.upsert_city(district)

            print(f"Added school district '{district_name}, {state}' with banana {banana}")
            if population:
                print(f"   Population: {population:,}")
            if county_banana:
                print(f"   Linked to county: {county_banana}")
            return True

        except KeyboardInterrupt:
            print("\n\nCancelled")
            return False
        except Exception as e:
            print(f"Error adding school district: {e}")
            return False

    async def update_city(self):
        """Update city information - continuous edit mode"""
        current_banana = None

        while True:
            try:
                if not current_banana:
                    print("\n=== UPDATE CITY ===")
                    await self.show_cities_table(20)

                    banana = input("\nEnter banana to update (or 'q' to quit): ").strip()
                    if not banana or banana.lower() == 'q':
                        return False

                    # Get current city
                    city = await self.db.cities.get_city(banana)
                    if not city:
                        print(f"No city found with banana {banana}")
                        continue

                    current_banana = banana
                else:
                    # Refresh city data
                    city = await self.db.cities.get_city(current_banana)
                    if not city:
                        print(f"City {current_banana} no longer exists")
                        current_banana = None
                        continue

                print(f"\n=== {city.name}, {city.state} ({current_banana}) ===")
                print(f"  name:   {city.name}")
                print(f"  state:  {city.state}")
                print(f"  vendor: {city.vendor}")
                print(f"  slug:   {city.slug}")
                print(f"  type:           {city.type}")
                print(f"  county_banana:  {city.county_banana or 'None'}")
                print(f"  status: {(city.status or 'active')}")

                field = input(
                    "\nField to update (name/state/slug/vendor/status/type/county_banana) or 'q' to quit: "
                ).strip()

                if not field or field.lower() == 'q':
                    current_banana = None
                    continue

                valid_fields = ['name', 'state', 'slug', 'vendor', 'status', 'type', 'county_banana']
                if field not in valid_fields:
                    print(f"Invalid field. Valid: {', '.join(valid_fields)}")
                    continue

                new_value = input(f"New value for {field} (or 'cancel' to skip): ").strip()

                if new_value.lower() == 'cancel':
                    continue

                if not new_value and field not in ('county_banana', 'type'):
                    print("Value cannot be empty (except county)")
                    continue

                # Update the field
                setattr(city, field, new_value if new_value else None)

                # If updating name or state, recalculate banana
                if field in ['name', 'state']:
                    new_banana = to_banana_slug(city.name) + city.state.upper()

                    if new_banana != current_banana:
                        # Need to update banana and all foreign keys
                        async with self.db.pool.acquire() as conn:
                            async with conn.transaction():
                                # Update city
                                await conn.execute(
                                    """
                                    UPDATE jurisdictions
                                    SET name = $1, state = $2, banana = $3, updated_at = NOW()
                                    WHERE banana = $4
                                    """,
                                    city.name, city.state, new_banana, current_banana
                                )

                                # Update foreign keys
                                await conn.execute(
                                    "UPDATE meetings SET banana = $1 WHERE banana = $2",
                                    new_banana, current_banana
                                )
                                await conn.execute(
                                    "UPDATE city_matters SET banana = $1 WHERE banana = $2",
                                    new_banana, current_banana
                                )

                        print(f"Updated city and banana: {current_banana} → {new_banana}")
                        current_banana = new_banana
                    else:
                        await self.db.cities.upsert_city(city)
                        print(f"Updated {field} = '{new_value}'")
                else:
                    await self.db.cities.upsert_city(city)
                    print(f"Updated {field} = '{new_value}'")

            except KeyboardInterrupt:
                print("\n\nReturning to main menu...")
                return False
            except Exception as e:
                print(f"Error updating city: {e}")
                continue

    async def search_database(self):
        """Search across cities and meetings"""
        print("\n=== SEARCH DATABASE ===")

        try:
            query = input("Search for: ").strip()
            if not query:
                print("Search query required")
                return
        except (KeyboardInterrupt, EOFError):
            print("\nSearch cancelled")
            return

        print(f"\nSearching for '{query}'...")

        results = []

        # Search cities
        all_cities = await self.db.cities.get_cities(status="active", limit=1000)
        for city in all_cities:
            if (
                query.lower() in city.name.lower()
                or query.lower() in city.state.lower()
                or query.lower() in city.banana.lower()
                or query.lower() in city.slug.lower()
                or query.lower() in city.vendor.lower()
            ):
                results.append({
                    'type': 'CITY',
                    'id': city.banana,
                    'name': f"{city.name}, {city.state}",
                    'info': f"{city.banana} ({city.vendor})",
                    'extra': city.slug
                })

        # Search zipcodes
        async with self.db.pool.acquire() as conn:
            zipcode_rows = await conn.fetch(
                """
                SELECT z.zipcode, z.banana, z.is_primary, c.name, c.state
                FROM zipcodes z
                JOIN jurisdictions c ON z.banana = c.banana
                WHERE z.zipcode LIKE $1
                """,
                f"%{query}%"
            )

        for row in zipcode_rows:
            results.append({
                'type': 'ZIPCODE',
                'id': row['zipcode'],
                'name': row['zipcode'],
                'info': f"{row['name']}, {row['state']}",
                'extra': row['banana']
            })

        # Search meetings (title and banana)
        for city in all_cities:
            if query.lower() in city.banana.lower():
                meetings = await self.db.meetings.get_meetings_for_city(city.banana, limit=10)
                for meeting in meetings:
                    date_str = ''
                    if meeting.date:
                        if isinstance(meeting.date, str):
                            date_str = meeting.date[:10]
                        else:
                            date_str = meeting.date.strftime("%Y-%m-%d")

                    results.append({
                        'type': 'MEETING',
                        'id': meeting.id[:10],
                        'name': (meeting.title or 'Unknown')[:40],
                        'info': f"{city.name}, {city.state}",
                        'extra': date_str
                    })

        if not results:
            print("No results found")
            return

        print(f"\nFound {len(results)} results:")
        print(f"{'Type':<10} {'ID':<12} {'Name':<40} {'Info':<30} {'Extra':<20}")
        print("-" * 115)

        for result in results[:50]:
            print(
                f"{result['type']:<10} {result['id'][:11]:<12} {result['name'][:39]:<40} "
                f"{result['info'][:29]:<30} {result['extra'][:19]:<20}"
            )

        if len(results) > 50:
            print(f"\n... and {len(results) - 50} more results")

    async def show_statistics(self):
        """Show database statistics"""
        # Get counts
        async with self.db.pool.acquire() as conn:
            city_count = await conn.fetchval("SELECT COUNT(*) FROM jurisdictions WHERE status = 'active'")
            meeting_count = await conn.fetchval("SELECT COUNT(*) FROM meetings")
            item_count = await conn.fetchval("SELECT COUNT(*) FROM agenda_items")
            matter_count = await conn.fetchval("SELECT COUNT(*) FROM city_matters")

            summarized_meetings = await conn.fetchval(
                "SELECT COUNT(*) FROM meetings WHERE summary IS NOT NULL"
            )

            summarized_items = await conn.fetchval(
                "SELECT COUNT(*) FROM agenda_items WHERE summary IS NOT NULL"
            )

            vendor_breakdown = await conn.fetch(
                """
                SELECT vendor, COUNT(*) as count
                FROM jurisdictions
                WHERE vendor IS NOT NULL AND status = 'active'
                GROUP BY vendor
                ORDER BY count DESC
                """
            )

        print("\n=== DATABASE STATISTICS ===")
        print(f"Active cities:       {city_count}")
        print(f"Total meetings:      {meeting_count}")
        print(f"Summarized meetings: {summarized_meetings}")
        print(f"Agenda items:        {item_count}")
        print(f"Summarized items:    {summarized_items}")
        print(f"Unique matters:      {matter_count}")

        if meeting_count > 0:
            summary_rate = (summarized_meetings / meeting_count) * 100
            print(f"Meeting summary rate: {summary_rate:.1f}%")

        if vendor_breakdown:
            print("\nVendor breakdown:")
            for vendor in vendor_breakdown:
                percentage = (vendor['count'] / city_count) * 100 if city_count > 0 else 0
                print(f"  {vendor['vendor']:<15} {vendor['count']:>4} cities ({percentage:.1f}%)")

        # Queue stats
        queue_stats = await self.db.queue.get_queue_stats()
        if any(queue_stats.values()):
            print("\nProcessing queue:")
            print(f"  Pending:    {queue_stats.get('pending_count', 0)}")
            print(f"  Processing: {queue_stats.get('processing_count', 0)}")
            print(f"  Completed:  {queue_stats.get('completed_count', 0)}")
            print(f"  Failed:     {queue_stats.get('failed_count', 0)}")

    async def search_meeting_summaries(self):
        """Search within meeting and item summaries using PostgreSQL full-text search"""
        print("\n=== SEARCH SUMMARIES ===")

        try:
            search_term = input("Search term: ").strip()
            if not search_term:
                print("Search term required")
                return
        except (KeyboardInterrupt, EOFError):
            print("\nSearch cancelled")
            return

        print(f"\nSearching for '{search_term}'...\n")

        try:
            # Search meetings
            async with self.db.pool.acquire() as conn:
                meeting_results = await conn.fetch(
                    """
                    SELECT m.id, m.banana, m.title, m.date, m.summary, m.agenda_url, m.packet_url,
                           c.name as city_name, c.state
                    FROM meetings m
                    JOIN jurisdictions c ON m.banana = c.banana
                    WHERE m.summary ILIKE $1
                    ORDER BY m.date DESC
                    LIMIT 20
                    """,
                    f"%{search_term}%"
                )

                item_results = await conn.fetch(
                    """
                    SELECT i.id, i.meeting_id, i.title, i.summary, i.attachments,
                           m.banana, m.title as meeting_title, m.date, m.agenda_url,
                           c.name as city_name, c.state
                    FROM items i
                    JOIN meetings m ON i.meeting_id = m.id
                    JOIN jurisdictions c ON m.banana = c.banana
                    WHERE i.summary ILIKE $1
                    ORDER BY m.date DESC
                    LIMIT 20
                    """,
                    f"%{search_term}%"
                )

            total_results = len(meeting_results) + len(item_results)

            if total_results == 0:
                print("No results found")
                return

            print(f"Found {total_results} results ({len(meeting_results)} meetings, {len(item_results)} items)\n")
            print("=" * 100)

            # Display meeting results
            for i, result in enumerate(meeting_results, 1):
                print(f"\n[Meeting Result {i}]")
                print(f"City: {result['city_name']}, {result['state']}")
                print(f"Date: {result['date'].strftime('%Y-%m-%d') if result['date'] else 'N/A'}")
                print(f"Meeting: {result['title']}")

                if result.get('agenda_url'):
                    print(f"Agenda URL: {result['agenda_url']}")
                if result.get('packet_url'):
                    print(f"Packet URL: {result['packet_url']}")

                # Show context snippet
                summary = result.get('summary', '')
                if summary:
                    # Find context around search term
                    search_lower = search_term.lower()
                    summary_lower = summary.lower()
                    idx = summary_lower.find(search_lower)

                    if idx >= 0:
                        start = max(0, idx - 100)
                        end = min(len(summary), idx + len(search_term) + 100)
                        context = summary[start:end]
                        if start > 0:
                            context = "..." + context
                        if end < len(summary):
                            context = context + "..."
                        print(f"\nContext: {context}")

                print("\n" + "=" * 100)

            # Display item results
            for i, result in enumerate(item_results, 1):
                print(f"\n[Item Result {i}]")
                print(f"City: {result['city_name']}, {result['state']}")
                print(f"Date: {result['date'].strftime('%Y-%m-%d') if result['date'] else 'N/A'}")
                print(f"Meeting: {result['meeting_title']}")
                print(f"Item: {result['title']}")

                if result.get('agenda_url'):
                    print(f"Agenda URL: {result['agenda_url']}")

                if result.get('attachments'):
                    print(f"Attachments: {len(result['attachments'])}")

                # Show context snippet
                summary = result.get('summary', '')
                if summary:
                    search_lower = search_term.lower()
                    summary_lower = summary.lower()
                    idx = summary_lower.find(search_lower)

                    if idx >= 0:
                        start = max(0, idx - 100)
                        end = min(len(summary), idx + len(search_term) + 100)
                        context = summary[start:end]
                        if start > 0:
                            context = "..." + context
                        if end < len(summary):
                            context = context + "..."
                        print(f"\nContext: {context}")

                print("\n" + "=" * 100)

        except Exception as e:
            print(f"Error searching summaries: {e}")
            import traceback
            traceback.print_exc()


async def main_loop():
    viewer = DatabaseViewer()
    await viewer.initialize()

    try:
        while True:
            print("\n" + "=" * 60)
            print("ENGAGIC DATABASE VIEWER v5.0 (PostgreSQL)")
            print("=" * 60)
            print("View Data:")
            print("  1. Cities")
            print("  2. Zipcodes")
            print("  3. Meetings")
            print("  4. Agenda items")
            print("  5. Processing queue")
            print("  6. Statistics")
            print("\nEdit Data:")
            print("  7. Add city")
            print("  8. Add county")
            print("  9. Add school district")
            print("  10. Update jurisdiction")
            print("\nSearch & Analysis:")
            print("  11. Search database (cities, zipcodes, meetings)")
            print("  12. Search summaries (full-text summary search)")
            print("\nOther:")
            print("  0. Exit")

            try:
                choice = input("\nChoice: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n\nExiting...")
                break

            if choice == "1":
                limit_input = input("How many cities? (default 50): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 50
                await viewer.show_cities_table(limit)

            elif choice == "2":
                limit_input = input("How many zipcodes? (default 50): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 50
                await viewer.show_zipcodes_table(limit)

            elif choice == "3":
                city_filter = input(
                    "Filter by city (optional, press Enter to skip): "
                ).strip()
                limit_input = input("How many meetings? (default 20): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 20
                await viewer.show_meetings_table(limit, city_filter if city_filter else None)

            elif choice == "4":
                limit_input = input("How many items? (default 20): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 20
                await viewer.show_agenda_items_table(limit)

            elif choice == "5":
                limit_input = input("How many queue items? (default 50): ").strip()
                limit = int(limit_input) if limit_input.isdigit() else 50
                await viewer.show_queue_table(limit)

            elif choice == "6":
                await viewer.show_statistics()

            elif choice == "7":
                await viewer.add_city()

            elif choice == "8":
                await viewer.add_county()

            elif choice == "9":
                await viewer.add_school_district()

            elif choice == "10":
                await viewer.update_city()

            elif choice == "11":
                await viewer.search_database()

            elif choice == "12":
                await viewer.search_meeting_summaries()

            elif choice == "0":
                print("Goodbye!")
                break

            else:
                print("Invalid choice.")

    finally:
        await viewer.close()


def main():
    """Entry point - run async main loop"""
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
