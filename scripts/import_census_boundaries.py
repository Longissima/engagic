#!/usr/bin/env python3
"""Import Census TIGER/Line Place boundaries into cities.geom

Downloads Census PLACE shapefiles and matches them to our cities table.

Usage:
    python scripts/import_census_boundaries.py --download   # Download shapefiles
    python scripts/import_census_boundaries.py --import     # Import to staging table
    python scripts/import_census_boundaries.py --match      # Match to cities
    python scripts/import_census_boundaries.py --counties   # County boundaries (carto 500k)
    python scripts/import_census_boundaries.py --all        # Full pipeline

Requirements:
    - PostGIS extension enabled
    - ogr2ogr (gdal-bin package)
    - wget
"""

import argparse
import asyncio
import subprocess
import re
from pathlib import Path

import asyncpg

from config import config, get_logger

logger = get_logger(__name__).bind(component="census_import")

# Census TIGER/Line FTP base URL
TIGER_BASE_URL = "https://www2.census.gov/geo/tiger/TIGER2023/PLACE"

# County boundaries come from the cartographic boundary series, not TIGER/Line:
# county legal boundaries extend into open water (Great Lakes, coastal buffers),
# which reads as wrong on display surfaces. The carto file is shoreline-clipped,
# pre-generalized to 1:500k, and one national zip covers every state.
CARTO_COUNTY_FILENAME = "cb_2023_us_county_500k.zip"
CARTO_COUNTY_URL = f"https://www2.census.gov/geo/tiger/GENZ2023/shp/{CARTO_COUNTY_FILENAME}"

# State FIPS codes
STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
    "CO": "08", "CT": "09", "DE": "10", "FL": "12", "GA": "13",
    "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
    "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29",
    "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34",
    "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45",
    "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50",
    "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56",
    "DC": "11", "PR": "72",
}

# Reverse lookup
FIPS_TO_STATE = {v: k for k, v in STATE_FIPS.items()}

# Data directory
DATA_DIR = Path("/opt/engagic/data/census")


def get_states_we_track() -> set[str]:
    """Get unique states from our cities table."""
    # This will be populated from the database
    return set(STATE_FIPS.keys())


async def download_shapefiles(states: set[str] | None = None) -> None:
    """Download Census PLACE shapefiles for specified states."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if states is None:
        # Get states we actually track from the database
        dsn = config.get_postgres_dsn()
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch("SELECT DISTINCT state FROM jurisdictions")
            states = {row["state"] for row in rows}
        finally:
            await conn.close()

    logger.info("downloading shapefiles", states=sorted(states))

    for state in sorted(states):
        fips = STATE_FIPS.get(state)
        if not fips:
            logger.warning("unknown state FIPS", state=state)
            continue

        filename = f"tl_2023_{fips}_place.zip"
        url = f"{TIGER_BASE_URL}/{filename}"
        dest = DATA_DIR / filename

        if dest.exists():
            logger.debug("shapefile exists, skipping", state=state)
            continue

        logger.info("downloading", state=state, url=url)
        result = subprocess.run(
            ["wget", "-q", "-O", str(dest), url],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("download failed", state=state, stderr=result.stderr)
            dest.unlink(missing_ok=True)

    logger.info("download complete")


async def import_to_staging() -> None:
    """Import shapefiles to census_places staging table using ogr2ogr."""
    dsn = config.get_postgres_dsn()

    # Build ogr2ogr connection string
    # Parse DSN to get components
    conn = await asyncpg.connect(dsn)
    try:
        # Drop and recreate staging table
        await conn.execute("DROP TABLE IF EXISTS census_places CASCADE")
        logger.info("dropped existing census_places table")
    finally:
        await conn.close()

    # ogr2ogr connection string format
    pg_conn = f"PG:{dsn}"

    # Find all downloaded shapefiles
    shapefiles = sorted(DATA_DIR.glob("tl_2023_*_place.zip"))
    if not shapefiles:
        logger.error("no shapefiles found", directory=str(DATA_DIR))
        return

    logger.info("importing shapefiles", count=len(shapefiles))

    for i, shapefile in enumerate(shapefiles):
        # Extract state FIPS from filename
        match = re.search(r"tl_2023_(\d{2})_place\.zip", shapefile.name)
        if not match:
            continue

        state_fips = match.group(1)
        state = FIPS_TO_STATE.get(state_fips, "??")

        # ogr2ogr reads directly from zip
        vsi_path = f"/vsizip/{shapefile}"

        # First shapefile creates table, subsequent ones append
        mode = "-overwrite" if i == 0 else "-append"

        logger.info("importing", state=state, shapefile=shapefile.name)

        result = subprocess.run(
            [
                "ogr2ogr",
                "-f", "PostgreSQL",
                pg_conn,
                vsi_path,
                "-nln", "census_places",
                "-nlt", "MULTIPOLYGON",
                "-t_srs", "EPSG:4326",
                mode,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("ogr2ogr failed", state=state, stderr=result.stderr)

    # Create index on staging table
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_census_places_name
            ON census_places (UPPER(name))
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_census_places_statefp
            ON census_places (statefp)
        """)

        # Get count
        count = await conn.fetchval("SELECT COUNT(*) FROM census_places")
        logger.info("import complete", total_places=count)
    finally:
        await conn.close()


