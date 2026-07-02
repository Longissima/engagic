#!/usr/bin/env python3
"""Detect and purge jurisdictions that scrape the WRONG government's agendas.

Root problem: a city's vendor `slug` points at a different entity's portal (a
county, or a same-prefixed city), so its meetings/matters/roster all belong to
the wrong government. The matter/item hash IDs embed the banana, so a slug fix +
resync produces fresh IDs rather than reconciling -- there is no clean migration.
The fix is therefore: purge the scraped content, fix the slug (manual), resync.

Detection runs three independent signals over type='city' jurisdictions so it
catches both city->county and city->city collisions:

  1. slug_county   -- slug names a county body (slug ILIKE '%county%' etc.)
  2. county_titles -- >=1 stored meeting titled like a county body
                      (Board of Supervisors / County Commission / Freeholders ...)
  3. name_mismatch -- slug encodes a real identity (non-numeric) that is NOT the
                      city's name AND the city's own name never appears in its
                      scraped content. Catches West Bend -> West Allis.

`--audit` is read-only. `--purge` defaults to a dry-run (row counts only);
add `--execute` to delete inside a single transaction. The jurisdiction row,
its zipcodes, and user/tenant subscriptions are always preserved.

Usage:
    uv run scripts/audit_jurisdiction_collisions.py --audit
    uv run scripts/audit_jurisdiction_collisions.py --purge laramieWY,westbendWI
    uv run scripts/audit_jurisdiction_collisions.py --purge laramieWY,westbendWI --execute
"""

import argparse
import asyncio

import asyncpg

from config import config, get_logger

logger = get_logger(__name__).bind(component="collision_audit")

# Body names that only a county/parish-level government convenes. A type='city'
# jurisdiction showing these is almost certainly scraping the wrong portal.
COUNTY_BODY_PATTERN = (
    r"board of (county )?(commissioner|supervisor)|county (board|commission|council)"
    r"|freeholder|quorum court|police jury"
)

# Ordered delete plan for a misconfigured banana. Order is load-bearing: several
# child tables FK city_matters with ON DELETE SET NULL but a NOT NULL matter_id
# column, so the parent cannot be deleted while they reference it. matter_appearances
# and votes clear via the meetings cascade; matter_topics/sponsorships/deliberations
# have no cascade path and must be deleted explicitly BEFORE city_matters.
# $1 = banana array. Tables keyed only on banana use it directly; matter/meeting
# children are scoped by subquery so we never touch another jurisdiction's rows.
PURGE_PLAN = [
    ("queue", "DELETE FROM queue WHERE banana = ANY($1)"),
    ("tracked_items", "DELETE FROM tracked_items WHERE banana = ANY($1)"),
    ("matter_appearances",
     "DELETE FROM matter_appearances WHERE matter_id IN (SELECT id FROM city_matters WHERE banana = ANY($1))"
     " OR meeting_id IN (SELECT id FROM meetings WHERE banana = ANY($1))"),
    ("votes",
     "DELETE FROM votes WHERE meeting_id IN (SELECT id FROM meetings WHERE banana = ANY($1))"
     " OR matter_id IN (SELECT id FROM city_matters WHERE banana = ANY($1))"),
    ("matter_topics", "DELETE FROM matter_topics WHERE matter_id IN (SELECT id FROM city_matters WHERE banana = ANY($1))"),
    ("sponsorships", "DELETE FROM sponsorships WHERE matter_id IN (SELECT id FROM city_matters WHERE banana = ANY($1))"),
    ("deliberations", "DELETE FROM deliberations WHERE matter_id IN (SELECT id FROM city_matters WHERE banana = ANY($1))"),
    # meetings cascades items, item_topics, item_revisions, happening_items,
    # meeting_topics, tracked_item_meetings, meeting_revisions.
    ("meetings", "DELETE FROM meetings WHERE banana = ANY($1)"),
    ("city_matters", "DELETE FROM city_matters WHERE banana = ANY($1)"),
    ("committees", "DELETE FROM committees WHERE banana = ANY($1)"),  # cascades committee_members
    ("council_members", "DELETE FROM council_members WHERE banana = ANY($1)"),
    ("happening_items", "DELETE FROM happening_items WHERE banana = ANY($1)"),  # mop up direct rows
]

