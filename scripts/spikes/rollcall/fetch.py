"""Fetch ground truth from the Legistar web API and download minutes PDFs.

Cache-first: every HTTP GET is stored under cache/ keyed by a slug of the URL,
so re-runs make zero network calls. Polite: sequential, small sleeps.
"""

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CACHE = ROOT / "cache"
PDFS = ROOT / "pdfs"
OUT = ROOT / "out"
BASE = "https://webapi.legistar.com/v1"
UA = {"User-Agent": "rollcall-feasibility-spike/0.1 (research; contact ibansadowski@icloud.com)"}

CLIENTS = ["milwaukee", "denver"]
# Prefer full-council bodies; committees are usable but council is vote-richest.
COUNCIL_PAT = re.compile(r"(common council|city council)", re.I)
EVENTS_PER_CLIENT = 4


def prepare_output_dirs() -> None:
    """Create the generated-data directories used by a clean checkout."""
    for directory in (CACHE, PDFS, OUT):
        directory.mkdir(parents=True, exist_ok=True)


def slug(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", url.replace(BASE + "/", ""))[:180]


def get_json(url: str):
    p = CACHE / (slug(url) + ".json")
    if p.exists():
        return json.loads(p.read_text())
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    time.sleep(0.5)
    obj = json.loads(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1))
    return obj


def get_pdf(url: str, dest: Path) -> bool:
    if dest.exists():
        return True
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
    except Exception as e:
        print(f"    PDF FAIL {url}: {e}", file=sys.stderr)
        return False
    time.sleep(0.5)
    if not data[:5].startswith(b"%PDF"):
        print(f"    NOT A PDF ({len(data)} bytes) {url}", file=sys.stderr)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def main():
    prepare_output_dirs()
    manifest = {}
    for client in CLIENTS:
        print(f"== {client}")
        url = (
            f"{BASE}/{client}/Events?$orderby=EventDate+desc&$top=120"
            "&$filter=EventMinutesFile+ne+null"
        )
        try:
            events = get_json(url)
        except Exception as e:
            print(f"  filter query failed ({e}); falling back to plain top-200")
            events = [
                e
                for e in get_json(f"{BASE}/{client}/Events?$orderby=EventDate+desc&$top=200")
                if e.get("EventMinutesFile")
            ]
        council = [e for e in events if COUNCIL_PAT.search(e.get("EventBodyName") or "")]
        pool = council if len(council) >= EVENTS_PER_CLIENT else events
        print(f"  {len(events)} events w/ minutes, {len(council)} council; using pool of {len(pool)}")

        chosen = []
        for ev in pool:
            if len(chosen) >= EVENTS_PER_CLIENT:
                break
            eid = ev["EventId"]
            pdf_path = PDFS / f"{client}_{eid}.pdf"
            if not get_pdf(ev["EventMinutesFile"], pdf_path):
                continue
            items = get_json(f"{BASE}/{client}/Events/{eid}/EventItems")
            n_votes_items = 0
            gt_items = []
            for it in items:
                itid = it["EventItemId"]
                votes = get_json(f"{BASE}/{client}/EventItems/{itid}/Votes")
                if votes:
                    n_votes_items += 1
                gt_items.append(
                    {
                        "EventItemId": itid,
                        "MatterFile": it.get("EventItemMatterFile"),
                        "Title": it.get("EventItemTitle"),
                        "PassedFlag": it.get("EventItemPassedFlag"),
                        "PassedFlagName": it.get("EventItemPassedFlagName"),
                        "ActionName": it.get("EventItemActionName"),
                        "Mover": it.get("EventItemMover"),
                        "Seconder": it.get("EventItemSeconder"),
                        "AgendaNumber": it.get("EventItemAgendaNumber"),
                        "Votes": [
                            {
                                "Person": v.get("VotePersonName"),
                                "Value": v.get("VoteValueName"),
                            }
                            for v in votes
                        ],
                    }
                )
            print(
                f"  event {eid} {ev.get('EventDate','')[:10]} {ev.get('EventBodyName')}: "
                f"{len(items)} items, {n_votes_items} with roll-call votes"
            )
            if n_votes_items == 0:
                # useless as ground truth; skip but keep cache
                continue
            chosen.append(
                {
                    "EventId": eid,
                    "EventDate": ev.get("EventDate"),
                    "Body": ev.get("EventBodyName"),
                    "MinutesUrl": ev["EventMinutesFile"],
                    "PdfPath": str(pdf_path),
                    "Items": gt_items,
                }
            )
        manifest[client] = chosen
    (OUT / "ground_truth.json").write_text(json.dumps(manifest, indent=1))
    total = sum(len(v) for v in manifest.values())
    print(f"manifest written: {total} events")


if __name__ == "__main__":
    main()
