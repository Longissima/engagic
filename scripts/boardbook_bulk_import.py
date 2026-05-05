#!/usr/bin/env python3
"""Bulk-import BoardBook school districts into jurisdictions.

Scrapes meetings.boardbook.org/Public/, classifies each org by name shape,
infers the home state from the modal "City, ST ZIP" token in the org's
listing page, and (with --apply) upserts the high-confidence school
districts as type='school_district', vendor='boardbook'.

Auto-add criteria:
  - Name matches school-district pattern (ISD, School District, Public Schools, ...)
  - Name does NOT match an exclusion (library, college, ESD, RESA, charter, ...)
  - State inference is unambiguous: only one state present, OR modal count >= 3
    AND modal >= 3x runner-up
  - No existing jurisdiction with the same (name, state) -- the UNIQUE
    constraint would otherwise reject the insert

Anything failing those goes to /tmp/boardbook_review.tsv for triage.

Default mode is dry-run. Pass --apply to actually write.
"""

import argparse
import asyncio
import html as html_lib
import os
import re
import sys
from collections import Counter
from typing import Optional, Tuple

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database.db_postgres import Database
from database.models import Jurisdiction
from scripts._jurisdiction_naming import (
    derive_district_stem,
    disambiguate_bananas,
    _strip_acronym_dots,
)


BASE = "https://meetings.boardbook.org"
DIRECTORY_URL = f"{BASE}/Public/"
USER_AGENT = "Mozilla/5.0 (engagic-boardbook-importer)"

# Concurrent fetches against BoardBook. They're a SaaS with capacity, but be polite.
CONCURRENCY = 12

# Substring keywords (lowercase, on cleaned name) that mark an org as a
# school district. Order doesn't matter; first match wins.
_SD_KEYWORDS = (
    " isd",
    " cisd",
    "independent school district",
    "consolidated isd",
    "school district",
    "public schools",
    "community schools",
    "area schools",
    "municipal schools",
    "township schools",
    "unit school",
    "city schools",
    "unified",
    " schools",
)

# Word-boundary excludes: things that look like school districts by some
# measure but aren't local education agencies.
#   library/college/university -- different jurisdiction type
#   department of education    -- state-level body
#   electric (co)op, service co-op -- utility / non-LEA
#   ESD/RESA, intermediate school district -- regional service agency
#   charter -- charter schools are governed differently; revisit later
_EXCLUDE_RE = re.compile(
    r"\b(library|college|university|department of education|"
    r"electric cooperative|electric coop|service cooperative|"
    r"esd|resa|intermediate school district|charter)\b",
    re.IGNORECASE,
)

# "City, ST ZIP" or "City, Full State Name ZIP". The strict 5-digit anchor
# blocks matches like "a.m. on Monday, Ma". State token is normalized via
# _normalize_state -- non-state tokens (Esquire, Street, etc.) are dropped.
_LOCATION_RE = re.compile(
    r"\b([A-Z][\w .\-/&']{1,40}),\s*([A-Za-z]{2,20}(?:\s+[A-Za-z]{2,20})?)\s+(\d{5})\b"
)

_US_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_US_STATE_ABBREVS = set(_US_STATE_NAMES.values())


def _normalize_state(token: str) -> Optional[str]:
    """Resolve a state token to its 2-letter code, or None if unrecognized.
    Accepts 'TX', 'tx', 'Texas', 'TEXAS', 'New York', 'new york', etc.
    """
    s = token.strip().lower()
    if len(s) == 2:
        u = s.upper()
        return u if u in _US_STATE_ABBREVS else None
    return _US_STATE_NAMES.get(s)


def classify_name(name: str) -> str:
    """school_district | other"""
    # Normalize dotted acronyms ('C.I.S.D.' -> 'CISD') so keyword matching catches them.
    n = _strip_acronym_dots(html_lib.unescape(name)).lower()
    if _EXCLUDE_RE.search(n):
        return "other"
    for kw in _SD_KEYWORDS:
        if kw in n:
            return "school_district"
    return "other"


