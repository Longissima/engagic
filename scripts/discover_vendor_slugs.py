"""Resumable discovery seeded by a city list; guesses are never registrations.

Examples (run from repository root):
  python -m scripts.discover_vendor_slugs probe --seed munis/discovery-seed-2026-09.csv --out data/slug-discovery-2026-09 --vendors civicclerk --patterns 2
  python -m scripts.discover_vendor_slugs validate --out data/slug-discovery-2026-09
  python -m scripts.discover_vendor_slugs apply --out data/slug-discovery-2026-09 [--apply]

Probe learns template rankings from city records with stored meetings, logs every
attempt, and preserves plausible responses for validation. Only explicit city /
state address evidence and recent municipal agenda data can pass validation.
Apply is insert/repair only: existing populated or inactive records are protected.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import aiohttp
import asyncpg
from bs4 import BeautifulSoup

from config import Config
from scripts._jurisdiction_naming import to_banana_slug
from scripts.boardbook_bulk_import import _US_STATE_NAMES

STATE_NAMES = {v: k for k, v in _US_STATE_NAMES.items()}
VENDORS = (
    "civicclerk",
    "legistar",
    "granicus",
    "civicplus",
    "primegov",
    "escribe",
    "iqm2",
    "municode",
    "novusagenda",
    "civicweb",
)
MUNICIPAL_BODY = re.compile(
    r"\b(city council|town council|village (?:board|council)|board of (?:aldermen|selectmen|trustees)|select board|common council|municipal council|commissioners|city commission|assembly)\b",
    re.I,
)
FOREIGN_BODY = re.compile(
    r"\b(school|county|parish|library|water district|housing authority)\b", re.I
)


def norm(text):
    return to_banana_slug(text) if isinstance(text, str) else ""


def templates(name, state):
    city, st = norm(name), state.lower()
    dashed = re.sub(
        r"[^a-z0-9]+",
        "-",
        unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode(),
    ).strip("-")
    return {
        "city": city,
        "cityst": city + st,
        "city-st": city + "-" + st,
        "st-city": st + "-" + city,
        "cityofcity": "cityof" + city,
        "cityofcityst": "cityof" + city + st,
        "townofcity": "townof" + city,
        "villageofcity": "villageof" + city,
        "citycityst": city + "city" + st,
        "citytownst": city + "town" + st,
        "citycity": city + "city",
        "citygov": city + "gov",
        "city-fullstate": city + norm(STATE_NAMES[state]),
        "pub-city": "pub-" + city,
        "pub-cityst": "pub-" + city + st,
        "pub-city-st": "pub-" + city + "-" + st,
        "pub-cityofcity": "pub-cityof" + city,
        "pub-dashed": "pub-" + dashed,
        "dashed": dashed,
        "dashed-st": dashed + "-" + st,
        "st-city2": st + "-" + city + "2",
        "st-citytownship": st + "-" + city + "township",
        "citytownshipst": city + "township" + st,
    }


def learn_patterns(registry):
    counts = defaultdict(Counter)
    examples = defaultdict(lambda: defaultdict(list))
    for row in registry:
        if row["type"] != "city" or not row["meetings"] or row["status"] != "active":
            continue
        for template, slug in templates(row["name"], row["state"]).items():
            if slug == row["slug"].lower():
                counts[row["vendor"]][template] += 1
                if len(examples[row["vendor"]][template]) < 4:
                    examples[row["vendor"]][template].append(
                        f"{row['name']}, {row['state']}: {row['slug']}"
                    )
                break
    return {
        v: [
            {"template": t, "count": c, "examples": examples[v][t]}
            for t, c in counts[v].most_common()
        ]
        for v in VENDORS
    }


def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path, value):
    with path.open("a") as handle:
        handle.write(json.dumps(value, default=str) + "\n")


async def registry_snapshot(conn):
    async with conn.transaction(readonly=True):
        rows = await conn.fetch("""SELECT j.banana,j.name,j.state,j.vendor,j.slug,j.type,j.status,
            (SELECT count(*) FROM meetings m WHERE m.banana=j.banana) AS meetings
            FROM jurisdictions j""")
    return [dict(r) for r in rows]


def load_targets(seed, registry):
    by_banana = {r["banana"]: r for r in registry}
    by_name = {(norm(r["name"]), r["state"]): r for r in registry}
    targets = []
    seen = set()
    with seed.open() as handle:
        for row in csv.DictReader(handle):
            name, state = row["city"].strip(), row["state"].strip().upper()
            if not name or state not in STATE_NAMES:
                raise ValueError(f"Invalid seed: {row!r}")
            key = (norm(name), state)
            if key in seen:
                raise ValueError(f"Duplicate seed: {key}")
            seen.add(key)
            existing = by_banana.get(row.get("banana")) or by_name.get(key)
            if existing and (
                existing["meetings"]
                or existing["status"] != "active"
                or existing["type"] != "city"
            ):
                continue
            targets.append(
                dict(
                    name=name,
                    state=state,
                    banana=existing["banana"] if existing else norm(name) + state,
                    existing=existing,
                )
            )
    return targets


def probe_url(vendor, slug, now):
    if vendor == "civicclerk":
        since = (now - timedelta(days=120)).strftime("%Y-%m-%dT00:00:00Z")
        until = (now + timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
        return f"https://{slug}.api.civicclerk.com/v1/Events?$filter=startDateTime%20gt%20{since}%20and%20startDateTime%20lt%20{until}&$orderby=startDateTime%20desc&$top=100"
    return {
        "legistar": f"https://{slug}.legistar.com/Calendar.aspx",
        "granicus": f"https://{slug}.granicus.com/ViewPublisher.php?view_id=1",
        "civicplus": f"https://{slug}.civicplus.com/AgendaCenter",
        "primegov": f"https://{slug}.primegov.com/public/portal",
        "escribe": f"https://{slug}.escribemeetings.com/",
        "iqm2": f"https://{slug}.iqm2.com/Citizens/Default.aspx",
        "municode": f"https://{slug}.municodemeetings.com/",
        "novusagenda": f"https://{slug}.novusagenda.com/agendapublic/",
        "civicweb": f"https://{slug}.civicweb.net/Portal/",
    }[vendor]


def address_matches(location, name, state):
    """Match structured address fields exactly, never arbitrary state substrings."""
    return (
        isinstance(location, dict)
        and norm(location.get("city")) == norm(name)
        and norm(location.get("state")) in {norm(state), norm(STATE_NAMES[state])}
    )


def recent_date(value, now, days=120):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return now - timedelta(days=days) <= dt <= now + timedelta(days=30)
    except (ValueError, TypeError, AttributeError):
        return False


def civicclerk_evidence(data, target, now):
    if not isinstance(data, dict) or not isinstance(data.get("value"), list):
        return []
    good = []
    for event in data["value"]:
        if not isinstance(event, dict):
            continue
        title = event.get("eventName")
        if not isinstance(title, str):
            continue
        if (
            MUNICIPAL_BODY.search(title)
            and not FOREIGN_BODY.search(title)
            and address_matches(
                event.get("eventLocation"), target["name"], target["state"]
            )
            and recent_date(event.get("startDateTime"), now)
            and event.get("agendaId")
            and event.get("hasAgenda")
            and any(
                d.get("fileId") and re.search("agenda|packet", d.get("type", ""), re.I)
                for d in event.get("publishedFiles", [])
            )
        ):
            good.append(event)
    return good


async def read_limited(response, limit=3_000_000):
    chunks = []
    size = 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > limit:
            raise ValueError("response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_probe(session, target, vendor, slug, now, out):
    record = dict(
        name=target["name"],
        state=target["state"],
        banana=target["banana"],
        vendor=vendor,
        slug=slug,
        checked_at=now.isoformat(),
        body_complete=False,
        probe_version=2,
    )
    url = probe_url(vendor, slug, now)
    record["url"] = url
    try:
        async with session.get(url, allow_redirects=True, max_redirects=5) as response:
            record.update(http_status=response.status, final_url=str(response.url))
            body = await read_limited(response)
            record["body_complete"] = True
            text = body.decode("utf-8", errors="replace")
            plausible = False
            if response.status == 200:
                if vendor == "civicclerk":
                    try:
                        data = json.loads(text)
                        plausible = isinstance(data, dict) and bool(data.get("value"))
                        record["identity_events"] = len(
                            civicclerk_evidence(data, target, now)
                        )
                    except (ValueError, TypeError):
                        pass
                else:
                    soup = BeautifulSoup(text, "html.parser")
                    record["title"] = (
                        soup.title.get_text(" ", strip=True) if soup.title else ""
                    )
                    # This is a LEAD, not identity verification or authorization to insert.
                    visible = soup.get_text(" ", strip=True)
                    plausible = norm(target["name"]) in norm(
                        record["title"] + " " + visible[:20000]
                    ) and bool(re.search("agenda|meeting", visible, re.I))
                if plausible:
                    filename = hashlib.sha256(url.encode()).hexdigest()[:20] + ".txt"
                    (out / "responses").mkdir(exist_ok=True)
                    (out / "responses" / filename).write_text(text)
                    record["response_file"] = filename
            record["status"] = "lead" if plausible else "no_match"
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        record.update(status="request_failed", error=type(exc).__name__)
    return record


def retryable_probe(record):
    return record.get("http_status") in {429, 500, 502, 503, 504} or record.get(
        "error"
    ) in {
        "TimeoutError",
        "ServerDisconnectedError",
        "ClientOSError",
        "ClientPayloadError",
    }


async def probe(args):
    args.out.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(Config().get_postgres_dsn())
    try:
        registry = await registry_snapshot(conn)
    finally:
        await conn.close()
    targets = load_targets(args.seed, registry)
    patterns = learn_patterns(registry)
    (args.out / "patterns.json").write_text(json.dumps(patterns, indent=2))
    if not (args.out / "registry.json").exists():
        (args.out / "registry.json").write_text(json.dumps(registry, indent=2))
    (args.out / "targets.json").write_text(json.dumps(targets, indent=2))
    attempts_path = args.out / "attempts.jsonl"
    seen = {
        (r["name"], r["state"], r["vendor"], r["slug"])
        for r in read_jsonl(attempts_path)
        if (r.get("probe_version") == 2 or r.get("body_complete"))
        and not (args.retry_failures and retryable_probe(r))
    }
    known = defaultdict(set)
    for r in registry:
        known[(r["vendor"], r["slug"].lower())].add((norm(r["name"]), r["state"]))
    queue = asyncio.Queue()
    for target in targets:
        for vendor in args.vendors.split(","):
            if vendor not in VENDORS:
                raise ValueError(f"Unsupported probe vendor {vendor}")
            generated = templates(target["name"], target["state"])
            slugs = list(
                dict.fromkeys(
                    generated[p["template"]] for p in patterns[vendor][: args.patterns]
                )
            )
            for slug in slugs:
                key = (target["name"], target["state"], vendor, slug)
                # A tenant already assigned to another city is not new coverage.
                owners = known.get((vendor, slug), set())
                if owners and (norm(target["name"]), target["state"]) not in owners:
                    continue
                if key not in seen:
                    queue.put_nowait((target, vendor, slug))
    if args.limit:
        limited = asyncio.Queue()
        for _ in range(min(args.limit, queue.qsize())):
            limited.put_nowait(queue.get_nowait())
        queue = limited
    total = queue.qsize()
    print(f"Targets: {len(targets)}, pending probes: {total}", flush=True)
    counters = Counter()
    now = datetime.now(timezone.utc)
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(
        limit=args.concurrency, limit_per_host=1, ttl_dns_cache=600
    )
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=timeout,
        headers={"User-Agent": "Engagic municipal meeting source discovery"},
    ) as session:

        async def worker():
            while not queue.empty():
                target, vendor, slug = queue.get_nowait()
                record = await fetch_probe(session, target, vendor, slug, now, args.out)
                append_jsonl(attempts_path, record)
                counters[record["status"]] += 1
                if record["status"] == "lead":
                    append_jsonl(args.out / "leads.jsonl", record)
                    print(
                        f"LEAD {record['name']}, {record['state']} {vendor}/{slug} identity_events={record.get('identity_events', 'unvalidated')}",
                        flush=True,
                    )
                if sum(counters.values()) % 100 == 0:
                    print(
                        f"Progress {sum(counters.values())}/{total}: {dict(counters)}",
                        flush=True,
                    )
                queue.task_done()

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    print(f"Completed: {dict(counters)}", flush=True)


def unique_leads(out):
    latest = {}
    for record in read_jsonl(out / "leads.jsonl"):
        if record.get("body_complete"):
            latest[
                (record["name"], record["state"], record["vendor"], record["slug"])
            ] = record
    return list(latest.values())


def html_identity(html, target):
    """Require city+state branding AND a matching postal address, not a substring."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for node in soup.select("script,style"):
        node.decompose()
    name, state = target["name"], target["state"]
    expected = {norm(name + state), norm(name + STATE_NAMES[state])}
    parts = re.split(r"[•|]", title)
    branded = any(
        norm(re.sub(r"^(?:City|Town|Village) of\s+", "", part.strip(), flags=re.I))
        in expected
        for part in parts
    )
    # Contact-address token requires ZIP; DE in 'department' cannot match.
    words = [
        re.escape(w)
        for w in re.findall(
            r"[a-z0-9]+",
            unicodedata.normalize("NFKD", name.lower())
            .encode("ascii", "ignore")
            .decode(),
        )
    ]
    city_pattern = r"[\W_]*".join(words)
    state_pattern = re.escape(state) + "|" + re.escape(STATE_NAMES[state])
    visible = (
        unicodedata.normalize("NFKD", soup.get_text(" ", strip=True))
        .encode("ascii", "ignore")
        .decode()
    )
    address = re.search(
        r"(?<![\w])"
        + city_pattern
        + r"\s*,?\s+(?:"
        + state_pattern
        + r")\s+\d{5}(?:-\d{4})?(?!\d)",
        visible,
        re.I,
    )
    if branded and address:
        return {"title": title, "address_text": address.group(0)}
    return None


