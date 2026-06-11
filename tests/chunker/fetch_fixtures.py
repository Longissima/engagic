"""Fetch chunker corpus fixtures from the prod-derived seed TSV.

Reads tests/chunker_corpus_seed.tsv (stratified sample: vendor x ok/fallback
x city, pulled from prod 2026-06-10), downloads each meeting's chunker-input
PDF, and writes tests/chunker/manifest.json recording provenance + sha256 +
fetch status. Fixtures land in tests/chunker/fixtures/ (gitignored — URLs
rot, so refetches are best-effort; the manifest's sha256 pins what the
goldens were generated against).

Usage:
    uv run python tests/chunker/fetch_fixtures.py
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
SEED = HERE.parent / "chunker_corpus_seed.tsv"
FIXTURES = HERE / "fixtures"
MANIFEST = HERE / "manifest.json"

MAX_BYTES = 80 * 1024 * 1024
TIMEOUT = 40


def chunker_input_url(row: dict) -> tuple[str, str]:
    """Pick the URL the chunker would actually receive.

    packet_url is the chunker's primary input. agenda_url is used when no
    packet exists — stripping ?html=true turns CivicPlus HTML agenda views
    back into their PDF form.
    """
    if row["packet_url"]:
        return row["packet_url"], "packet"
    url = row["agenda_url"].replace("?html=true", "").replace("&html=true", "")
    return url, "agenda"


def fetch(url: str) -> tuple[str, bytes]:
    try:
        resp = requests.get(
            url, impersonate="chrome", timeout=TIMEOUT, allow_redirects=True
        )
    except requests.exceptions.CertificateVerifyError:
        # several CivicPlus custom domains present chains curl_cffi rejects;
        # fixtures are sha256-pinned so unverified fetch is acceptable here
        try:
            resp = requests.get(
                url, impersonate="chrome", timeout=TIMEOUT,
                allow_redirects=True, verify=False,
            )
        except Exception as e:
            return f"error:{type(e).__name__}", b""
    except Exception as e:
        return f"error:{type(e).__name__}", b""
    if resp.status_code != 200:
        return f"http_{resp.status_code}", b""
    body = resp.content or b""
    if len(body) > MAX_BYTES:
        return "too_large", b""
    if not body.lstrip()[:5].startswith(b"%PDF"):
        return "not_pdf", b""
    return "ok", body


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    # resume: keep prior successful fetches, only retry failures
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
        url, url_kind = chunker_input_url(row)
        entry = {
            "meeting_id": row["meeting_id"],
            "banana": row["banana"],
            "vendor": row["vendor"],
            "prod_outcome": row["outcome"],
            "meeting_date": row["date"],
            "url": url,
            "url_kind": url_kind,
        }
        if not url:
            entry["fetch_status"] = "no_url"
            entries.append(entry)
            continue

        status, body = fetch(url)
        entry["fetch_status"] = status
        if status == "ok":
            filename = f"{row['meeting_id']}.pdf"
            (FIXTURES / filename).write_bytes(body)
            entry["filename"] = filename
            entry["sha256"] = hashlib.sha256(body).hexdigest()
            entry["size"] = len(body)
            ok += 1
        entries.append(entry)
        print(f"[{i + 1}/{len(rows)}] {row['vendor']:>14} {row['banana']:<40} {status}")
        time.sleep(0.2 + random.random() * 0.3)

    MANIFEST.write_text(json.dumps({"fixtures": entries}, indent=2))
    print(f"\n{ok}/{len(rows)} fixtures fetched -> {FIXTURES}")
    print(f"manifest -> {MANIFEST}")


if __name__ == "__main__":
    sys.exit(main())
