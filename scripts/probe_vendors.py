"""
Vendor probe script -- detect correct vendor and slug for unconfigured cities.

For each city, generates plausible slug candidates per vendor, then probes
the vendor's known URL pattern to see which responds. Outputs confirmed
vendor+slug pairs for cities that get a hit.
"""

import asyncio
import sys
import re
from typing import Optional

import aiohttp
import asyncpg

DB_URL = "postgresql://engagic:engagic_secure_2025@localhost:5432/engagic"

# Probe URLs per vendor -- must return 200 (or redirect) to count as a hit.
# Each is a function: slug -> URL to probe.
# Probe config per vendor: (url_template, min_content_length)
# min_content_length filters out shell pages that return 200 for any subdomain.
# Vendors that 404 or DNS-fail on bad slugs can use 0.
VENDOR_PROBES = {
    "legistar":    (lambda slug: f"https://{slug}.legistar.com/Calendar.aspx", 1000),
    "granicus":    (lambda slug: f"https://{slug}.granicus.com/ViewPublisher.php?view_id=1", 0),
    "primegov":    (lambda slug: f"https://{slug}.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings", 0),
    "novusagenda": (lambda slug: f"https://{slug}.novusagenda.com/agendapublic", 5000),
    "civicclerk":  (lambda slug: f"https://{slug}.api.civicclerk.com/v1/Events", 0),
    "civicplus":   (lambda slug: f"https://{slug}.civicplus.com/AgendaCenter", 0),
    "iqm2":        (lambda slug: f"https://{slug}.iqm2.com", 0),
    "onbase":      (lambda slug: f"https://{slug}.onbaseonline.com", 0),
    "municode":    (lambda slug: f"https://{slug}.municodemeetings.com", 0),
    "escribe":     (lambda slug: f"https://{slug}.escribemeetings.com", 0),
}

# Timeout per probe -- fast fail on DNS errors
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8)


def generate_slug_candidates(name: str, state: str) -> list[str]:
    """Generate plausible slug variations from city name and state.

    Ordered by likelihood based on observed patterns across vendors.
    """
    # Normalize: lowercase, strip periods, collapse spaces
    clean = re.sub(r"[.'']", "", name.lower())
    words = clean.split()
    joined = "".join(words)
    st = state.lower()

    candidates = []

    # cityname: "sanantonio"
    candidates.append(joined)

    # citynamest: "sanantoniotx"
    candidates.append(joined + st)

    # cityofcityname: "cityofsanantonio"
    candidates.append("cityof" + joined)

    # cityname-st: "sanantonio-tx" (granicus dash pattern)
    candidates.append(joined + "-" + st)

    # citynamecity: "sanantoniotxcity" -- rare but exists
    candidates.append(joined + "city")

    # citynamecityst: "sanantoniotxcity" (iqm2 pattern: "atlantacityga")
    candidates.append(joined + "city" + st)

    # For multi-word: "san-antonio" (dashed)
    if len(words) > 1:
        dashed = "-".join(words)
        candidates.append(dashed)
        candidates.append(dashed + "-" + st)

    # townof / villageof variants
    candidates.append("townof" + joined)
    candidates.append("villageof" + joined)

    # municode uses uppercase short codes (CPTX) and hyphenated (columbus-ga)
    candidates.append(joined + "-" + st)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    return unique


def generate_civicplus_candidates(name: str, state: str) -> list[str]:
    """CivicPlus uses st-cityname pattern: fl-fortmyers, ca-colton."""
    clean = re.sub(r"[.'']", "", name.lower())
    joined = "".join(clean.split())
    st = state.lower()

    candidates = [
        f"{st}-{joined}",
        f"{st}-{joined}city",
        f"cityof{joined}{st}",
        f"cityof{joined}",
        joined,
        f"{joined}{st}",
    ]

    seen = set()
    return [c for c in candidates if c not in seen and not seen.add(c)]


def generate_escribe_candidates(name: str, state: str) -> list[str]:
    """eScribe uses pub-cityname pattern."""
    clean = re.sub(r"[.'']", "", name.lower())
    joined = "".join(clean.split())
    st = state.lower()

    return [
        f"pub-{joined}",
        f"pub-{joined}{st}",
        joined,
    ]


# US state abbreviation -> full name for content matching
STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "DC": "district of columbia", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
    "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska",
    "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio",
    "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island",
    "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}


