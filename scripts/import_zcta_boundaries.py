#!/usr/bin/env python3
"""Import Census ZCTA (ZIP Code Tabulation Area) boundaries and backfill zipcodes.

Replaces the legacy uszipcode population path, which truncated every city to its
five lowest-numbered ZIPs (uszipcode's by_city/query defaults to returns=5). The
fix is spatial and Census-sourced: load the national ZCTA polygons, then assign a
ZCTA to a jurisdiction when their boundaries overlap meaningfully.

A ZCTA is assigned to a jurisdiction when the intersection area is at least
ASSIGN_FRACTION of the SMALLER of the two polygons. Using the smaller area handles
both nesting directions:
  - dense urban ZCTAs nested inside a large city (intersection ~= ZCTA area)
  - a small town fully inside one large rural ZCTA (intersection ~= town area)
while dropping incidental edge-clips along shared borders.

Usage:
    uv run scripts/import_zcta_boundaries.py --download   # Fetch national ZCTA shapefile
    uv run scripts/import_zcta_boundaries.py --import      # Load into zcta_boundaries (PostGIS)
    uv run scripts/import_zcta_boundaries.py --backfill    # Intersect jurisdictions.geom -> zipcodes
    uv run scripts/import_zcta_boundaries.py --status      # Report zip coverage
    uv run scripts/import_zcta_boundaries.py --all         # download + import + backfill + status

Requirements:
    - PostGIS extension enabled
    - ogr2ogr (gdal-bin package)
"""

import argparse
import asyncio
import subprocess
from pathlib import Path

import asyncpg

from config import config, get_logger

logger = get_logger(__name__).bind(component="zcta_import")

# National ZCTA file (single nationwide shapefile, ~528MB). 2020-vintage ZCTA codes.
TIGER_ZCTA_URL = "https://www2.census.gov/geo/tiger/TIGER2024/ZCTA520/tl_2024_us_zcta520.zip"
ZCTA_FILENAME = "tl_2024_us_zcta520.zip"

# Fraction of the smaller polygon's area that must overlap to assign a ZCTA to a
# jurisdiction. 0.10 keeps substantively-shared ZIPs and drops border slivers.
# Confidence: 7/10 -- tuned against San Francisco (32 ZCTAs) and small suburbs.
ASSIGN_FRACTION = 0.10

DATA_DIR = Path("/opt/engagic/data/census")


