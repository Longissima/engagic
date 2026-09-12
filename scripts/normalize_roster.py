#!/usr/bin/env python3
"""Derive display labels, person clusters and body membership from the raw roster.

Pass one captures what the source printed and never edits it: "ALD. BAUMAN" is
what Milwaukee's minutes say, and the votes attached to that row are correctly
attributed. This is pass two. It writes only council_member_profiles and
member_bodies -- no column of council_members, not even updated_at -- so which
half is authoritative is a question about which table you read. Everything it
writes is recomputable from raw, and it is safe to run repeatedly.

Three derivations per jurisdiction, all landing in council_member_profiles:

  display_name  The label to show. clean_name already strips the title, and the
                gazetteer already applies it when resolving, so the dirty label
                was the only thing left carrying the title.

  person_key    Which rows are one person. A bare-surname row ("McNeill") and a
                full-name row ("Sean McNeill") are the same member; the ingestion
                path creates a row per distinct printed spelling, so these
                accumulate. Clustering reuses Gazetteer rather than a second
                matching rule, because a cluster that disagrees with resolution
                would split a member's record instead of joining it.

  is_person     Whether the row is a member at all. "District Attorney" and
                "Yoo REMOTE Zay" are a staff role and a parse artifact. They are
                flagged, never deleted: the row is evidence of what the document
                said, and it may already carry votes whose ids are referenced.

Plus member_bodies: a vote belongs to a body, and meetings.title records which
body sat. Wauwatosa's minutes contain the Milwaukee Metro Fire Rescue Board of
Directors, whose directors are also genuine Wauwatosa officials -- so a
city-wide vote_count silently sums two different offices.

    uv run scripts/normalize_roster.py                  # dry run, prints a diff
    uv run scripts/normalize_roster.py --apply
    uv run scripts/normalize_roster.py --banana milwaukeeWI --apply

Confidence 8/10 on clustering: it inherits the gazetteer's own ambiguity rules,
so two members sharing a surname stay separate. Confidence 6/10 on body naming,
which is a title-string normalization over 23,073 distinct titles and will have
a tail of near-duplicates.
"""

import argparse
import asyncio
import re
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

import hashlib

from config import get_logger
from database.db_postgres import Database
from parsing.rollcall.engine import Gazetteer
from parsing.rollcall.names import clean_name, fold

logger = get_logger(__name__).bind(component="normalize_roster")

# An organization is named by an explicit organizational noun. Testing for
# department *words* anywhere -- which is what attendance._NOT_PERSON_RE does,
# correctly, on a roster line fragment -- misfires on a surname: it read
# "Ryana Parks-Shaw" (700 votes) and "Kevin Park" as departments.
_ORG_NOUN_RE = re.compile(
    r"\b(?:department|dept|division|bureau|office|agency|authority|board|commission|"
    r"committee|council|subcommittee|taskforce|task\s+force|panel|district|zone|"
    r"corporation|trustees|caucus|delegation|administration|services|works)\b",
    re.IGNORECASE,
)
# A jurisdiction is not a deliberative body and cannot hold a seat or cast a vote.
_JURISDICTION_RE = re.compile(
    r"^(?:county|city|town|village|borough|township|parish|state)\s+of\b|"
    r"^(?:the\s+)?(?:county|city|town|village)$",
    re.IGNORECASE,
)
# A role with nobody named in it: the minutes said the office, not the person.
_BARE_ROLE_RE = re.compile(
    r"^(?:(?:vice|deputy|acting|interim|assistant)\s+)?"
    r"(?:mayor|chair(?:person|man|woman)?|president|councilmember|councilman|councilwoman|"
    r"councilor|commissioner|supervisor|trustee|alderman|alderwoman|alderperson|alder|"
    r"city\s+manager|city\s+attorney|city\s+clerk|clerk\s+of\s+council|clerk|attorney|"
    r"administrator|manager|director|treasurer|auditor|sheriff|staff|applicant|petitioner)"
    r"(?:\s+pro\s*[- ]?tem(?:pore)?)?$",
    re.IGNORECASE,
)
# Attendance-marker residue that reached the roster as a name: "Yoo REMOTE Zay"
# fuses two members and a participation mode, "ARRIVED John Kiefner" keeps a label.
_MARKER_RESIDUE_RE = re.compile(
    r"\b(?:remote|virtual|teleconference|telephonic|zoom|webex|online|present|absent|"
    r"excused|arrived|departed|late|joined|roll\s*call|quorum)\b",
    re.IGNORECASE,
)
# A name cut mid-parenthetical was truncated by the source layout, not chosen.
_UNBALANCED_PAREN_RE = re.compile(r"\([^)]*$|^[^(]*\)")