def verify_city_identity(html: str, name: str, state: str) -> bool:
    """Prove uniqueness: does the page content belong to this city+state?

    Checks for city name AND state (full name or abbreviation) in the page.
    This filters out same-name cities in different states (Arlington TX vs MA)
    and stale portals from vendor migrations.
    """
    lower = html.lower()
    city_lower = name.lower()

    # City name must appear
    if city_lower not in lower:
        return False

    # State must appear (try abbreviation, full name, and ", ST" pattern)
    st = state.upper()
    state_full = STATE_NAMES.get(st, "")

    # ", CA" or ", CA " or "California" etc.
    state_patterns = [
        f", {st}",
        f", {st} ",
        f",{st}",
        state_full,
        st.lower(),
    ]

    for pattern in state_patterns:
        if pattern and pattern.lower() in lower:
            return True

    return False


def check_freshness(html: str) -> bool:
    """Check if the page has meeting dates from 2025 or 2026.

    Filters out stale portals from cities that migrated to a new vendor.
    """
    return bool(re.search(r'202[56]', html))


async def probe_url(
    session: aiohttp.ClientSession,
    url: str,
    min_length: int = 0,
    city_name: str = "",
    state: str = "",
) -> bool:
    """Check existence and uniqueness of a vendor page for a specific city.

    Step 1 (existence): URL responds with real content above min_length.
    Step 2 (uniqueness): Page content mentions the correct city + state.
    Step 3 (freshness): Page has dates from 2025-2026 (not stale).
    """
    try:
        async with session.get(url, timeout=PROBE_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                return False
            text = await resp.text()
            if len(text) < max(min_length, 100):
                return False
            lower = text.lower()
            if "page not found" in lower or "does not exist" in lower:
                return False
            if "invalid parameters" in lower:
                return False

            # Uniqueness: verify city + state
            if city_name and state:
                if not verify_city_identity(text, city_name, state):
                    return False

            # Freshness: recent meeting dates
            if not check_freshness(text):
                return False

            return True
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return False


async def probe_city(
    session: aiohttp.ClientSession,
    name: str,
    state: str,
    banana: str,
    current_vendor: str,
) -> Optional[dict]:
    """Probe all vendors for a single city. Returns first confirmed hit."""

    # Vendor-specific slug generators
    slug_generators = {
        "civicplus": generate_civicplus_candidates,
        "escribe": generate_escribe_candidates,
    }

    for vendor, (url_fn, min_len) in VENDOR_PROBES.items():
        gen = slug_generators.get(vendor, generate_slug_candidates)
        candidates = gen(name, state)

        for slug in candidates:
            url = url_fn(slug)
            hit = await probe_url(session, url, min_len, name, state)
            if hit:
                return {
                    "banana": banana,
                    "name": name,
                    "state": state,
                    "current_vendor": current_vendor,
                    "detected_vendor": vendor,
                    "detected_slug": slug,
                    "probe_url": url,
                }

    return None


async def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    conn = await asyncpg.connect(DB_URL)

    # Get never-synced cities ordered by population
    rows = await conn.fetch("""
        SELECT c.banana, c.name, c.state, c.vendor, c.population
        FROM jurisdictions c
        WHERE c.status = 'active'
          AND NOT EXISTS (SELECT 1 FROM meetings m WHERE m.banana = c.banana)
          AND c.population IS NOT NULL
        ORDER BY c.population DESC
        LIMIT $1
    """, limit)

    await conn.close()

    print(f"Probing {len(rows)} cities...\n")

    # Use connection pooling with concurrency limit
    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(10)

        async def probe_with_sem(row):
            async with sem:
                result = await probe_city(
                    session, row["name"], row["state"],
                    row["banana"], row["vendor"]
                )
                if result:
                    pop = row["population"] or 0
                    print(
                        f"HIT  {result['banana']:<25} "
                        f"pop={pop:>8,}  "
                        f"{result['current_vendor']:>12} -> {result['detected_vendor']:<12} "
                        f"slug={result['detected_slug']}"
                    )
                else:
                    pop = row["population"] or 0
                    print(
                        f"MISS {row['banana']:<25} "
                        f"pop={pop:>8,}  "
                        f"vendor={row['vendor']}"
                    )
                return result

        results = await asyncio.gather(*[probe_with_sem(row) for row in rows])

    hits = [r for r in results if r]
    misses = len(results) - len(hits)
    print("\n--- Summary ---")
    print(f"Hits: {len(hits)}/{len(results)}")
    print(f"Misses: {misses}")

    if hits:
        print("\n--- SQL updates ---")
        for h in hits:
            vendor = h['detected_vendor'].replace("'", "''")
            slug = h['detected_slug'].replace("'", "''")
            ban = h['banana'].replace("'", "''")
            print(
                f"UPDATE jurisdictions SET vendor = '{vendor}', "
                f"slug = '{slug}' "
                f"WHERE banana = '{ban}';"
            )


if __name__ == "__main__":
    asyncio.run(main())