def download_shapefile() -> bool:
    """Download the national ZCTA shapefile if not already present."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATA_DIR / ZCTA_FILENAME

    if dest.exists() and dest.stat().st_size > 0:
        logger.info("zcta shapefile exists, skipping download", path=str(dest), bytes=dest.stat().st_size)
        return True

    logger.info("downloading zcta shapefile", url=TIGER_ZCTA_URL)
    result = subprocess.run(
        ["curl", "-sS", "-C", "-", "-o", str(dest), TIGER_ZCTA_URL],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        logger.error("download failed", stderr=result.stderr)
        dest.unlink(missing_ok=True)
        return False

    logger.info("download complete", bytes=dest.stat().st_size)
    return True


async def import_to_postgis() -> None:
    """Load ZCTA polygons into zcta_boundaries, reprojected to EPSG:4326."""
    dsn = config.get_postgres_dsn()
    shapefile = DATA_DIR / ZCTA_FILENAME
    if not shapefile.exists():
        logger.error("zcta shapefile not found, run --download first", path=str(shapefile))
        return

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS zcta_boundaries CASCADE")
        logger.info("dropped existing zcta_boundaries table")
    finally:
        await conn.close()

    # Source SRS is EPSG:4269 (NAD83); reproject to 4326 to match jurisdictions.geom.
    # OGRSQL renames ZCTA5CE20 -> zcta5 so downstream queries read cleanly.
    vsi_path = f"/vsizip/{shapefile}"
    result = subprocess.run(
        [
            "ogr2ogr",
            "-f", "PostgreSQL",
            f"PG:{dsn}",
            vsi_path,
            "-nln", "zcta_boundaries",
            "-nlt", "MULTIPOLYGON",
            "-t_srs", "EPSG:4326",
            "-lco", "GEOMETRY_NAME=geom",
            "-lco", "SPATIAL_INDEX=GIST",
            "-dialect", "OGRSQL",
            "-sql", "SELECT ZCTA5CE20 AS zcta5 FROM tl_2024_us_zcta520",
            "-overwrite",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode != 0:
        logger.error("ogr2ogr failed", stderr=result.stderr)
        return

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_zcta_zcta5 ON zcta_boundaries (zcta5)")
        # Repair any self-intersecting rings up front so the backfill's
        # ST_Intersection calls cannot abort on a single bad polygon.
        repaired = await conn.execute(
            "UPDATE zcta_boundaries SET geom = ST_MakeValid(geom) WHERE NOT ST_IsValid(geom)"
        )
        count = await conn.fetchval("SELECT COUNT(*) FROM zcta_boundaries")
        logger.info("zcta import complete", total_zctas=count, repaired=repaired)
    finally:
        await conn.close()


async def backfill_zipcodes() -> None:
    """Assign ZCTAs to active jurisdictions by spatial overlap and insert into zipcodes."""
    dsn = config.get_postgres_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.zcta_boundaries')")
        if not exists:
            logger.error("zcta_boundaries table missing, run --import first")
            return

        before = await conn.fetchval("SELECT COUNT(*) FROM zipcodes")

        # Candidate pairs are filtered by the GiST-indexed ST_Intersects join first;
        # ST_Intersection/ST_Area only run on the survivors. ST_MakeValid guards the
        # jurisdiction side (ZCTA side was repaired at import).
        inserted = await conn.execute(
            f"""
            WITH active AS (
                SELECT banana,
                       ST_MakeValid(geom) AS geom,
                       ST_Area(geom) AS place_area
                FROM jurisdictions
                WHERE status = 'active' AND geom IS NOT NULL
            ),
            pairs AS (
                SELECT a.banana,
                       z.zcta5,
                       ST_Area(ST_Intersection(a.geom, z.geom)) AS inter_area,
                       a.place_area,
                       ST_Area(z.geom) AS zcta_area
                FROM active a
                JOIN zcta_boundaries z ON ST_Intersects(a.geom, z.geom)
            )
            INSERT INTO zipcodes (banana, zipcode, is_primary)
            SELECT banana, zcta5, false
            FROM pairs
            WHERE inter_area >= {ASSIGN_FRACTION} * LEAST(place_area, zcta_area)
            ON CONFLICT (banana, zipcode) DO NOTHING
            """
        )

        after = await conn.fetchval("SELECT COUNT(*) FROM zipcodes")
        logger.info(
            "zipcode backfill complete",
            rows_added=after - before,
            zipcodes_before=before,
            zipcodes_after=after,
            insert_status=inserted,
        )
    finally:
        await conn.close()


async def report_status() -> None:
    """Report zipcode coverage across active jurisdictions."""
    dsn = config.get_postgres_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        total = await conn.fetchval("SELECT COUNT(*) FROM jurisdictions WHERE status='active'")
        with_zips = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT j.banana)
            FROM jurisdictions j
            JOIN zipcodes z ON z.banana = j.banana
            WHERE j.status = 'active'
            """
        )
        total_rows = await conn.fetchval("SELECT COUNT(*) FROM zipcodes")

        print("\nZipcode Coverage:")
        print(f"  Active jurisdictions: {total}")
        print(f"  With >=1 zipcode: {with_zips} ({100 * with_zips / total:.1f}%)")
        print(f"  Total zipcode rows: {total_rows}")

        rows = await conn.fetch(
            """
            SELECT zipcount, COUNT(*) AS jurisdictions
            FROM (
                SELECT banana, COUNT(*) AS zipcount FROM zipcodes GROUP BY banana
            ) t
            WHERE zipcount <= 6
            GROUP BY zipcount ORDER BY zipcount
            """
        )
        print("\n  Jurisdictions by zip count (<=6, watch for a spike at 5 = old truncation):")
        for row in rows:
            print(f"    {row['zipcount']} zips: {row['jurisdictions']} jurisdictions")
    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Import Census ZCTA boundaries and backfill zipcodes")
    parser.add_argument("--download", action="store_true", help="Download national ZCTA shapefile")
    parser.add_argument("--import", dest="import_", action="store_true", help="Load into zcta_boundaries")
    parser.add_argument("--backfill", action="store_true", help="Intersect geometries and fill zipcodes")
    parser.add_argument("--status", action="store_true", help="Report zip coverage")
    parser.add_argument("--all", action="store_true", help="Full pipeline")
    args = parser.parse_args()

    if args.all or args.download:
        if not download_shapefile():
            return

    if args.all or args.import_:
        await import_to_postgis()

    if args.all or args.backfill:
        await backfill_zipcodes()

    if args.status or args.all:
        await report_status()

    if not any([args.download, args.import_, args.backfill, args.status, args.all]):
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
