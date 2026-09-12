#!/usr/bin/env python3
"""A/B the roll-call parser against a git ref over every saved minutes run.

The unit tests cover the layout you happen to be looking at. This covers the
ones you are not: a fix for one city's format routinely breaks another's, and
only a full replay shows it. Three of five attendance changes on 2026-09-12 were
wrong on the first attempt with a green test suite every time.

Baseline is the parser at a git ref, loaded as throwaway modules and rebound into
the importers; candidate is the working tree. Both replay the same saved text and
saved inputs, so nothing is fetched, no model is called, and nothing is written.

What it reports: published member ballots gained and lost per city, every lost
ballot named, and the motion count either side. A loss is the signal -- a member
whose recorded vote stopped publishing is a regression until explained.

Usage:
    uv run scripts/replay_rollcall_ab.py
    uv run scripts/replay_rollcall_ab.py --ref HEAD~3 --banana milwaukeeWI
    uv run scripts/replay_rollcall_ab.py --limit 500 --json /tmp/ab.json

Confidence 8/10: parity at the publish gate implies parity in published data,
because persist_meeting writes exactly what the gate returns. It cannot see
changes below the gate (identity reconciliation, item anchoring) that leave the
published tuples identical.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import parsing.rollcall.engine as engine
import parsing.rollcall.evidence as evidence
import parsing.rollcall.observations as observations
from config import get_logger
from database.db_postgres import Database
from parsing.rollcall.identity import MemberIdentities, reconcile_members

logger = get_logger(__name__).bind(component="replay_rollcall_ab")

# Rebound wholesale rather than patched individually: these are the helpers the
# importers captured at import time, so a module-level swap is the only way to
# make an already-imported caller use the baseline copy.
NAME_HELPERS = ("clean_name", "split_names", "fold", "looks_like_name")
PARSER_MODULES = ("names", "attendance", "evidence", "align", "observations")

RUNS_SQL = """
    SELECT DISTINCT ON (r.meeting_id) r.meeting_id, r.inputs, t.text_content
    FROM minutes_parse_runs r
    JOIN minutes_text_snapshots t USING (text_sha256)
    WHERE r.inputs IS NOT NULL
      AND ($1::text IS NULL OR split_part(r.meeting_id, '_', 1) = $1)
    ORDER BY r.meeting_id, r.created_at DESC