# A body is the standing committee or board, not the occasion it met on. The
# occasion appears as a trailing noun, as an adjective anywhere, and as a date,
# in any combination: "August 4, 2026 - City Council - Regular Meeting".
_MEETING_NOUN_RE = re.compile(
    r"\s*[-–,:]?\s*\b(?:meeting|session|minutes|agenda|workshop|retreat|conference)\b.*$",
    re.IGNORECASE,
)
_OCCASION_WORD_RE = re.compile(
    r"\b(?:regular|special|annual|organizational|adjourned|emergency|rescheduled|"
    r"continued|reconvened|evening|morning|afternoon|canceled|cancelled)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{1,2}(?:\s*,)?\s*\d{4}\b"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b",
    re.IGNORECASE,
)
_TRAILING_NOISE_RE = re.compile(r"\s*[-–(]\s*(?:final|draft|amended|revised|approved)\b.*$", re.IGNORECASE)
# Some clerks lead with the occasion: "MEETING OF THE FLOOD CONTROL ADVISORY BOARD".
_LEADING_OCCASION_RE = re.compile(
    r"^[\s*]*(?:notice\s+of\s+)?(?:a\s+|the\s+)?"
    r"(?:regular|special|annual|adjourned|concurrent|joint)?\s*"
    r"(?:meeting|session|minutes|agenda)\s+(?:of|for)\s+(?:the\s+)?",
    re.IGNORECASE,
)
# A body noun must survive, or the occasion word was load-bearing and the title
# named no standing body at all.
# What remains when a title said nothing but when and how the body met.
_OCCASION_LEFTOVER_RE = re.compile(
    r"\b(?:regular|special|annual|organizational|adjourned|emergency|concurrent|"
    r"joint|general|closed|executive|study|work|working|budget|public|open|"
    r"legislative|the|of|a|and|for|to|in|at|on)\b|[\d\W_]+",
    re.IGNORECASE,
)
_BODY_NOUN_RE = re.compile(
    r"\b(?:council|commission|committee|board|authority|district|trustees?|"
    r"agency|bureau|panel|cabinet|assembly|legislature|court|senate|caucus|"
    r"zone|corporation|council\s*of\s*governments|cog)\b",
    re.IGNORECASE,
)

ROSTER_SQL = """
    SELECT cm.id, cm.banana, cm.name
    FROM council_members cm
    WHERE ($1::text IS NULL OR cm.banana = $1)
    ORDER BY cm.banana, cm.id
"""
REPORTED_BODIES_SQL = """
    SELECT im.item_id, im.motion_index, im.source, im.reported_body, m.banana
    FROM item_motions im
    JOIN meetings mt ON mt.id = im.meeting_id
    JOIN city_matters m ON m.id = im.matter_id
    WHERE im.reported_body IS NOT NULL
      AND ($1::text IS NULL OR m.banana = $1)
"""
SPONSORSHIPS_SQL = """
    SELECT s.matter_id, s.council_member_id, s.is_primary, s.sponsor_order
    FROM sponsorships s
    JOIN council_members cm ON cm.id = s.council_member_id
    WHERE ($1::text IS NULL OR cm.banana = $1)
"""
BODY_VOTES_SQL = """
    SELECT v.council_member_id, m.title AS body,
           count(*)::int AS vote_count,
           min(v.vote_date)::date AS first_vote,
           max(v.vote_date)::date AS last_vote
    FROM votes v
    JOIN meetings m ON m.id = v.meeting_id
    JOIN council_members cm ON cm.id = v.council_member_id
    WHERE ($1::text IS NULL OR cm.banana = $1)
    GROUP BY v.council_member_id, m.title
"""


