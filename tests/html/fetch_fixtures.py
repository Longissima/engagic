"""Fetch HTML agenda fixtures from the prod-derived seed TSV.

Reads tests/html_corpus_seed.tsv (vendor x ok/fallback x city, pulled from
prod 2026-06-11), fetches each meeting's agenda HTML the way its adapter
would (CivicPlus: ?html=true on the ViewFile base; Granicus: follow the
AgendaViewer redirect chain and record final_url — it drives dialect
dispatch; PrimeGov: Portal/Meeting page as-is), and writes manifest.json.
Fixtures land in tests/html/fixtures/ (gitignored).

Non-HTML outcomes are corpus signal, not errors: 'pdf_redirect' means the
city serves a PDF where HTML was hoped for; 'no_html_agenda' means
CivicPlus has no HTML rendering for this meeting.

Usage:
    uv run python tests/html/fetch_fixtures.py
"""

import csv
import hashlib
import json
import random
import sys
import time
from pathlib import Path

from curl_cffi import requests

HERE = Path(__file__).parent
SEED = HERE.parent / "html_corpus_seed.tsv"
FIXTURES = HERE / "fixtures"
MANIFEST = HERE / "manifest.json"
TIMEOUT = 40


def fetch_url(row: dict) -> str:
    url = row["agenda_url"]
    if row["vendor"] == "civicplus":
        return url.split("?")[0] + "?html=true"
    return url


def fetch(url: str):
    try:
        resp = requests.get(url, impersonate="chrome", timeout=TIMEOUT,
                            allow_redirects=True)
    except requests.exceptions.CertificateVerifyError:
        try:
            resp = requests.get(url, impersonate="chrome", timeout=TIMEOUT,
                                allow_redirects=True, verify=False)
        except Exception as e:
            return f"error:{type(e).__name__}", None
    except Exception as e:
        return f"error:{type(e).__name__}", None
    if resp.status_code != 200:
        return f"http_{resp.status_code}", None
    return "ok", resp


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    prior = {}
    if MANIFEST.exists():
        for e in json.loads(MANIFEST.read_text())["fixtures"]:
            if e.get("fetch_status") == "ok" and (FIXTURES / e["filename"]).exists():
                prior[e["meeting_id"]] = e

    entries = []
    rows = list(csv.DictReader(open(SEED), delimiter="\t"))
    ok = 0
    for i, row in enumerate(rows):
        if row["meeting_id"] in prior:
            entries.append(prior[row["meeting_id"]])
            ok += 1
            continue

        url = fetch_url(row)
        entry = {
            "meeting_id": row["meeting_id"],
            "banana": row["banana"],
            "vendor": row["vendor"],
            "prod_outcome": row["outcome"],
            "meeting_date": row["date"],
            "url": url,
        }
        status, resp = fetch(url)
        if resp is not None:
            body = resp.content or b""
            final_url = str(resp.url)
            if body.lstrip()[:5].startswith(b"%PDF"):
                status = "pdf_redirect"
            elif row["vendor"] == "civicplus" and (
                b'<div id="divItems"' not in body and b'class="item level' not in body
            ):
                status = "no_html_agenda"
            else:
                filename = f"{row['meeting_id']}.html"
                (FIXTURES / filename).write_bytes(body)
                entry.update(
                    filename=filename,
                    final_url=final_url,
                    sha256=hashlib.sha256(body).hexdigest(),
                    size=len(body),
                )
                ok += 1
        entry["fetch_status"] = status
        entries.append(entry)
        print(f"[{i + 1}/{len(rows)}] {row['vendor']:>10} {row['banana']:<36} {status}")
        time.sleep(0.2 + random.random() * 0.3)

    MANIFEST.write_text(json.dumps({"fixtures": entries}, indent=2))
    print(f"\n{ok}/{len(rows)} HTML fixtures -> {FIXTURES}")


if __name__ == "__main__":
    sys.exit(main())