def get_name_variations(name: str, state: str) -> list[str]:
    """Generate name variations to try for Census matching.

    Handles:
    - Hyphens (Winston Salem -> Winston-Salem)
    - Township suffixes (Canton -> Canton charter township)
    - Saint/St. variations
    - Opa Locka -> Opa-locka
    """
    name_upper = name.strip().upper()
    variations = [name_upper]

    # Hyphenated version
    if ' ' in name_upper:
        variations.append(name_upper.replace(' ', '-'))

    # Saint/St. variations
    if name_upper.startswith('SAINT '):
        variations.append('ST. ' + name_upper[6:])
    elif name_upper.startswith('ST ') or name_upper.startswith('ST. '):
        base = re.sub(r'^ST\.?\s+', '', name_upper)
        variations.append('SAINT ' + base)

    # Township variations for MI, NJ, PA
    if state.upper() in ('MI', 'NJ', 'PA'):
        variations.append(f"{name_upper} TOWNSHIP")
        variations.append(f"{name_upper} CHARTER TOWNSHIP")
        if name_upper.endswith(' TOWNSHIP'):
            base = name_upper[:-9]
            variations.append(base)
            variations.append(f"{base} CHARTER TOWNSHIP")

    # Opa Locka special case
    if 'OPA ' in name_upper:
        variations.append(name_upper.replace('OPA ', 'OPA-'))

    # City/town suffixes
    variations.append(f"{name_upper} CITY")
    variations.append(f"{name_upper} TOWN")

    # Expand abbreviations for fuzzy matching
    expanded = name_upper
    expanded = re.sub(r"\bFT\.?\b", "FORT", expanded)
    expanded = re.sub(r"\bMT\.?\b", "MOUNT", expanded)
    if expanded != name_upper:
        variations.append(expanded)

    return variations