def infer_state(html: str) -> Tuple[Optional[str], int, int]:
    """Return (modal_state, modal_count, runner_up_count) from address tokens."""
    counts: Counter = Counter()
    for m in _LOCATION_RE.finditer(html):
        st = _normalize_state(m.group(2))
        if st:
            counts[st] += 1
    if not counts:
        return None, 0, 0
    top = counts.most_common(2)
    modal_state, modal_count = top[0]
    runner_up = top[1][1] if len(top) > 1 else 0
    return modal_state, modal_count, runner_up


def is_confident(modal_count: int, runner_up: int) -> bool:
    """Confidence rule: only one state, OR modal dominates 3:1 with >= 3 hits."""
    if modal_count == 0:
        return False
    if runner_up == 0:
        return modal_count >= 1
    return modal_count >= 3 and modal_count >= 3 * runner_up


async def fetch_directory(session: aiohttp.ClientSession) -> list[Tuple[str, str]]:
    async with session.get(DIRECTORY_URL) as r:
        text = await r.text()
    pairs = re.findall(
        r'<a href="/Public/Organization/([^"]+)">([^<]+)</a>', text
    )
    return [(slug, html_lib.unescape(name).strip()) for slug, name in pairs]


async def fetch_org(
    session: aiohttp.ClientSession, slug: str, sem: asyncio.Semaphore
) -> Optional[str]:
    url = f"{BASE}/Public/Organization/{slug}"
    async with sem:
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                if r.status != 200:
                    return None
                return await r.text()
        except Exception:
            return None