def body_of(title: Optional[str]) -> Optional[str]:
    """"City Council Regular Meeting" and "City Council" are one body."""
    if not title:
        return None
    body = _TRAILING_NOISE_RE.sub("", title)
    body = _LEADING_OCCASION_RE.sub("", body)
    body = _MEETING_NOUN_RE.sub("", body)
    body = _DATE_RE.sub(" ", body)
    stripped = _OCCASION_WORD_RE.sub(" ", body)
    # Drop the occasion adjective only while a body noun is left: "Special City
    # Council" is the council, but "Special Meeting" names nobody and a bare
    # "Special" is not a body. "Reinvestment Zone No. 3" keeps its number.
    if _BODY_NOUN_RE.search(stripped):
        body = stripped
    body = re.sub(r"\s+", " ", body).strip(" -–,:*.")
    # A title that was only an occasion or a date names no standing body, and
    # guessing one would invent a membership. A subject-named committee
    # ("Transportation and Infrastructure") carries no body noun and is kept.
    if not body or not _OCCASION_LEFTOVER_RE.sub("", body).strip(" -–,:*."):
        return None
    return body


def body_id(banana: str, name: str) -> str:
    """Stable id from the jurisdiction and the normalized body name."""
    digest = hashlib.sha256(fold(name).encode()).hexdigest()[:12]
    return f"{banana}_body_{digest}"


def classify(raw: str) -> Tuple[str, str]:
    """Return the display label and what kind of actor the row names.

    Four kinds, because "not a person" conflates things that need different
    handling downstream: a body can move and sponsor and therefore deserves
    modelling, a bare role is a person the minutes declined to name, and an
    artifact is a parse failure to be excluded from everything.
    """
    stripped = (raw or "").strip()
    # A bare office is tested before cleaning, because clean_name empties exactly
    # these ("Mayor", "Chair") and an empty label cannot be told apart from junk.
    if _BARE_ROLE_RE.match(stripped):
        return stripped, "role"
    cleaned = clean_name(raw or "")
    if not cleaned:
        # clean_name also rejects a parenthetical aside and a bare office word.
        return stripped, "artifact"
    if _MARKER_RESIDUE_RE.search(cleaned) or _UNBALANCED_PAREN_RE.search(cleaned):
        return cleaned, "artifact"
    if _JURISDICTION_RE.search(cleaned):
        return cleaned, "artifact"
    if _BARE_ROLE_RE.match(cleaned):
        return cleaned, "role"
    if _ORG_NOUN_RE.search(cleaned):
        return cleaned, "body"
    if len(cleaned.split()) > 5:
        return cleaned, "artifact"
    return cleaned, "person"