async def match_cities() -> None:
    """Match our cities to Census places and populate geom column."""
    dsn = config.get_postgres_dsn()
    conn = await asyncpg.connect(dsn)

    try:
        # Get our cities without geometry
        # Cities only: the fuzzy fallback below once handed Clallam County a
        # 0.6 sq mi place polygon. Counties match in match_counties(); other
        # types (school_district, ...) have no PLACE representation at all.
        cities = await conn.fetch("""
            SELECT banana, name, state
            FROM jurisdictions
            WHERE geom IS NULL AND type = 'city'
            ORDER BY state, name
        """)

        logger.info("matching cities", total=len(cities))

        matched = 0
        unmatched = []

        for city in cities:
            banana = city["banana"]
            name = city["name"]
            state = city["state"]
            state_fips = STATE_FIPS.get(state)

            if not state_fips:
                logger.warning("unknown state", banana=banana, state=state)
                unmatched.append({"banana": banana, "reason": "unknown_state"})
                continue

            # Try name variations
            place = None
            variations = get_name_variations(name, state)
            for variation in variations:
                place = await conn.fetchrow("""
                    SELECT wkb_geometry
                    FROM census_places
                    WHERE UPPER(name) = $1 AND statefp = $2
                """, variation, state_fips)
                if place:
                    break

            # Fallback: fuzzy LIKE match
            if not place:
                place = await conn.fetchrow("""
                    SELECT wkb_geometry
                    FROM census_places
                    WHERE UPPER(name) LIKE $1 AND statefp = $2
                """, f"%{name.upper()}%", state_fips)

            if place:
                await conn.execute("""
                    UPDATE jurisdictions SET geom = $1 WHERE banana = $2
                """, place["wkb_geometry"], banana)
                matched += 1
                logger.debug("matched", banana=banana, name=name)
            else:
                unmatched.append({"banana": banana, "name": name, "state": state})
                logger.warning("no match", banana=banana, name=name, state=state)

        logger.info("matching complete", matched=matched, unmatched=len(unmatched))

        # Report unmatched for manual review
        if unmatched:
            logger.info("unmatched cities require manual review:")
            for city in unmatched[:20]:  # First 20
                print(f"  {city.get('banana', '?')}: {city.get('name', '?')}, {city.get('state', '?')}")
            if len(unmatched) > 20:
                print(f"  ... and {len(unmatched) - 20} more")

    finally:
        await conn.close()


async def download_county_shapefile() -> None:
    """Download the national cartographic boundary county shapefile."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / CARTO_COUNTY_FILENAME

    if dest.exists():
        logger.debug("county shapefile exists, skipping")
        return

    logger.info("downloading county boundaries", url=CARTO_COUNTY_URL)
    result = subprocess.run(
        ["wget", "-q", "-O", str(dest), CARTO_COUNTY_URL],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("download failed", stderr=result.stderr)
        dest.unlink(missing_ok=True)


async def import_counties_to_staging() -> None:
    """Import county shapefile to census_counties staging table."""
    dsn = config.get_postgres_dsn()

    shapefile = DATA_DIR / CARTO_COUNTY_FILENAME
    if not shapefile.exists():
        logger.error("county shapefile not found", path=str(shapefile))
        return

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS census_counties CASCADE")
    finally:
        await conn.close()

    result = subprocess.run(
        [
            "ogr2ogr",
            "-f", "PostgreSQL",
            f"PG:{dsn}",
            f"/vsizip/{shapefile}",
            "-nln", "census_counties",
            "-nlt", "MULTIPOLYGON",
            "-t_srs", "EPSG:4326",
            "-overwrite",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("ogr2ogr failed", stderr=result.stderr)
        return

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_census_counties_namelsad
            ON census_counties (UPPER(namelsad), stusps)
        """)
        count = await conn.fetchval("SELECT COUNT(*) FROM census_counties")
        logger.info("county import complete", total_counties=count)
    finally:
        await conn.close()


# Consolidated city-counties whose registered name differs from the Census
# county record that carries their territory.
COUNTY_ALIASES = {
    ("MACON BIBB COUNTY", "GA"): "BIBB COUNTY",
}


