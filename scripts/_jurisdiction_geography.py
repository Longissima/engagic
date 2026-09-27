"""Optional city enrichment for jurisdiction onboarding."""

from typing import Optional

from database.db_postgres import Database


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
        from uszipcode import SearchEngine

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