async def validate_civicplus(lead, out, session, now, allow_committees=False):
    from vendors.adapters.civicplus_adapter_async import AsyncCivicPlusAdapter
    import fitz

    html = (out / "responses" / lead["response_file"]).read_text()
    identity = html_identity(html, lead)
    if not identity:
        return {"reason": "No exact city/state branding plus matching postal address"}
    adapter = AsyncCivicPlusAdapter(lead["slug"])
    links = adapter._extract_meeting_links(
        BeautifulSoup(html, "html.parser"), lead["final_url"]
    )
    candidates = []
    for link in links:
        if "/ViewFile/Agenda/" not in link["url"]:
            continue
        title = link.get("body_name", "") + " " + link["title"]
        if re.search(r"\bcancel(?:l)?ed\b|\bcancellation\b", title, re.I):
            continue
        eligible_body = MUNICIPAL_BODY.search(title) or (
            allow_committees
            and re.search(r"\b(board|commission|committee)\b", title, re.I)
        )
        if not eligible_body or (not allow_committees and FOREIGN_BODY.search(title)):
            continue
        meeting = adapter._create_meeting_from_viewfile_link(link)
        if meeting and recent_date(meeting.get("start"), now):
            candidates.append(meeting)
    for meeting in sorted(candidates, key=lambda m: m["start"], reverse=True)[:2]:
        try:
            async with session.get(
                meeting["packet_url"], timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    continue
                pdf = await read_limited(response, limit=8_000_000)
                if not pdf.startswith(b"%PDF"):
                    continue
            with fitz.open(stream=pdf, filetype="pdf") as doc:
                text = " ".join(doc[i].get_text() for i in range(min(2, len(doc))))
            body_in_document = MUNICIPAL_BODY.search(text) or (
                allow_committees
                and re.search(r"\b(board|commission|committee)\b", text, re.I)
            )
            if norm(lead["name"]) not in norm(text) or not body_in_document:
                continue
            return dict(
                status="verified",
                verification="civicplus_branded_address_recent_parsed_meeting_and_pdf",
                identity=identity,
                event_id=meeting["vendor_id"],
                event_title=meeting["title"],
                event_date=meeting["start"],
                document_url=meeting["packet_url"],
                document_sha256=hashlib.sha256(pdf).hexdigest(),
                document_bytes=len(pdf),
                extraction_level="meeting_and_pdf",
                source_scope="committee_sample"
                if allow_committees
                else "governing_body_sample",
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError):
            continue
    return {
        "reason": "Identity matches but no recent municipal meeting parsed with a readable agenda PDF"
    }


async def validate(args):
    from vendors.adapters.civicclerk_adapter_async import AsyncCivicClerkAdapter
    from vendors.session_manager_async import AsyncSessionManager

    now = datetime.now(timezone.utc)
    seen = {
        (r["name"], r["state"], r["vendor"], r["slug"])
        for r in read_jsonl(args.out / "validation.jsonl")
    }
    sem = asyncio.Semaphore(8)
    document_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=8, limit_per_host=1)
    )

    async def one(lead):
        key = (lead["name"], lead["state"], lead["vendor"], lead["slug"])
        if key in seen:
            return
        async with sem:
            record = dict(lead, status="review", validated_at=now.isoformat())
            if lead["vendor"] == "civicclerk":
                data = json.loads(
                    (args.out / "responses" / lead["response_file"]).read_text()
                )
                events = civicclerk_evidence(data, lead, now)
                if events:
                    adapter = AsyncCivicClerkAdapter(lead["slug"])
                    for event in events[:3]:
                        try:
                            items = await asyncio.wait_for(
                                adapter._fetch_meeting_items(
                                    event["agendaId"], event["id"]
                                ),
                                30,
                            )
                        except Exception as exc:
                            record["error"] = type(exc).__name__
                            continue
                        substantive = [
                            i for i in items if i.get("title") and i.get("attachments")
                        ]
                        if substantive:
                            record.update(
                                status="verified",
                                verification="structured_city_state_address_and_recent_municipal_agenda_items",
                                event_id=event["id"],
                                event_title=event["eventName"],
                                event_date=event["startDateTime"],
                                location=event["eventLocation"],
                                item_count=len(items),
                                items_with_attachments=len(substantive),
                                sample_titles=[i["title"] for i in substantive[:3]],
                            )
                            break
                if record["status"] != "verified":
                    record["reason"] = (
                        "No recent municipal agenda with exact city/state address and parsed items with attachments"
                    )
            elif lead["vendor"] == "civicplus":
                record.update(
                    await validate_civicplus(lead, args.out, document_session, now)
                )
            else:
                record["reason"] = (
                    "HTML portal needs official-source identity and adapter validation"
                )
            append_jsonl(args.out / "validation.jsonl", record)
            print(
                f"{record['status'].upper()} {lead['name']}, {lead['state']} {lead['vendor']}/{lead['slug']}",
                flush=True,
            )

    try:
        await asyncio.gather(*(one(r) for r in unique_leads(args.out)))
    finally:
        await document_session.close()
        await AsyncSessionManager.close_all()