# Keyed on banana but intentionally PRESERVED across a purge.
PRESERVE_TABLES = ["jurisdictions", "zipcodes", "tenant_coverage", "user_topic_subscriptions"]


DETECT_SQL = f"""
WITH src AS (
    SELECT DISTINCT ON (banana) banana,
           split_part(split_part(COALESCE(agenda_url, packet_url), '://', 2), '/', 1) AS source_host
    FROM meetings
    WHERE COALESCE(agenda_url, packet_url) IS NOT NULL
    ORDER BY banana, date DESC NULLS LAST
),
meeting_stats AS (
    SELECT banana,
           count(*) AS meetings,
           count(*) FILTER (WHERE title ~* '{COUNTY_BODY_PATTERN}') AS county_titles,
           bool_or(title ILIKE '%' || (SELECT name FROM jurisdictions j2 WHERE j2.banana = meetings.banana) || '%') AS name_in_meetings
    FROM meetings GROUP BY banana
),
matter_name AS (
    SELECT cm.banana,
           bool_or(cm.title ILIKE '%' || j.name || '%') AS name_in_matters
    FROM city_matters cm JOIN jurisdictions j ON j.banana = cm.banana
    GROUP BY cm.banana
),
pend AS (
    SELECT banana,
           count(*) FILTER (WHERE job_type='meeting') AS pend_meetings,
           count(*) FILTER (WHERE job_type='matter') AS pend_matters
    FROM queue WHERE status='pending' GROUP BY banana
)
SELECT j.banana, j.name, j.state, j.vendor, j.slug,
       s.source_host,
       COALESCE(ms.meetings,0) AS meetings,
       COALESCE(ms.county_titles,0) AS county_titles,
       COALESCE(p.pend_meetings,0) AS pend_meetings,
       COALESCE(p.pend_matters,0) AS pend_matters,
       -- signal 1
       (j.slug ~* 'county|supervisor|freeholder|parish') AS slug_county,
       -- signal 3 components
       (j.slug !~ '^[0-9]+$') AS slug_has_identity,
       regexp_replace(lower(j.name), '[^a-z0-9]', '', 'g') AS norm_name,
       regexp_replace(lower(j.slug), '[^a-z0-9]', '', 'g') AS norm_slug,
       COALESCE(ms.name_in_meetings, false) OR COALESCE(mn.name_in_matters, false) AS name_in_content
FROM jurisdictions j
LEFT JOIN src s ON s.banana = j.banana
LEFT JOIN meeting_stats ms ON ms.banana = j.banana
LEFT JOIN matter_name mn ON mn.banana = j.banana
LEFT JOIN pend p ON p.banana = j.banana
WHERE j.type = 'city'
"""


def classify(row) -> tuple[bool, list[str]]:
    """Return (is_suspect, reasons) from the three detection signals."""
    reasons = []
    if row["slug_county"]:
        reasons.append("slug=county")
    if row["county_titles"] > 0:
        reasons.append(f"county-body-titles={row['county_titles']}")
    # name_mismatch: slug encodes a real (non-numeric) identity that neither
    # contains nor is contained by the city name, and the city never self-names.
    nn, ns = row["norm_name"], row["norm_slug"]
    token_disjoint = bool(nn) and bool(ns) and nn not in ns and ns not in nn
    if row["slug_has_identity"] and token_disjoint and not row["name_in_content"] and row["meetings"] > 0:
        reasons.append("name-absent-from-content+slug")
    return (len(reasons) > 0, reasons)


async def audit() -> None:
    conn = await asyncpg.connect(config.get_postgres_dsn())
    try:
        rows = await conn.fetch(DETECT_SQL)
    finally:
        await conn.close()

    suspects = []
    for r in rows:
        is_suspect, reasons = classify(r)
        if is_suspect:
            suspects.append((r, reasons))

    # Active (has pending work -> will sync wrong data on next run) first, then by load.
    suspects.sort(key=lambda x: (x[0]["pend_meetings"] + x[0]["pend_matters"]), reverse=True)

    print(f"\n{len(suspects)} suspected wrong-government jurisdictions (of {len(rows)} cities)\n")
    header = f"{'banana':<22} {'name, state':<26} {'slug':<22} {'pend(m/M)':<10} signals"
    print(header)
    print("-" * len(header))
    active = 0
    for r, reasons in suspects:
        load = r["pend_meetings"] + r["pend_matters"]
        if load > 0:
            active += 1
        pend = f"{r['pend_meetings']}/{r['pend_matters']}"
        print(f"{r['banana']:<22} {(r['name']+', '+r['state']):<26} {r['slug']:<22} {pend:<10} {', '.join(reasons)}")
        if r["source_host"]:
            print(f"{'':<22} -> scraping: {r['source_host']}")

    print(f"\n{active} have pending work (will publish wrong data on a process run); "
          f"{len(suspects) - active} dormant.")
    print("Verify each, then: --purge <banana,...> [--execute]")