async def main(apply: bool, limit: Optional[int]):
    db = await Database.create(min_size=1, max_size=5)
    try:
        connector = aiohttp.TCPConnector(limit=CONCURRENCY)
        sem = asyncio.Semaphore(CONCURRENCY)
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT}, connector=connector
        ) as session:
            print("Fetching BoardBook directory...")
            directory = await fetch_directory(session)
            print(f"  {len(directory)} orgs in directory")

            sds = [
                (slug, name) for slug, name in directory
                if classify_name(name) == "school_district"
            ]
            print(f"  {len(sds)} school-district-shaped names")

            if limit:
                sds = sds[:limit]
                print(f"  --limit {limit} -> processing {len(sds)}")

            print(
                f"Fetching org pages (concurrency={CONCURRENCY}) for state inference..."
            )
            htmls = await asyncio.gather(
                *(fetch_org(session, slug, sem) for slug, _ in sds)
            )

        # decisions: list of dicts (mutable so disambiguate_bananas can rewrite them)
        decisions: list[dict] = []
        for (slug, name), html in zip(sds, htmls):
            base = {
                "slug": slug, "name": name, "state": None,
                "modal": 0, "runner_up": 0, "banana": None,
            }
            if html is None:
                decisions.append({**base, "outcome": "fetch_failed"})
                continue
            state, modal, runner = infer_state(html)
            base.update(state=state, modal=modal, runner_up=runner)
            if not state:
                decisions.append({**base, "outcome": "no_location"})
                continue
            if not is_confident(modal, runner):
                decisions.append({**base, "outcome": "ambiguous"})
                continue
            stem = derive_district_stem(name)
            if not stem:
                decisions.append({**base, "outcome": "empty_stem"})
                continue
            base["banana"] = stem + "sd" + state
            decisions.append({**base, "outcome": "ready"})

        # Capture each ready row's initial banana before disambiguation so the
        # apply phase can split unchanged vs. changed bananas (ordering matters
        # for the UNIQUE(name, state) constraint).
        ready = [d for d in decisions if d["outcome"] == "ready"]
        for d in ready:
            d["initial_banana"] = d["banana"]

        # Rewrite bananas for any same-(stem, state) collisions in the ready set
        # (e.g. Skokie SD 68 / SD 69, Rice CISD / Rice ISD).
        orphan_bananas = disambiguate_bananas(ready)

        outcome_counts = Counter(d["outcome"] for d in decisions)
        print("\n=== DECISION SUMMARY ===")
        for outcome, count in outcome_counts.most_common():
            print(f"  {outcome:20s} {count}")
        if orphan_bananas:
            print(f"  orphan_bananas       {len(orphan_bananas)}  (collision bananas to delete)")

        review = [d for d in decisions if d["outcome"] != "ready"]

        def _write_tsv(path, rows):
            with open(path, "w") as f:
                f.write("slug\tname\tstate\tmodal_count\trunner_up\tbanana\toutcome\n")
                for d in rows:
                    f.write("\t".join(
                        "" if d.get(k) is None else str(d.get(k))
                        for k in ("slug", "name", "state", "modal", "runner_up", "banana", "outcome")
                    ) + "\n")

        review_path = "/tmp/boardbook_review.tsv"
        _write_tsv(review_path, review)
        print(f"\nReview TSV: {review_path}  ({len(review)} rows)")

        ready_path = "/tmp/boardbook_ready.tsv"
        _write_tsv(ready_path, ready)
        print(f"Ready TSV:  {ready_path}  ({len(ready)} rows)")

        if ready:
            state_counts = Counter(d["state"] for d in ready)
            print("\nReady inserts by state:")
            for st, n in state_counts.most_common():
                print(f"  {st}  {n}")

            print("\nFirst 10 ready inserts:")
            for d in ready[:10]:
                print(
                    f"  banana={d['banana']:35s} vendor=boardbook slug={d['slug']:8s} "
                    f"state={d['state']} (modal={d['modal']}, runner_up={d['runner_up']}) -- {d['name']}"
                )

        if orphan_bananas:
            print(f"\nOrphan collision bananas to DELETE: {sorted(orphan_bananas)}")

        if not apply:
            print(
                f"\nDry run. {len(ready)} jurisdictions would be upserted; "
                f"{len(orphan_bananas)} orphan bananas would be deleted. "
                "Re-run with --apply to execute."
            )
            return

        # Apply phase. Ordering matters because of the UNIQUE(name, state) constraint:
        #   1. DELETE orphan bananas (collision-banana rows whose entity no longer claims them).
        #   2. UPSERT rows whose banana didn't change (these may overwrite a colliding
        #      row's name in place, freeing that name for step 3).
        #   3. UPSERT rows whose banana changed (now safe to INSERT at new banana).
        if orphan_bananas:
            print(f"\nDeleting {len(orphan_bananas)} orphan collision banana(s)...")
            async with db.pool.acquire() as conn:
                for banana in sorted(orphan_bananas):
                    result = await conn.execute(
                        "DELETE FROM jurisdictions WHERE banana = $1",
                        banana,
                    )
                    print(f"  {banana}: {result}")

        unchanged = [d for d in ready if d["banana"] == d["initial_banana"]]
        changed = [d for d in ready if d["banana"] != d["initial_banana"]]
        print(f"\nUpserting: {len(unchanged)} unchanged-banana, {len(changed)} new-banana")

        async def _upsert(d):
            j = Jurisdiction(
                banana=d["banana"],
                name=d["name"],
                state=d["state"],
                vendor="boardbook",
                slug=d["slug"],
                type="school_district",
                status="active",
            )
            await db.jurisdictions.upsert_city(j)

        inserted = 0
        failed = []
        for batch_label, batch in (("unchanged", unchanged), ("new-banana", changed)):
            for d in batch:
                try:
                    await _upsert(d)
                    inserted += 1
                except Exception as e:
                    failed.append((batch_label, d["banana"], d["name"], d["state"], str(e)))
        print(f"Upserted: {inserted}/{len(ready)}")
        if failed:
            print(f"\nFailures ({len(failed)}):")
            for batch_label, banana, name, state, err in failed[:20]:
                print(f"  [{batch_label}] {banana} ({name}, {state}): {err}")
            if len(failed) > 20:
                print(f"  ... and {len(failed) - 20} more")

    finally:
        await db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually upsert to the database (default: dry-run)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="process only the first N school-district-shaped orgs (for testing)",
    )
    args = p.parse_args()
    asyncio.run(main(apply=args.apply, limit=args.limit))
