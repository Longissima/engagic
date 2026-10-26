"""One onboarding flow for every jurisdiction kind.

The operator picks a kind from the jurisdiction_kinds registry; its class
(type) follows. Only geographic enrichment and county linking depend on the
class; identity and vendor configuration do not. There is no free-text entry:
that prompt grew 60 types by 2026-10, so a new kind is added by migration.
"""

import re

from database.models import Jurisdiction
from scripts._jurisdiction_geography import (
    lookup_census_geometry,
    lookup_census_population,
    lookup_zipcodes,
)
from scripts._jurisdiction_naming import make_banana


async def select_kind(db) -> tuple[str, str]:
    """(kind, class) picked from the registry, general-purpose classes first."""
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT kind, type, label FROM jurisdiction_kinds
               ORDER BY array_position(ARRAY['city','county','school_district','state','special_district'], type), label"""
        )
    for index, row in enumerate(rows, 1):
        print(f"  {index}. {row['label']} ({row['type']})")
    choice = input("Kind (number): ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
        raise ValueError("Pick a listed kind; new kinds are added to jurisdiction_kinds by migration")
    row = rows[int(choice) - 1]
    return row["kind"], row["type"]


def required(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        raise ValueError("This field is required")
    return value


async def collect_jurisdiction(db, kind: str, cls: str) -> tuple[Jurisdiction, bytes | None]:
    name = required("Jurisdiction name (full name, including County/District/etc.): ")
    state = required("State (2-letter code): ").upper()
    if not re.fullmatch(r"[A-Z]{2}", state):
        raise ValueError("State must be a 2-letter code")
    vernacular = input("Common abbreviation for banana (optional, e.g. PAUSD): ").strip()
    banana = make_banana(name, state, vernacular or None)
    print(f"   Banana: {banana}")
    if await db.jurisdictions.get_city(banana):
        raise ValueError(f"{banana} already exists; use Update jurisdiction")
    slug = required("Slug (vendor-specific): ")
    vendor = required("Vendor (granicus/primegov/legistar/boardbook/etc.): ").lower()
    county_banana = None
    if cls != "county":
        county_banana = input("Parent county banana (optional; leave blank for regional bodies): ").strip() or None
        if county_banana:
            parent = await db.jurisdictions.get_city(county_banana)
            if not parent or parent.type != "county" or parent.state != state:
                raise ValueError("Parent must be an existing county in the same state")

    zipcodes, population, geometry = [], None, None
    if kind == "city":
        print(f"Looking up city ZIPs and Census data for {name}, {state}...")
        zipcodes = lookup_zipcodes(name, state)
        population = await lookup_census_population(db, name, state)
        geometry = await lookup_census_geometry(db, name, state)
        print(f"   {len(zipcodes)} ZIPs; population: {population}; boundary: {'found' if geometry else 'none'}")
    entered_zips = input(f"ZIPs (comma-separated; Enter keeps {len(zipcodes)} defaults): ").strip()
    if entered_zips:
        zipcodes = list(dict.fromkeys(z.strip() for z in entered_zips.split(',')))
    if any(not re.fullmatch(r"[0-9]{5}", z) for z in zipcodes):
        raise ValueError("ZIP codes must each contain five digits")
    entered_population = input(f"Service-area population (Enter keeps {population}): ").strip()
    if entered_population:
        population = int(entered_population.replace(',', ''))
    if population is not None and not 0 <= population <= 2**31 - 1:
        raise ValueError("Population must be a nonnegative PostgreSQL integer")
    return Jurisdiction(
        banana=banana, name=name, state=state, type=cls, kind=kind,
        vendor=vendor, slug=slug, county_banana=county_banana,
        population=population, zipcodes=zipcodes, status="active",
    ), geometry


async def link_county_cities(db, county: Jurisdiction) -> None:
    if input("Link existing cities to this county? (y/N): ").strip().lower() != 'y':
        return
    cities = [c for c in await db.jurisdictions.get_cities(state=county.state) if c.type == 'city']
    if not cities:
        print("No active cities found in this state")
        return
    for index, city in enumerate(cities, 1):
        print(f"  {index}. {city.name} ({city.banana}) -> {city.county_banana or 'unlinked'}")
    choice = input("City numbers (comma-separated, or 'all'): ").strip()
    if not choice:
        return
    selected = cities
    if choice.lower() != 'all':
        indices = [int(i.strip()) - 1 for i in choice.split(',')]
        if any(i < 0 or i >= len(cities) for i in indices):
            raise ValueError("Invalid city selection")
        selected = [cities[i] for i in dict.fromkeys(indices)]
    async with db.jurisdictions.transaction() as conn:
        await conn.executemany(
            "UPDATE jurisdictions SET county_banana = $1 WHERE banana = $2",
            [(county.banana, city.banana) for city in selected],
        )
    print(f"Linked {len(selected)} cities to {county.name}")


async def add_jurisdiction(db) -> bool:
    print("\n=== ADD JURISDICTION ===")
    try:
        kind, cls = await select_kind(db)
        jurisdiction, geometry = await collect_jurisdiction(db, kind, cls)
        await db.jurisdictions.add_city(jurisdiction, geometry=geometry)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled")
        return False
    except Exception as exc:
        print(f"Error adding jurisdiction: {exc}")
        return False
    print(f"Added {kind} '{jurisdiction.name}, {jurisdiction.state}' ({jurisdiction.banana})")
    if cls == 'county':
        try:
            await link_county_cities(db, jurisdiction)
        except (KeyboardInterrupt, EOFError, Exception) as exc:
            print(f"County was added; city linking was not completed: {exc}")
    return True
