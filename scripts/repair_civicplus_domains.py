#!/usr/bin/env python3
"""
Find the real domain for CivicPlus cities whose vanity subdomain is gone.

CivicPlus has been retiring {slug}.civicplus.com hostnames. The name stops
resolving entirely (NXDOMAIN, not a 404), so every request fails before it is
sent and the city goes quiet -- agendas as well as minutes. The city itself
keeps serving the same AgendaCenter from its own domain.

For each affected jurisdiction this probes candidate hostnames built from the
city name and state, keeps the first that serves a real AgendaCenter, and
writes it into data/civicplus_sites.json, which the adapter already reads as
a domain override. A candidate counts only if the page comes back 200 and
actually contains AgendaCenter markup; a parked page or a redirect to a
search portal does not.

Usage:
    uv run scripts/repair_civicplus_domains.py            # dry run
    uv run scripts/repair_civicplus_domains.py --apply
"""

import argparse
import asyncio
import json
import os
import re
import socket
from typing import Dict, List, Optional

import aiohttp

from config import config, get_logger
from database.db_postgres import Database

logger = get_logger(__name__).bind(component="repair_civicplus_domains")

CITIES_SQL = """
    SELECT banana, slug, name, state
    FROM jurisdictions
    WHERE vendor = 'civicplus' AND status = 'active'
      AND slug NOT ILIKE '%needs_fix%'
      AND name IS NOT NULL AND state IS NOT NULL
    ORDER BY banana
"""

# Suffixes cities actually use, most official first.
_TLDS = ("gov", "org", "com", "us", "net")
_PREFIXES = ("www.", "")
_AGENDA_MARKER = re.compile(r"AgendaCenter", re.IGNORECASE)
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; engagic/1.0)"}

_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming", "DC": "District of Columbia",
}


def resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except socket.gaierror:
        return False


def name_forms(name: str, state: str) -> List[str]:
    """Hostname stems a city plausibly uses, without punctuation or spaces."""
    bare = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not bare:
        return []
    state_lower = (state or "").lower()
    forms = [
        bare,
        f"cityof{bare}",
        f"townof{bare}",
        f"{bare}{state_lower}",
        f"cityof{bare}{state_lower}",
        f"{bare}city",
    ]
    seen: List[str] = []
    for form in forms:
        if form not in seen:
            seen.append(form)
    return seen


def candidate_hosts(name: str, state: str) -> List[str]:
    hosts: List[str] = []
    for stem in name_forms(name, state):
        for tld in _TLDS:
            for prefix in _PREFIXES:
                host = f"{prefix}{stem}.{tld}"
                if host not in hosts:
                    hosts.append(host)
    return hosts


def belongs_to_city(body: str, name: str, state: str) -> bool:
    """The page must name this city AND this state.

    City names repeat across states, and every CivicPlus portal serves the
    same AgendaCenter markup, so matching on the markup alone would happily
    bind a city to another state's portal. That is exactly the failure that
    produced the wrong-jurisdiction cleanup, so the bar is evidence from the
    page itself rather than a plausible-looking hostname.
    """
    haystack = body[:200_000].lower()
    squashed = re.sub(r"[^a-z0-9]", "", haystack)
    city_token = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not city_token or city_token not in squashed:
        return False
    state_code = (state or "").strip().upper()
    state_name = _STATE_NAMES.get(state_code, "")
    patterns = []
    if state_code:
        patterns.append(rf"\b{re.escape(state_code.lower())}\b")
    if state_name:
        patterns.append(re.escape(state_name.lower()))
    return any(re.search(pattern, haystack) for pattern in patterns)


async def serves_agenda_center(
    session: aiohttp.ClientSession, host: str, name: str, state: str
) -> Optional[str]:
    url = f"https://{host}/AgendaCenter"
    body = None
    # One retry: a timeout and a city that genuinely has no portal here look
    # identical from the caller, and treating a slow host as absent is how a
    # correct domain gets reported unresolved (Brea, first run).
    for attempt in range(2):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=25), allow_redirects=True) as response:
                if response.status != 200:
                    return None
                body = await response.text(errors="replace")
                break
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
            if attempt:
                return None
            await asyncio.sleep(1.5)
    if body is None:
        return None
    if not _AGENDA_MARKER.search(body):
        return None
    if not belongs_to_city(body, name, state):
        logger.debug("agenda center found but city/state unconfirmed", host=host, city=name, state=state)
        return None
    return str(response.url.host or host)


async def main() -> int:
    ap = argparse.ArgumentParser(description="Repair CivicPlus domains whose subdomain no longer resolves")
    ap.add_argument("--apply", action="store_true", help="write data/civicplus_sites.json (default is dry run)")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    db = await Database.create()
    try:
        async with db.pool.acquire() as conn:
            cities = await conn.fetch(CITIES_SQL)
    finally:
        await db.pool.close()

    dead = [c for c in cities if not resolves(f"{c['slug']}.civicplus.com")]
    logger.info("civicplus domains checked", total=len(cities), unresolvable=len(dead))
    if not dead:
        return 0

    config_path = os.path.join(config.DB_DIR, "civicplus_sites.json")
    site_config: Dict[str, Dict] = {}
    if os.path.exists(config_path):
        with open(config_path) as handle:
            site_config = json.load(handle)

    found: Dict[str, str] = {}
    unresolved: List[str] = []
    semaphore = asyncio.Semaphore(args.concurrency)

    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        async def probe(city) -> None:
            async with semaphore:
                if site_config.get(city["slug"], {}).get("domain"):
                    return
                for host in candidate_hosts(city["name"], city["state"]):
                    if not resolves(host):
                        continue
                    working = await serves_agenda_center(
                        session, host, city["name"], city["state"]
                    )
                    if working:
                        found[city["slug"]] = working
                        logger.info("domain found", banana=city["banana"], domain=working)
                        return
                unresolved.append(city["banana"])
                # Not proof of absence: a probe that timed out on every
                # candidate lands here too, so this list is "undetermined".

        await asyncio.gather(*(probe(city) for city in dead))

    logger.info(
        "probe complete",
        unresolvable=len(dead),
        repaired=len(found),
        still_unknown=len(unresolved),
        apply=args.apply,
    )
    for banana in unresolved[:20]:
        logger.info("no working domain found", banana=banana)

    if not args.apply:
        print(json.dumps({"repaired": found, "unresolved": unresolved}, indent=1))
        return 0

    for slug, domain in found.items():
        entry = site_config.setdefault(slug, {})
        entry["domain"] = domain
        entry["domain_source"] = "repair_civicplus_domains"
    with open(config_path, "w") as handle:
        json.dump(site_config, handle, indent=2, sort_keys=True)
    logger.info("site config written", path=config_path, entries=len(found))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