def cluster(rows: List[dict]) -> Dict[str, str]:
    """Map each row id to a person key, reusing the gazetteer's own resolution."""
    people = [r["name"] for r in rows if classify(r["name"])[1] == "person"]
    gazetteer = Gazetteer(sorted(set(people)))
    keys: Dict[str, str] = {}
    for row in rows:
        label, kind = classify(row["name"])
        if kind != "person":
            continue
        # resolve() returns None for a surname two members share; that row keeps
        # its own key rather than being merged into one of them.
        canonical = gazetteer.resolve(row["name"]) or label
        keys[row["id"]] = fold(canonical)
    return keys


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--banana", help="one jurisdiction only")
    ap.add_argument("--apply", action="store_true", help="write (default dry run)")
    args = ap.parse_args()

    db = await Database.create()
    try:
        async with db.pool.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(ROSTER_SQL, args.banana)]
            body_rows = [dict(r) for r in await conn.fetch(BODY_VOTES_SQL, args.banana)]
            sponsorships = [dict(r) for r in await conn.fetch(SPONSORSHIPS_SQL, args.banana)]
            reported = [dict(r) for r in await conn.fetch(REPORTED_BODIES_SQL, args.banana)]

        by_city: Dict[str, List[dict]] = defaultdict(list)
        member_banana: Dict[str, str] = {}
        actor_of_row: Dict[str, Tuple[str, str]] = {}
        for row in rows:
            by_city[row["banana"]].append(row)
            member_banana[row["id"]] = row["banana"]

        profiles: List[Tuple[str, str, Optional[str], bool, str]] = []
        counts: Counter = Counter()
        per_key: Counter = Counter()
        for banana, city_rows in by_city.items():
            keys = cluster(city_rows)
            for row in city_rows:
                label, kind = classify(row["name"])
                key = keys.get(row["id"])
                counts["rows"] += 1
                counts[f"kind:{kind}"] += 1
                if label != (row["name"] or ""):
                    counts["label_changes"] += 1
                profiles.append((row["id"], label, key, kind == "person", kind))
                if key:
                    per_key[(banana, key)] += 1
        # Rows sharing a key beyond the first are duplicate spellings of one member.
        counts["clustered_duplicates"] = sum(n - 1 for n in per_key.values() if n > 1)

        bodies: Dict[Tuple[str, str], dict] = {}
        for row in body_rows:
            body = body_of(row["body"])
            if not body:
                counts["votes_with_no_body"] += row["vote_count"]
                continue
            key = (row["council_member_id"], body)
            existing = bodies.get(key)
            if existing is None:
                bodies[key] = {**row, "body": body}
            else:
                existing["vote_count"] += row["vote_count"]
                existing["first_vote"] = min(filter(None, (existing["first_vote"], row["first_vote"])), default=None)
                existing["last_vote"] = max(filter(None, (existing["last_vote"], row["last_vote"])), default=None)
        # A body is seen as a venue when it hosts a meeting, and as an actor when
        # a roster row names it instead of a person. Both are real and a body is
        # routinely both: Jacksonville's Land Use & Zoning Committee hosts its own
        # meetings and sponsors matters at council.
        body_rows: Dict[Tuple[str, str], dict] = {}
        for (member_id, body), _ in bodies.items():
            banana = member_banana[member_id]
            entry = body_rows.setdefault((banana, fold(body)),
                                         {"name": body, "venue": False, "actor": False})
            entry["venue"] = True
        for banana, city_rows in by_city.items():
            for row in city_rows:
                label, kind = classify(row["name"])
                if kind != "body":
                    continue
                name = body_of(label) or label
                entry = body_rows.setdefault((banana, fold(name)),
                                             {"name": name, "venue": False, "actor": False})
                entry["actor"] = True
                actor_of_row[row["id"]] = (banana, fold(name))

        counts["member_body_rows"] = len(bodies)
        counts["members_in_more_than_one_body"] = sum(
            1 for n in Counter(m for m, _ in bodies).values() if n > 1)

        print(f"roster rows            {counts['rows']}")
        print(f"  label changes        {counts['label_changes']}")
        print("  kinds                " + ", ".join(
        f"{k.split(':')[1]}={v}" for k, v in sorted(counts.items()) if k.startswith("kind:")))
        print(f"  clustered duplicates {counts['clustered_duplicates']}")
        print(f"profile rows to write  {len(profiles)}")
        # The same sponsorship, expressed with the actor it names. A role-only
        # sponsor ("Mayor") names no actor to attribute to and is left out rather
        # than guessed at; the raw sponsorship still records what was printed.
        kind_of: Dict[str, str] = {}
        for banana, city_rows in by_city.items():
            for row in city_rows:
                kind_of[row["id"]] = classify(row["name"])[1]
        matter_actors: Dict[Tuple[str, str, Optional[str], Optional[str]], dict] = {}
        for sp in sponsorships:
            member = sp["council_member_id"]
            kind = kind_of.get(member)
            if kind == "person":
                key = (sp["matter_id"], "sponsor", member, None)
            elif kind == "body" and member in actor_of_row:
                banana, folded = actor_of_row[member]
                key = (sp["matter_id"], "sponsor", None, body_id(banana, folded))
            else:
                counts[f"sponsorship_unattributable:{kind}"] += 1
                continue
            keep = matter_actors.setdefault(key, {"is_primary": False, "order": sp["sponsor_order"]})
            keep["is_primary"] = keep["is_primary"] or bool(sp["is_primary"])
        # A recommending body named in the minutes becomes an actor on the motion.
        # The name is resolved through the same body key as everything else, and a
        # body named only here still earns a row -- it acted, it just has no
        # meetings of its own in the corpus.
        motion_rows: List[Tuple[str, int, str, str, str]] = []
        for r in reported:
            banana, name = r["banana"], body_of(r["reported_body"]) or r["reported_body"]
            folded = fold(name)
            entry = body_rows.setdefault((banana, folded),
                                         {"name": name, "venue": False, "actor": False})
            entry["actor"] = True
            motion_rows.append((r["item_id"], r["motion_index"], r["source"],
                                "recommender", body_id(banana, folded)))
        counts["motion_actors"] = len(motion_rows)
        counts["matter_actors"] = len(matter_actors)
        counts["matter_actors_body"] = sum(1 for k in matter_actors if k[3])

        counts["bodies"] = len(body_rows)
        counts["bodies_seen_acting"] = sum(1 for v in body_rows.values() if v["actor"])
        print(f"bodies                 {counts['bodies']}"
              f"  (seen acting: {counts['bodies_seen_acting']})")
        print(f"member_bodies rows     {counts['member_body_rows']}")
        print(f"motion_actors          {counts['motion_actors']} (recommending bodies)")
        print(f"matter_actors          {counts['matter_actors']}"
              f"  (body sponsors: {counts['matter_actors_body']})")
        for k, v in sorted(counts.items()):
            if k.startswith("sponsorship_unattributable"):
                print(f"  unattributable {k.split(':')[1]:<10} {v}")
        print(f"  members in 2+ bodies {counts['members_in_more_than_one_body']}")
        print(f"  votes with no body   {counts['votes_with_no_body']}")

        if not args.apply:
            print("\ndry run; nothing written")
            return 0

        # Both derived tables are replaced in full for the scope being
        # normalized: a stale profile or a body a member no longer votes in is
        # not a fact, and nothing in council_members is touched either way.
        scope_sql = ("SELECT id FROM council_members"
                     " WHERE ($1::text IS NULL OR banana = $1)")
        async with db.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"DELETE FROM council_member_profiles WHERE council_member_id IN ({scope_sql})",
                    args.banana)
                await conn.executemany(
                    """INSERT INTO council_member_profiles
                           (council_member_id, display_name, person_key, is_person, actor_kind)
                       VALUES ($1, $2, $3, $4, $5)""", profiles)
                await conn.executemany(
                    """INSERT INTO bodies (id, banana, name, seen_as_venue, seen_as_actor)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (id) DO UPDATE SET
                           seen_as_venue = bodies.seen_as_venue OR EXCLUDED.seen_as_venue,
                           seen_as_actor = bodies.seen_as_actor OR EXCLUDED.seen_as_actor,
                           derived_at = CURRENT_TIMESTAMP""",
                    [(body_id(b, folded), b, v["name"], v["venue"], v["actor"])
                     for (b, folded), v in body_rows.items()])
                await conn.execute(
                    f"DELETE FROM member_bodies WHERE council_member_id IN ({scope_sql})",
                    args.banana)
                await conn.execute(
                    "DELETE FROM motion_actors WHERE body_id IN"
                    " (SELECT id FROM bodies WHERE ($1::text IS NULL OR banana = $1))",
                    args.banana)
                await conn.executemany(
                    """INSERT INTO motion_actors (item_id, motion_index, source, role, body_id)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT DO NOTHING""", motion_rows)
                await conn.execute(
                    "DELETE FROM matter_actors WHERE person_id IN (" + scope_sql + ")"
                    " OR body_id IN (SELECT id FROM bodies WHERE ($1::text IS NULL OR banana = $1))",
                    args.banana)
                await conn.executemany(
                    """INSERT INTO matter_actors
                           (matter_id, role, person_id, body_id, is_primary, actor_order)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    [(m, r, p_id, b_id, v["is_primary"], v["order"])
                     for (m, r, p_id, b_id), v in matter_actors.items()])
                await conn.executemany(
                    """INSERT INTO member_bodies
                           (council_member_id, body, vote_count, first_vote, last_vote)
                       VALUES ($1, $2, $3, $4, $5)""",
                    [(m, b, r["vote_count"], r["first_vote"], r["last_vote"])
                     for (m, b), r in bodies.items()])
        logger.info("roster normalized", profiles=len(profiles), bodies=len(bodies))
        print(f"\nwrote {len(profiles)} profiles and {len(bodies)} member_bodies rows;"
              f" council_members untouched")
        return 0
    finally:
        await db.pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