"""


def load_baseline(ref: str) -> Dict[str, types.ModuleType]:
    """Build the parser modules as they stand at a git ref.

    Each module is exec'd into a bare namespace with its intra-package imports
    satisfied from the baseline set already built, so the baseline never reaches
    back into the working tree.
    """
    built: Dict[str, types.ModuleType] = {}
    for name in PARSER_MODULES:
        path = f"parsing/rollcall/{name}.py"
        try:
            source = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                capture_output=True, text=True, check=True,
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"cannot read {path} at {ref}: {exc.stderr.strip()}")
        module = types.ModuleType(f"baseline_{name}")
        module.__file__ = path
        # Satisfy "from parsing.rollcall.X import y" against the baseline copies.
        for built_name, built_module in built.items():
            for attr in dir(built_module):
                if not attr.startswith("_"):
                    module.__dict__.setdefault(attr, getattr(built_module, attr))
            source = source.replace(
                f"from parsing.rollcall.{built_name} import", "# baseline:"
            )
        exec(compile(source, path, "exec"), module.__dict__)
        built[name] = module
    return built


class Swap:
    """Run each side's own pipeline, not a hybrid of the two.

    Swapping only the leaf helpers and leaving the candidate's observe_meeting in
    place measures "candidate gate over baseline helpers", which is nobody's code.
    It also mixes dataclasses across versions: a baseline Attendance reaching the
    candidate validator has none of the candidate's newer fields, which skipped
    2,070 meetings before this was fixed. Each arm now entertains only its own
    modules, down to the helpers that `engine` captured at import time.
    """

    def __init__(self, baseline: Dict[str, types.ModuleType]):
        self.baseline = baseline
        self.live = {
            module: {a: getattr(module, a) for a in NAME_HELPERS if hasattr(module, a)}
            for module in (engine, evidence, observations)
        }

    def use(self, which: str) -> None:
        names = self.baseline["names"]
        for module, attrs in self.live.items():
            for attr, live in attrs.items():
                setattr(module, attr, getattr(names, attr) if which == "baseline" else live)

    def observe(self, which: str, text: str, inputs: dict):
        module = self.baseline["observations"] if which == "baseline" else observations
        return module.observe_meeting(
            text, inputs["items"], [r["name"] for r in inputs["roster"]], inputs["dialect"]
        )


def replay(swap: "Swap", which: str, text: str, inputs: dict):
    parsed = swap.observe(which, text, inputs)
    reconcile_members(parsed, MemberIdentities(
        inputs["roster"], [v["council_member_id"] for v in inputs["api_votes"]]
    ))
    return parsed


def ballots(parsed) -> Counter:
    """One entry per published member ballot, keyed so a move is visible."""
    out: Counter = Counter()
    for pub in parsed.published:
        item = pub.item.get("id") if isinstance(pub.item, dict) else pub.item
        for name, value in (pub.member_votes or []):
            out[(item, pub.motion_index, name, value)] += 1
    return out


def slots(counter: Counter) -> Counter:
    """Ballots without the member name: the seat a vote was published into."""
    out: Counter = Counter()
    for (item, index, _, value), count in counter.items():
        out[(item, index, value)] += count
    return out


def motions(parsed) -> int:
    return len(parsed.published)


async def fetch_runs(banana: Optional[str], limit: Optional[int]) -> List[dict]:
    db = await Database.create()
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("SET TRANSACTION READ ONLY")
            rows = await conn.fetch(RUNS_SQL, banana)
    finally:
        await db.close()
    runs = [dict(r) for r in rows]
    return runs[:limit] if limit else runs


def compare(runs: List[dict], swap: Swap) -> Tuple[Counter, Counter, Counter, Counter, List[str]]:
    totals: Counter = Counter()
    gained: Counter = Counter()
    lost_detail: Counter = Counter()
    renamed: Counter = Counter()
    skipped: List[str] = []
    for run in runs:
        text = run["text_content"] or ""
        inputs = run["inputs"]
        if isinstance(inputs, str):
            inputs = json.loads(inputs)
        if not text or not inputs:
            continue
        city = run["meeting_id"].rsplit("_", 1)[0]
        totals["meetings"] += 1
        try:
            swap.use("candidate")
            new_parse = replay(swap, "candidate", text, inputs)
            new, new_motions = ballots(new_parse), motions(new_parse)
            swap.use("baseline")
            old_parse = replay(swap, "baseline", text, inputs)
            old, old_motions = ballots(old_parse), motions(old_parse)
        except Exception as exc:
            # Never swallowed: a skipped meeting is an unmeasured meeting, and
            # silent skips once hid two cities' losses from this very report.
            skipped.append(f"{run['meeting_id']}: {type(exc).__name__}: {exc}")
            totals["skipped"] += 1
            continue
        finally:
            swap.use("candidate")
        totals["ballots_candidate"] += sum(new.values())
        totals["ballots_baseline"] += sum(old.values())
        totals["motions_candidate"] += new_motions
        totals["motions_baseline"] += old_motions
        if new == old:
            continue
        totals["meetings_differing"] += 1
        gained[city] += sum((new - old).values())
        # A ballot whose slot still publishes under a different canonical name is
        # a rename, not a loss: "Turner" becoming "Robert Turner" is the identity
        # fix working. Only an emptied slot is a regression.
        new_slots, old_slots = slots(new), slots(old)
        for (item, index, name, value), count in (old - new).items():
            slot = (item, index, value)
            if new_slots[slot] >= old_slots[slot]:
                renamed[(city, name, value)] += count
                continue
            lost_detail[(city, name, value)] += count
    return totals, gained, lost_detail, renamed, skipped


def report(totals, gained, lost_detail, renamed, skipped, ref) -> dict:
    lost_by_city: Counter = Counter()
    for (city, _, _), count in lost_detail.items():
        lost_by_city[city] += count
    delta = totals["ballots_candidate"] - totals["ballots_baseline"]
    print(f"\nbaseline {ref} vs working tree over {totals['meetings']} meetings")
    print(f"  ballots   {totals['ballots_baseline']} -> {totals['ballots_candidate']}  ({delta:+d})")
    print(f"  motions   {totals['motions_baseline']} -> {totals['motions_candidate']}")
    print(f"  meetings differing: {totals['meetings_differing']}")
    print(f"\nGAINED {sum(gained.values())} ballots across {len(gained)} cities")
    for city, count in gained.most_common(20):
        print(f"   +{count:>6}  {city}")
    print(f"\nLOST {sum(lost_by_city.values())} ballots across {len(lost_by_city)} cities")
    for city, count in lost_by_city.most_common(20):
        print(f"   -{count:>6}  {city}")
    if renamed:
        print(f"\nRENAMED {sum(renamed.values())} ballots -- same slot, new canonical name:")
        for (city, name, value), count in renamed.most_common(15):
            print(f"   {count:>4}  {city:<20} was {name!r} [{value}]")
    if lost_detail:
        print("\nevery LOST ballot (a slot that now publishes nobody):")
        for (city, name, value), count in lost_detail.most_common(60):
            print(f"   {count:>4}  {city:<20} {name!r} [{value}]")
    if skipped:
        print(f"\nSKIPPED {len(skipped)} meetings -- these are unmeasured, not passing:")
        for line in skipped[:20]:
            print(f"   {line}")
    return {
        "ref": ref,
        "totals": dict(totals),
        "gained": {k: v for k, v in gained.items()},
        "renamed": {f"{c}|{n}|{v}": n2 for (c, n, v), n2 in renamed.items()},
        "lost": {f"{c}|{n}|{v}": c2 for (c, n, v), c2 in lost_detail.items()},
        "skipped": skipped,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="HEAD", help="git ref for the baseline parser")
    ap.add_argument("--banana", help="restrict to one jurisdiction")
    ap.add_argument("--limit", type=int, help="first N meetings only, for a smoke run")
    ap.add_argument("--json", type=Path, help="write the full result here")
    ap.add_argument("--fail-on-loss", action="store_true",
                    help="exit non-zero if any ballot was lost, for CI")
    args = ap.parse_args()

    baseline = load_baseline(args.ref)
    swap = Swap(baseline)
    runs = await fetch_runs(args.banana, args.limit)
    if not runs:
        print("no saved runs matched")
        return 0
    logger.info("replaying saved minutes", meetings=len(runs), ref=args.ref)
    totals, gained, lost_detail, renamed, skipped = compare(runs, swap)
    result = report(totals, gained, lost_detail, renamed, skipped, args.ref)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2))
        print(f"\nwrote {args.json}")
    if args.fail_on_loss and lost_detail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