SLUG_SENTINEL = "NEEDS_FIX"  # matches existing convention in jurisdictions.slug


async def purge(bananas: list[str], execute: bool, reset_slug: bool = True) -> None:
    conn = await asyncpg.connect(config.get_postgres_dsn())
    try:
        existing = await conn.fetch(
            "SELECT banana, name, state, slug FROM jurisdictions WHERE banana = ANY($1) ORDER BY banana", bananas
        )
        found = {r["banana"] for r in existing}
        missing = set(bananas) - found
        if missing:
            logger.warning("bananas not found, skipping", missing=sorted(missing))
        if not found:
            print("Nothing to purge.")
            return

        print(f"\nPurge target ({len(found)}):")
        print(f"{'(DRY RUN)' if not execute else '(EXECUTING)'}\n")
        # Print the wrong slugs now -- this is the list to correct after the purge.
        print("  banana / name / wrong slug (record these to fix):")
        for r in existing:
            print(f"  {r['banana']:<24} {(r['name']+', '+r['state']):<26} {r['slug']}")
        print("")

        async def counts() -> dict[str, int]:
            # Headline banana-keyed tables; the rest die via cascade/subquery in PURGE_PLAN.
            out = {}
            for t in ("queue", "tracked_items", "city_matters", "meetings", "council_members",
                      "committees", "happening_items"):
                out[t] = await conn.fetchval(
                    f"SELECT count(*) FROM {t} WHERE banana = ANY($1)", list(found)
                )
            out["items (via meetings)"] = await conn.fetchval(
                "SELECT count(*) FROM items WHERE meeting_id IN (SELECT id FROM meetings WHERE banana = ANY($1))",
                list(found),
            )
            return out

        before = await counts()
        for t, c in before.items():
            print(f"  {t:<26} {c:>8}")
        preserved = {}
        for t in PRESERVE_TABLES:
            preserved[t] = await conn.fetchval(
                f"SELECT count(*) FROM {t} WHERE banana = ANY($1)", list(found)
            )
        print("\n  preserved (untouched):")
        for t, c in preserved.items():
            print(f"  {t:<26} {c:>8}")

        if reset_slug:
            print(f"\n  slug -> '{SLUG_SENTINEL}' for all {len(found)} (so a resync can't re-ingest the wrong portal)")

        if not execute:
            print("\nDry run only. Re-run with --execute to delete.")
            return

        async with conn.transaction():
            for label, sql in PURGE_PLAN:
                status = await conn.execute(sql, list(found))
                logger.info("purged", table=label, result=status)
            if reset_slug:
                status = await conn.execute(
                    "UPDATE jurisdictions SET slug = $2, updated_at = now() WHERE banana = ANY($1)",
                    list(found), SLUG_SENTINEL,
                )
                logger.info("slug reset", result=status, sentinel=SLUG_SENTINEL)
        print("\nPurge complete. Jurisdiction rows, zipcodes, and subscriptions preserved.")
        print(f"Slugs set to '{SLUG_SENTINEL}'. Next: set the correct slug for each, then resync.")
    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Audit/purge wrong-government jurisdictions")
    parser.add_argument("--audit", action="store_true", help="Read-only detection report")
    parser.add_argument("--purge", type=str, help="Comma-separated bananas to purge content for")
    parser.add_argument("--execute", action="store_true", help="Actually delete (default: dry run)")
    args = parser.parse_args()

    if args.audit:
        await audit()
    elif args.purge:
        bananas = [b.strip() for b in args.purge.split(",") if b.strip()]
        await purge(bananas, args.execute)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