async def match_counties() -> None:
    """Match county jurisdictions to Census counties and populate geom.

    Overwrites existing geom on match: any geometry a county row carried
    before this existed came from the place matcher and is wrong.
    """
    dsn = config.get_postgres_dsn()
    conn = await asyncpg.connect(dsn)

    try:
        counties = await conn.fetch("""
            SELECT banana, name, state
            FROM jurisdictions
            WHERE type = 'county'
            ORDER BY state, name
        """)

        logger.info("matching counties", total=len(counties))

        matched = 0
        unmatched = []

        for county in counties:
            name_upper = county["name"].strip().upper()
            name_upper = COUNTY_ALIASES.get((name_upper, county["state"]), name_upper)

            # NAMELSAD carries the full legal name ("Oakland County",
            # "Terrebonne Parish"); NAME is the bare name ("Oakland").
            row = await conn.fetchrow("""
                SELECT wkb_geometry
                FROM census_counties
                WHERE UPPER(namelsad) = $1 AND stusps = $2
            """, name_upper, county["state"])
            if not row:
                row = await conn.fetchrow("""
                    SELECT wkb_geometry
                    FROM census_counties
                    WHERE UPPER(name) = $1 AND stusps = $2
                """, name_upper, county["state"])

            if row:
                await conn.execute("""
                    UPDATE jurisdictions SET geom = $1 WHERE banana = $2
                """, row["wkb_geometry"], county["banana"])
                matched += 1
            else:
                unmatched.append(county)
                logger.warning(
                    "no county match",
                    banana=county["banana"], name=county["name"], state=county["state"],
                )

        logger.info("county matching complete", matched=matched, unmatched=len(unmatched))
        for county in unmatched:
            print(f"  {county['banana']}: {county['name']}, {county['state']}")

    finally:
        await conn.close()


async def report_status() -> None:
    """Report geometry coverage status."""
    dsn = config.get_postgres_dsn()
    conn = await asyncpg.connect(dsn)

    try:
        total = await conn.fetchval("SELECT COUNT(*) FROM jurisdictions")
        with_geom = await conn.fetchval("SELECT COUNT(*) FROM jurisdictions WHERE geom IS NOT NULL")
        without_geom = await conn.fetchval("SELECT COUNT(*) FROM jurisdictions WHERE geom IS NULL")

        print("\nGeometry Coverage:")
        print(f"  Total jurisdictions: {total}")
        print(f"  With geometry: {with_geom} ({100*with_geom/total:.1f}%)")
        print(f"  Without geometry: {without_geom}")

        print("\nBy Type:")
        rows = await conn.fetch("""
            SELECT type, COUNT(*) AS total, COUNT(geom) AS with_geom
            FROM jurisdictions
            GROUP BY type
            ORDER BY COUNT(*) DESC
        """)
        for row in rows:
            print(f"  {row['type']}: {row['with_geom']} of {row['total']}")

        # By state
        print("\nBy State (top 10 missing):")
        rows = await conn.fetch("""
            SELECT state, COUNT(*) as total,
                   COUNT(*) FILTER (WHERE geom IS NOT NULL) as with_geom
            FROM jurisdictions
            GROUP BY state
            HAVING COUNT(*) FILTER (WHERE geom IS NULL) > 0
            ORDER BY COUNT(*) FILTER (WHERE geom IS NULL) DESC
            LIMIT 10
        """)
        for row in rows:
            missing = row["total"] - row["with_geom"]
            print(f"  {row['state']}: {missing} missing of {row['total']}")

    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Import Census TIGER boundaries")
    parser.add_argument("--download", action="store_true", help="Download shapefiles")
    parser.add_argument("--import", dest="import_", action="store_true", help="Import to staging")
    parser.add_argument("--match", action="store_true", help="Match to cities")
    parser.add_argument("--counties", action="store_true", help="Download + import + match county boundaries")
    parser.add_argument("--status", action="store_true", help="Report status")
    parser.add_argument("--all", action="store_true", help="Full pipeline")
    args = parser.parse_args()

    if args.all or args.download:
        await download_shapefiles()

    if args.all or args.import_:
        await import_to_staging()

    if args.all or args.match:
        await match_cities()

    if args.all or args.counties:
        await download_county_shapefile()
        await import_counties_to_staging()
        await match_counties()

    if args.status or args.all:
        await report_status()

    if not any([args.download, args.import_, args.match, args.counties, args.status, args.all]):
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