def valid_verification(record):
    # A guessed URL or a self-asserted city/state pair is not proof of existence.
    if (
        record.get("status") != "verified"
        or record.get("http_status") != 200
        or not record.get("body_complete")
    ):
        return False
    if not recent_date(record.get("event_date"), datetime.now(timezone.utc)):
        return False
    if record["vendor"] == "civicclerk":
        return (
            record.get("verification")
            == "structured_city_state_address_and_recent_municipal_agenda_items"
            and address_matches(record.get("location"), record["name"], record["state"])
            and bool(record.get("items_with_attachments"))
        )
    if record["vendor"] == "civicplus":
        identity = record.get("identity", {})
        return (
            record.get("verification")
            == "civicplus_branded_address_recent_parsed_meeting_and_pdf"
            and bool(record.get("document_sha256"))
            and bool(
                html_identity(
                    "<title>"
                    + identity.get("title", "")
                    + "</title><footer>"
                    + identity.get("address_text", "")
                    + "</footer>",
                    record,
                )
            )
        )
    return False


def source_specs(value):
    if isinstance(value, str):
        value = json.loads(value)
    return sorted((e["vendor"], e["slug"]) for e in (value or []))


async def tenant_collision(conn, vendor, slug, banana):
    return await conn.fetchval(
        """SELECT banana FROM jurisdictions WHERE banana <> $3 AND (
        (vendor=$1 AND lower(slug)=lower($2)) OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(extra_vendors)='array'
                THEN extra_vendors ELSE '[]'::jsonb END) e
            WHERE e->>'vendor'=$1 AND lower(e->>'slug')=lower($2))) LIMIT 1""",
        vendor,
        slug,
        banana,
    )


def existing_action(record, existing, original, has_meetings):
    if (
        existing["banana"] != record["banana"]
        or existing["state"] != record["state"]
        or norm(existing["name"]) != norm(record["name"])
        or existing["type"] != "city"
        or existing["status"] != "active"
    ):
        return "identity_or_status_conflict"
    if (
        existing["vendor"] == record["vendor"]
        and existing["slug"] == record["slug"]
        and (
            not record.get("extra_vendors")
            or source_specs(record["extra_vendors"])
            == source_specs(existing.get("extra_vendors"))
        )
    ):
        return "already_configured"
    if has_meetings:
        return "protected_has_meetings"
    if not original or any(
        existing[k] != original[k] for k in ("vendor", "slug", "status")
    ):
        return "changed_since_snapshot"
    return "repair"


def select_sources(records, decisions):
    """No implicit Clerk preference when Plus is also a discovered candidate."""
    grouped = defaultdict(list)
    for record in records:
        grouped[record["banana"]].append(record)
    selected, held = [], []
    for banana, group in grouped.items():
        latest = {(r["vendor"], r["slug"]): r for r in group}
        verified = {key: r for key, r in latest.items() if r["status"] == "verified"}
        if not verified:
            continue
        decision = decisions.get(banana)
        dual = {"civicclerk", "civicplus"}.issubset({r["vendor"] for r in group})
        if not decision and (dual or len(verified) > 1):
            held.append(dict(banana=banana, reason="source_coverage_decision_required"))
            continue
        if decision and decision.get("action") == "hold":
            held.append(dict(banana=banana, reason=decision["reason"]))
            continue
        if decision:
            primary = decision["primary"]
            key = (primary["vendor"], primary["slug"])
            extra_specs = decision.get("extras", [])
            if key not in verified or any(
                (e["vendor"], e["slug"]) not in verified for e in extra_specs
            ):
                held.append(
                    dict(
                        banana=banana, reason="chosen_source_not_independently_verified"
                    )
                )
                continue
            extras = [verified[(e["vendor"], e["slug"])] for e in extra_specs]
            if extras and decision.get("relationship") != "disjoint_verified_bodies":
                held.append(
                    dict(
                        banana=banana,
                        reason="overlapping_sources_need_scoped_ingestion",
                    )
                )
                continue
            record = dict(
                verified[key],
                extra_vendors=extra_specs,
                extra_verifications=extras,
                source_decision=decision,
            )
        else:
            record = dict(next(iter(verified.values())))
        selected.append(record)
    return selected, held


async def apply(args):
    records = read_jsonl(args.out / "validation.jsonl")
    decisions_path = args.out / "source-decisions.json"
    decisions = (
        json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    )
    verified, held = select_sources(records, decisions)
    (args.out / "held-source-decisions.json").write_text(json.dumps(held, indent=2))
    conn = await asyncpg.connect(Config().get_postgres_dsn())
    counts = Counter()
    expected = {
        r["banana"]: r for r in json.loads((args.out / "registry.json").read_text())
    }
    selected = set()
    verified.sort(key=lambda r: (r["vendor"] != "civicclerk", r["name"], r["state"]))
    try:
        for record in verified:
            if record["banana"] in selected:
                continue
            selected.add(record["banana"])
            if not valid_verification(record) or any(
                not valid_verification(e) for e in record.get("extra_verifications", [])
            ):
                raise ValueError("Invalid/stale verification record")
            async with conn.transaction():
                # Serialize discovery writers and check live data immediately before mutation.
                if args.apply:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtext('engagic-vendor-discovery'))"
                    )
                existing = await conn.fetchrow(
                    "SELECT banana,name,state,type,status,vendor,slug,extra_vendors FROM jurisdictions WHERE banana=$1 OR (name=$2 AND state=$3) FOR UPDATE",
                    record["banana"],
                    record["name"],
                    record["state"],
                )
                collision = await tenant_collision(
                    conn, record["vendor"], record["slug"], record["banana"]
                )
                for extra in record.get("extra_vendors", []):
                    collision = collision or await tenant_collision(
                        conn, extra["vendor"], extra["slug"], record["banana"]
                    )
                if collision:
                    counts["tenant_collision"] += 1
                    continue
                if existing:
                    if (
                        record.get("extra_vendors")
                        and source_specs(existing.get("extra_vendors"))
                        and source_specs(record["extra_vendors"])
                        != source_specs(existing["extra_vendors"])
                    ):
                        counts["existing_extra_sources_require_review"] += 1
                        continue
                    has_meetings = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM meetings WHERE banana=$1)",
                        existing["banana"],
                    )
                    action = existing_action(
                        record, existing, expected.get(existing["banana"]), has_meetings
                    )
                    if action != "repair":
                        counts[action] += 1
                        continue
                else:
                    # Recheck normalized names too; SQL exact uniqueness doesn't catch punctuation/accents.
                    names = await conn.fetch(
                        "SELECT name FROM jurisdictions WHERE state=$1", record["state"]
                    )
                    if any(norm(r["name"]) == norm(record["name"]) for r in names):
                        counts["normalized_name_collision"] += 1
                        continue
                    action = "insert"
                audit = dict(
                    action=action,
                    banana=record["banana"],
                    name=record["name"],
                    state=record["state"],
                    vendor=record["vendor"],
                    slug=record["slug"],
                    before=dict(existing) if existing else None,
                    applied=args.apply,
                    at=datetime.now(timezone.utc).isoformat(),
                    event_id=record["event_id"],
                    extra_vendors=record.get("extra_vendors", []),
                    source_decision=record.get("source_decision"),
                )
                if args.apply:
                    if action == "insert":
                        await conn.execute(
                            "INSERT INTO jurisdictions (banana,name,state,vendor,slug,extra_vendors,type,status) VALUES ($1,$2,$3,$4,$5,$6::jsonb,'city','active')",
                            record["banana"],
                            record["name"],
                            record["state"],
                            record["vendor"],
                            record["slug"],
                            json.dumps(record["extra_vendors"])
                            if record.get("extra_vendors")
                            else None,
                        )
                    else:
                        await conn.execute(
                            "UPDATE jurisdictions SET vendor=$2,slug=$3,extra_vendors=COALESCE($4::jsonb,extra_vendors),updated_at=CURRENT_TIMESTAMP WHERE banana=$1",
                            existing["banana"],
                            record["vendor"],
                            record["slug"],
                            json.dumps(record["extra_vendors"])
                            if record.get("extra_vendors")
                            else None,
                        )
                counts[action] += 1
            append_jsonl(
                args.out / ("applied.jsonl" if args.apply else "proposed.jsonl"), audit
            )
            print(json.dumps(audit), flush=True)
    finally:
        await conn.close()
    print(
        f"Apply={args.apply}: {dict(counts)}; held source choices={len(held)}",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--seed", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--vendors", default=",".join(VENDORS))
    p.add_argument("--patterns", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=24)
    p.add_argument("--timeout", type=float, default=8)
    p.add_argument("--limit", type=int)
    p.add_argument(
        "--retry-failures",
        action="store_true",
        help="Retry timeouts, transient network errors, 429s and 5xx responses",
    )
    for name in ("validate", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--out", type=Path, required=True)
        if name == "apply":
            p.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        {"probe": probe, "validate": validate, "apply": apply}[args.command](args)
    )


if __name__ == "__main__":
    main()
