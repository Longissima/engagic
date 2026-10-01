"""Pulse: change-signal watcher that replaces re-syncing on a clock.

Vendors do not push, but most expose a signal far cheaper than a sync: a
Legistar delta query, an agenda RSS feed, an ETag, or a small JSON endpoint
whose digest moves when something is published. The watcher probes one such
signal per jurisdiction on a short cadence and runs the canonical sync only
for jurisdictions whose signal moved. The full sweep remains the safety net
and the backfill path; pulse only upgrades the leading edge.

Two loops share one process:
  probe loop  due signals on each scheduler tick, with adaptive intervals
  sync loop   drains jurisdiction_pulse.dirty_since through run_sync_cycle,
              one cycle at a time (the Fetcher is not reentrant)

Signals are resolved per jurisdiction and verified per jurisdiction: a
disabled feed, an empty feed, or an API that ignores $filter marks that one
jurisdiction unusable instead of teaching us anything about its vendor.

TODO: email-subscription ingress (Cloudflare Email Worker -> API) as a true
push signal to complement polling.
"""

import asyncio
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp

from config import config, get_logger
from database.db_postgres import Database
from database.models import Jurisdiction
from exceptions import VendorError
from pipeline.fetcher import SyncStatus
from vendors.factory import get_async_adapter
from vendors.rate_limiter_async import SLOTS, get_rate_limiter
from vendors.session_manager_async import AsyncSessionManager

logger = get_logger(__name__).bind(component="pulse")

# Adaptive cadence (confidence 7/10 on the numbers; tune from changed_at data).
# Every unchanged probe stretches a jurisdiction's interval by one step, up to
# the ceiling; any change snaps it back to the floor. Busy councils stay near
# 10 minutes, a board that publishes twice a month settles at an hour.
PROBE_FLOOR_SECONDS = 600
PROBE_CEILING_SECONDS = 3600
PROBE_STEP_SECONDS = 300
PROBE_JITTER = 0.1
# Feeds that are not time-critical (Granicus minutes) are fetched at most this
# often, whatever the jurisdiction's cadence.
SLOW_URL_SECONDS = 3600
# A missing signal is re-checked on this cadence so a feed a city turns on
# later is still discovered; transient failures back off exponentially to it.
MISSING_RECHECK_SECONDS = 6 * 3600
SCHEDULER_TICK_SECONDS = 30
ROSTER_REFRESH_SECONDS = 900
SUMMARY_LOG_SECONDS = 600
SYNC_POLL_SECONDS = 20
TARGETED_MAX_SPAN_DAYS = 56
# A dirty jurisdiction whose sync keeps failing (an adapter bug, a dead
# portal) is retried on an exponential schedule instead of every cycle; it
# stays dirty, so a fix or restart picks it up. In memory on purpose: a
# restart is exactly when a retry should happen.
SYNC_RETRY_FLOOR_SECONDS = 600
SYNC_RETRY_CEILING_SECONDS = 6 * 3600
LEGISTAR_DELTA_PAGE = 200
# CivicClerk: agendas post days-to-weeks before a meeting, so a window from
# just behind today to 45 days out sees every publication that matters; the
# API pages at 15 events, and the page cap bounds a runaway tenant.
CIVICCLERK_WINDOW_BACK_DAYS = 2
CIVICCLERK_WINDOW_FORWARD_DAYS = 45
CIVICCLERK_MAX_PAGES = 6
MUNICODE_SHARED_HOST = "https://meetings.municode.com"


class SignalMissing(Exception):
    """This jurisdiction does not expose a usable signal (not transient)."""


class ProbeFailed(Exception):
    """Transient probe failure; the signal may work on the next attempt."""


@dataclass(frozen=True)
class Probe:
    banana: str
    vendor: str
    signal: str  # legistar_delta | civicclerk_activity | rows | civicplus | feed
    urls: Tuple[str, ...]
    params: Tuple[Tuple[str, str], ...] = ()
    slow_urls: Tuple[str, ...] = ()


def _granicus_probe(city: Jurisdiction, adapter: Any) -> Probe:
    feeds = {
        mode: tuple(
            f"{adapter.base_url}/ViewPublisherRSS.php?view_id={view['view_id']}&mode={mode}"
            for view in adapter.views
        )
        for mode in ("agendas", "minutes")
    }
    return Probe(city.banana, city.vendor, "feed", feeds["agendas"] + feeds["minutes"],
                 slow_urls=feeds["minutes"])


def _civicplus_probe(city: Jurisdiction, adapter: Any) -> Probe:
    slug = city.slug
    base_url = adapter.base_url or (
        f"https://{slug}" if "." in slug else f"https://{slug}.civicplus.com"
    )
    return Probe(city.banana, city.vendor, "civicplus",
                 (f"{base_url}/RSSFeed.aspx?ModID=65&CID=All-0", f"{base_url}/AgendaCenter/Search/"))


def _municode_probe(city: Jurisdiction, adapter: Any) -> Optional[Probe]:
    # PublishPage and Drupal modes scrape the shared host; only the
    # per-tenant REST API has a small listing worth watching.
    if adapter.base_url == MUNICODE_SHARED_HOST:
        return None
    return Probe(city.banana, city.vendor, "rows", (f"{adapter.base_url}/api/v1/public/meeting/list.json",))


def _primegov_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "rows",
                 (f"{adapter.base_url}/api/v2/PublicPortal/ListUpcomingMeetings",))


def _escribe_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "rows",
                 (f"{adapter.base_url}/MeetingsCalendarView.aspx/GetCalendarMeetings",))


def _civicweb_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "rows",
                 (f"{adapter.base_url}/Services/MeetingsService.svc/meetings",))


def _destiny_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "rows", (f"{adapter.base_url}/agenda_publish.cfm",),
                 (("id", str(adapter.site_id)),))


def _boardbook_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "rows", (f"{adapter.base_url}/Search/AjaxSearch/{adapter.org_id}",))


def _iqm2_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "feed", (f"{adapter.base_url}/Services/RSS.aspx?Feed=Calendar",))


def _civicclerk_probe(city: Jurisdiction, adapter: Any) -> Probe:
    return Probe(city.banana, city.vendor, "civicclerk_activity", (f"{adapter.base_url}/v1/Events",))


ADAPTER_PROBES = {
    "civicclerk": _civicclerk_probe,
    "escribe": _escribe_probe,
    "boardbook": _boardbook_probe,
    "civicweb": _civicweb_probe,
    "destiny": _destiny_probe,
    "iqm2": _iqm2_probe,
    "granicus": _granicus_probe,
    "civicplus": _civicplus_probe,
    "municode": _municode_probe,
    "primegov": _primegov_probe,
}
# BoardBook stays on the regular sweep: its search index flips between two
# result sets for the same district (2026-09-30: 94% of its changes found
# nothing new), so every probe resynced. _boardbook_probe is kept for a
# future signal that is stable.
PULSE_VENDORS = frozenset({"legistar", *ADAPTER_PROBES} - {"boardbook"})


def resolve_probe(city: Jurisdiction) -> Optional[Probe]:
    """Map a jurisdiction to its cheapest change signal, or None if unsupported.

    Adapters are constructed only to reuse their site-config resolution
    (Granicus view ids, CivicPlus domain overrides, Municode hosting mode);
    no request is made here.
    """
    vendor, slug = city.vendor, city.slug
    if vendor not in PULSE_VENDORS or not slug:
        return None

    if vendor == "legistar":
        params: Tuple[Tuple[str, str], ...] = ()
        if slug == "nyc" and config.NYC_LEGISTAR_TOKEN:
            params = (("token", config.NYC_LEGISTAR_TOKEN),)
        return Probe(city.banana, vendor, "legistar_delta",
                     (f"https://webapi.legistar.com/v1/{slug}/events",), params)

    try:
        adapter = get_async_adapter(vendor, slug)
    except (VendorError, ValueError) as exc:
        logger.debug("pulse probe unresolved", banana=city.banana, vendor=vendor, error=str(exc))
        return None
    return ADAPTER_PROBES[vendor](city, adapter)


# --- signal readers ---------------------------------------------------------

_FEED_ITEM = re.compile(r"<(item|entry)\b.*?</\1>", re.S | re.I)
_FEED_FIELD = re.compile(r"<(guid|id|link|pubDate|updated|title)\b[^>]*>(.*?)</\1>", re.S | re.I)


def extract_feed_keys(body: str) -> Optional[List[str]]:
    """Return one identity key per feed entry, or None if body is not a feed.

    The key includes pubDate so a republished agenda (same guid, new
    timestamp) reads as a change. Regex rather than an XML parser because
    vendor feeds are not reliably well-formed.
    """
    head = body[:2000].lower()
    if "<title>rss feed</title>" in head:
        return _iqm2_rendered_feed_keys(body)
    if "<rss" not in head and "<feed" not in head and "<rdf:rdf" not in head:
        return None
    keys = []
    for match in _FEED_ITEM.finditer(body):
        fields = sorted(f"{name.lower()}={value.strip()}" for name, value in _FEED_FIELD.findall(match.group(0)))
        keys.append("|".join(fields))
    return keys


_IQM2_ENTRY = re.compile(r"<h2>(.*?)</h2>(.*?)(?=<h2>|</body>|$)", re.S | re.I)
_HREF = re.compile(r"""href=['"]([^'"]+)['"]""", re.I)


def _iqm2_rendered_feed_keys(body: str) -> List[str]:
    """IQM2 serves its calendar feed as an HTML rendering whatever the Accept
    header: one <h2>Body - Agenda - Sep 29, 2026 6:00 PM</h2> per entry,
    followed by its document links. Key = heading + links, so a newly
    attached packet or minutes file reads as a new entry."""
    return [
        "|".join([heading.strip(), *sorted(_HREF.findall(block))])
        for heading, block in _IQM2_ENTRY.findall(body)
    ]


def parse_json_body(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SignalMissing(f"endpoint did not return JSON: {body[:80]!r}") from exc


def parse_legistar_time(value: str) -> datetime:
    # Legistar emits variable fractional precision ("...:11.46", "...:11.457").
    return datetime.fromisoformat(value.rstrip("Z"))


async def _get(
    vendor: str,
    url: str,
    *,
    params: Any = None,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    allow_redirects: bool = True,
) -> Tuple[int, Dict[str, str], str]:
    """GET, or POST when json_body is given (eScribe's calendar is a POST).
    params may be a list of pairs for repeated keys."""
    session = await AsyncSessionManager.get_session(vendor)
    await get_rate_limiter().wait_if_needed(vendor)
    method = "POST" if json_body is not None else "GET"
    try:
        async with session.request(method, url, params=params, headers=headers, json=json_body,
                                   allow_redirects=allow_redirects) as resp:
            body = "" if resp.status == 304 else await resp.text(errors="replace")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except (aiohttp.ClientConnectorDNSError, aiohttp.ClientConnectorCertificateError) as exc:
        # A host that does not resolve or presents someone else's certificate
        # is a misconfigured slug, not an outage.
        raise SignalMissing(f"{type(exc).__name__}: {exc}") from exc
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise ProbeFailed(f"{type(exc).__name__}: {exc}") from exc


def _raise_for_status(status: int, url: str) -> None:
    if status in (400, 401, 403, 404, 410):
        raise SignalMissing(f"HTTP {status} from {url}")
    if status >= 400:
        raise ProbeFailed(f"HTTP {status} from {url}")


@dataclass(frozen=True)
class Reading:
    """One probe's verdict. hint_dates names the meeting dates a change
    touched; None on a change means it could not be localized."""

    state: Dict[str, Any]
    changed: bool = False
    evidence: Tuple[Any, ...] = ()
    hint_dates: Optional[Tuple[date, ...]] = None


_TITLE_DATE = re.compile(r"([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})")


def parse_title_date(text: str) -> Optional[date]:
    """Meeting date from a feed title like "Council - Sep 30, 2026"."""
    for month, day, year in _TITLE_DATE.findall(text):
        try:
            return datetime.strptime(f"{month[:3]} {day} {year}", "%b %d %Y").date()
        except ValueError:
            continue
    return None


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def read_legistar_delta(probe: Probe, state: Dict[str, Any]) -> Reading:
    """Ask Legistar for events modified after our watermark.

    A first probe only establishes the watermark. An API that returns rows at
    or before the watermark is ignoring $filter (seen on Nashville-style
    tenants) and cannot serve as a signal. EventDate on each changed row is
    the targeted-sync hint.
    """
    url = probe.urls[0]
    headers = {"Accept": "application/json"}
    watermark = state.get("watermark")
    params = dict(probe.params)
    params["$select"] = "EventId,EventDate,EventLastModifiedUtc"
    if watermark is None:
        params.update({"$top": "1", "$orderby": "EventLastModifiedUtc desc"})
    else:
        # Ascending, so a truncated page advances the watermark only past
        # rows we actually saw; the next probe picks up the remainder.
        params.update({
            "$top": str(LEGISTAR_DELTA_PAGE),
            "$orderby": "EventLastModifiedUtc asc",
            "$filter": f"EventLastModifiedUtc gt datetime'{watermark}'",
        })

    status, _, body = await _get(probe.vendor, url, params=params, headers=headers)
    _raise_for_status(status, url)
    try:
        rows = json.loads(body)
    except ValueError as exc:
        raise SignalMissing(f"legistar API did not return JSON: {body[:80]!r}") from exc
    if not isinstance(rows, list):
        raise SignalMissing("legistar API returned a non-list payload")
    rows = [row for row in rows if isinstance(row, dict) and row.get("EventLastModifiedUtc")]

    if watermark is None:
        if not rows:
            raise SignalMissing("legistar API returned no events")
        return Reading({"watermark": rows[0]["EventLastModifiedUtc"]})

    floor = parse_legistar_time(watermark)
    # One second of slack: the server compares at higher precision than it serializes.
    stale = [row for row in rows if parse_legistar_time(row["EventLastModifiedUtc"]) < floor - timedelta(seconds=1)]
    if stale:
        raise SignalMissing("legistar API ignores $filter on EventLastModifiedUtc")
    newer = [row for row in rows if parse_legistar_time(row["EventLastModifiedUtc"]) > floor]
    if not newer:
        return Reading(state)
    latest = max(newer, key=lambda row: parse_legistar_time(row["EventLastModifiedUtc"]))
    dates = [parse_legistar_time(row["EventDate"]).date() for row in newer if row.get("EventDate")]
    # A full page may have truncated the delta; only a complete one localizes.
    complete = len(dates) == len(newer) and len(rows) < LEGISTAR_DELTA_PAGE
    return Reading(
        {"watermark": latest["EventLastModifiedUtc"]},
        changed=True,
        evidence=tuple(row.get("EventId") for row in newer),
        hint_dates=tuple(sorted(set(dates))) if complete else None,
    )


def _feed_entries(url: str, body: str) -> Tuple[str, List[str], List[str]]:
    """(digest, entry key hashes, raw keys) for one fetched feed."""
    keys = extract_feed_keys(body)
    if keys is None:
        raise SignalMissing(f"not a feed: {url}")
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest(), [_key_hash(k) for k in keys], keys


async def read_feeds(probe: Probe, state: Dict[str, Any]) -> Reading:
    """Digest each feed URL by its entry keys.

    Conditional GETs are sent whenever the server gave us validators; a 304
    carries the previous state forward. A URL seen for the first time is a
    baseline, never a change. For feeds, only entries not seen before count
    as a change (an old entry aging out of the feed is not news), and their
    titles supply targeted-sync dates when every new entry carries one.
    """
    new_state: Dict[str, Any] = {}
    changed_urls: List[str] = []
    new_entry_dates: List[Optional[date]] = []
    total_entries = 0
    params = dict(probe.params) or None
    now = time.time()
    for url in probe.urls:
        prev = state.get(url) or {}
        if url in probe.slow_urls and now - prev.get("fetched_at", 0) < SLOW_URL_SECONDS:
            new_state[url] = prev
            total_entries += prev.get("entries", 0)
            continue
        headers = {}
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]

        status, resp_headers, body = await _get(probe.vendor, url, params=params, headers=headers)
        if status == 304 and prev:
            new_state[url] = {**prev, "fetched_at": now}
            total_entries += prev.get("entries", 0)
            continue
        _raise_for_status(status, url)

        digest, key_hashes, keys = _feed_entries(url, body)
        entries = len(keys)
        new_state[url] = {
            "digest": digest,
            "entries": entries,
            "keys": key_hashes,
            "etag": resp_headers.get("etag"),
            "last_modified": resp_headers.get("last-modified"),
            "fetched_at": now,
        }
        total_entries += entries
        if not prev.get("digest") or prev["digest"] == digest:
            continue
        if "keys" not in prev:
            changed_urls.append(url)
            new_entry_dates.append(None)
            continue
        seen = set(prev["keys"])
        fresh = [key for key, key_hash in zip(keys, key_hashes) if key_hash not in seen]
        if fresh:
            changed_urls.append(url)
            new_entry_dates.extend(parse_title_date(key) for key in fresh)

    if total_entries == 0:
        raise SignalMissing("every feed for this jurisdiction is empty")
    if not changed_urls:
        return Reading(new_state)
    localized = bool(new_entry_dates) and all(new_entry_dates)
    return Reading(
        new_state,
        changed=True,
        evidence=tuple(changed_urls),
        hint_dates=tuple(sorted({d for d in new_entry_dates if d})) if localized else None,
    )


def civicclerk_activity(event: Dict[str, Any]) -> Optional[str]:
    """Latest publication moment on a CivicClerk event: its creation, its
    public release, or any file published to it (agenda, packet, minutes).
    ISO-8601 UTC strings compare correctly as text."""
    stamps = [event.get("createdOn"), event.get("publishStart")]
    stamps += [f.get("publishOn") for f in event.get("publishedFiles") or [] if isinstance(f, dict)]
    valid = [s for s in stamps if isinstance(s, str) and s[:4].isdigit() and not s.startswith("0001")]
    return max(valid) if valid else None


async def _fetch_civicclerk_window(probe: Probe) -> List[Dict[str, Any]]:
    today = date.today()
    start = today - timedelta(days=CIVICCLERK_WINDOW_BACK_DAYS)
    end = today + timedelta(days=CIVICCLERK_WINDOW_FORWARD_DAYS)
    params: Optional[Dict[str, str]] = {
        "$filter": f"startDateTime ge {start.isoformat()}T00:00:00.000Z and startDateTime lt {end.isoformat()}T00:00:00.000Z",
        "$orderby": "startDateTime asc, id asc",
    }
    url: Optional[str] = probe.urls[0]
    events: List[Dict[str, Any]] = []
    for _ in range(CIVICCLERK_MAX_PAGES):
        if url is None:
            break
        status, _, body = await _get(probe.vendor, url, params=params)
        _raise_for_status(status, url)
        try:
            page = json.loads(body)
        except ValueError as exc:
            raise SignalMissing(f"civicclerk API did not return JSON: {body[:80]!r}") from exc
        if not isinstance(page, dict) or not isinstance(page.get("value"), list):
            raise SignalMissing("civicclerk events response has no value list")
        events.extend(event for event in page["value"] if isinstance(event, dict))
        next_link = page.get("@odata.nextLink")
        next_url = urljoin(url, next_link) if isinstance(next_link, str) else None
        # Never follow a nextLink off the tenant's API host.
        url = next_url if next_url and urlparse(next_url).netloc == urlparse(probe.urls[0]).netloc else None
        params = None
    return events


async def read_civicclerk_activity(probe: Probe, state: Dict[str, Any]) -> Reading:
    """Watermark over publication activity in the upcoming-events window.

    CivicClerk's OData has no modified-date field and rejects $select, so we
    read the (gzip'd, ~4-10KB/page) window and reduce each event to its latest
    publication moment. Events sliding into or out of the window never read
    as changes; only activity newer than the watermark does. Blind spot: an
    agenda edited in place without a new publish timestamp (confidence 7/10
    that this is rare; the sweep covers it).
    """
    events = await _fetch_civicclerk_window(probe)
    stamps = [stamp for stamp in map(civicclerk_activity, events) if stamp]
    watermark = state.get("watermark")
    if watermark is None:
        # An empty window still establishes a baseline: the tenant answers,
        # it just has nothing scheduled yet.
        return Reading({"watermark": max(stamps) if stamps else "0000"})
    newer = [event for event in events if (civicclerk_activity(event) or "") > watermark]
    if not newer:
        return Reading(state)
    dates = [
        datetime.fromisoformat(event["startDateTime"].replace("Z", "+00:00")).date()
        for event in newer if isinstance(event.get("startDateTime"), str)
    ]
    return Reading(
        {"watermark": max(stamps)},
        changed=True,
        evidence=tuple(event.get("id") for event in newer),
        hint_dates=tuple(sorted(set(dates))) if len(dates) == len(newer) else None,
    )


# Rows: listing endpoints that return one JSON object per meeting. Each row is
# reduced to (id, meeting date, fingerprint); a row that is new or whose
# fingerprint moved is a change, and its date is the targeted-sync hint.
# Rows leaving the window are forgotten, never reported.
Row = Tuple[str, Optional[date], str]
ROWS_WINDOW_BACK_DAYS = 2
ROWS_WINDOW_FORWARD_DAYS = 45
# Fields that move around a meeting's broadcast, not its documents.
PRIMEGOV_VOLATILE = frozenset({
    "isShowVideoIcon", "streamCompleted", "mediaManagerClipPubliclyAvailable",
    "videoUrl", "swagitId", "isMediaManagerVideo", "meetingState",
})


def _fingerprint(value: Any) -> str:
    return _key_hash(json.dumps(value, sort_keys=True, default=str))


def _row_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10].replace("/", "-"))
    except ValueError:
        return None


def escribe_rows(payload: Any) -> List[Row]:
    rows = payload.get("d") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SignalMissing("eScribe calendar response lacks a meeting array")
    return [(str(r["ID"]), _row_date(r.get("StartDate")), _fingerprint(r))
            for r in rows if isinstance(r, dict) and r.get("ID")]


def primegov_rows(payload: Any) -> List[Row]:
    if not isinstance(payload, list):
        raise SignalMissing("PrimeGov upcoming meetings is not a list")
    return [(str(r["id"]), _row_date(r.get("dateTime")),
             _fingerprint({k: v for k, v in r.items() if k not in PRIMEGOV_VOLATILE}))
            for r in payload if isinstance(r, dict) and r.get("id") is not None]


def municode_rows(payload: Any) -> List[Row]:
    rows = payload.get("Meetings") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SignalMissing("Municode meeting list lacks Meetings")
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("MeetingID"):
            continue
        calendar = r.get("CalendarDate") or [{}]
        first = calendar[0] if isinstance(calendar, list) and calendar and isinstance(calendar[0], dict) else {}
        out.append((str(r["MeetingID"]), _row_date(first.get("FromDate")),
                    _fingerprint([r.get("RevisionID"), r.get("Changed"), r.get("Visibility")])))
    return out


def civicweb_rows(payload: Any) -> List[Row]:
    """CivicWeb's meetings service carries no document list; the Published
    flag flipping is the agenda-posted event (confidence 6/10 for catching
    documents added after publication; the sweep covers those)."""
    if not isinstance(payload, list):
        raise SignalMissing("CivicWeb meetings service did not return a list")
    return [(str(r["Id"]), _row_date(r.get("MeetingDate")),
             _fingerprint([r.get("Published"), r.get("Name"), r.get("MeetingDateTime")]))
            for r in payload if isinstance(r, dict) and r.get("Id") is not None]


_DESTINY_ROW = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_DESTINY_SEQ = re.compile(r"seq(?:=|&#x3d;)(\d+)", re.I)
_DESTINY_DATE = re.compile(r"\((\d{2})/(\d{2})/(\d{4})\)")
# A meeting near a month boundary is listed on both month pages, and each
# copy's links carry that page's get_month/get_year; strip them so both copies
# fingerprint alike and a window rolling into a new month is not a change.
_DESTINY_PAGE_PARAMS = re.compile(r"(?:&amp;|&|\?|&#x3f;)get_(?:month|year)(?:=|&#x3d;)\d+", re.I)


def destiny_rows(html: str) -> List[Row]:
    """Rows of the month listings (modern meeting-table or legacy list).
    The agenda link carries seq=<meeting id> and "(MM/DD/YYYY)" in its title."""
    rows = []
    for block in _DESTINY_ROW.findall(html):
        seq = _DESTINY_SEQ.search(block)
        if not seq:
            continue
        found = _DESTINY_DATE.search(block)
        day = None
        if found:
            month, dom, year = map(int, found.groups())
            try:
                day = date(year, month, dom)
            except ValueError:
                day = None
        normalized = re.sub(r"\s+", " ", _DESTINY_PAGE_PARAMS.sub("", block))
        rows.append((seq.group(1), day, _fingerprint(normalized)))
    return rows


_BOARDBOOK_RESULT = re.compile(r"<li>(.*?)</li>", re.S | re.I)
_BOARDBOOK_TARGET = re.compile(r"GoToResult/([\w-]+)\?Index=(\w+)")
_BOARDBOOK_LINK_TEXT = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S | re.I)
_MONTH_DAY_YEAR = re.compile(r"([A-Z][a-z]{2,8})\.? (\d{1,2}),? (\d{4})")


def boardbook_rows(html: str) -> List[Row]:
    """One row per search hit (meeting, agenda item, or document). Meeting
    and agenda-item titles carry the meeting date ("September 25, 2026",
    "Oct 1 2026"); documents do not, so a new document alone cannot be
    localized."""
    if "Search Results" not in html:
        raise SignalMissing("BoardBook search returned no results page")
    rows = []
    for block in _BOARDBOOK_RESULT.findall(html):
        target = _BOARDBOOK_TARGET.search(block)
        link = _BOARDBOOK_LINK_TEXT.search(block)
        if not target or not link:
            continue
        text = re.sub(r"<[^>]+>|\s+", " ", link.group(1)).strip()
        day = None
        for month, dom, year in _MONTH_DAY_YEAR.findall(text):
            try:
                day = datetime.strptime(f"{month[:3]} {dom} {year}", "%b %d %Y").date()
                break
            except ValueError:
                continue
        rows.append((f"{target.group(2)}:{target.group(1)}", day, _fingerprint(text)))
    return rows


ROW_EXTRACTORS = {
    "boardbook": boardbook_rows,
    "escribe": escribe_rows,
    "primegov": primegov_rows,
    "municode": municode_rows,
    "civicweb": civicweb_rows,
    "destiny": destiny_rows,
}


async def _fetch_destiny_months(probe: Probe, start: date, end: date) -> str:
    """Destiny lists by calendar month; fetch each month the window touches."""
    url, pages = probe.urls[0], []
    month = start.replace(day=1)
    while month <= end:
        params = {**dict(probe.params), "mt": "ALL", "get_month": str(month.month), "get_year": str(month.year)}
        status, _, body = await _get(probe.vendor, url, params=params)
        _raise_for_status(status, url)
        pages.append(body)
        month = (month + timedelta(days=32)).replace(day=1)
    return "\n".join(pages)


async def _fetch_rows_payload(probe: Probe) -> Any:
    url = probe.urls[0]
    today = date.today()
    start = today - timedelta(days=ROWS_WINDOW_BACK_DAYS)
    end = today + timedelta(days=ROWS_WINDOW_FORWARD_DAYS)
    if probe.vendor == "destiny":
        return await _fetch_destiny_months(probe, start, end)
    if probe.vendor == "boardbook":
        # Search is a per-district opt-in; a disabled one redirects to the
        # 259KB district page, which must never be followed from a probe.
        params = [("q", "*"), ("from", start.strftime("%m/%d/%Y")), ("to", end.strftime("%m/%d/%Y")),
                  ("i", "4"), ("i", "8"), ("i", "9")]
        status, _, body = await _get(probe.vendor, url, params=params, allow_redirects=False,
                                     headers={"X-Requested-With": "XMLHttpRequest"})
        if 300 <= status < 400:
            raise SignalMissing("BoardBook search is disabled for this district")
        _raise_for_status(status, url)
        return body
    if probe.vendor == "escribe":
        window = {"calendarStartDate": start.isoformat(), "calendarEndDate": end.isoformat()}
        status, _, body = await _get(probe.vendor, url, json_body=window,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
    else:
        params = {
            "civicweb": {"from": start.isoformat(), "to": end.isoformat()},
            "municode": {"datefrom": start.isoformat(), "dateto": end.isoformat()},
        }.get(probe.vendor, {})
        status, _, body = await _get(probe.vendor, url, params={**dict(probe.params), **params} or None)
    _raise_for_status(status, url)
    return parse_json_body(body)


def diff_rows(previous: Optional[Dict[str, Any]], rows: List[Row]) -> Reading:
    # A listing can repeat one id (Destiny month pages overlap); fold every
    # copy into one order-independent fingerprint so the stored state and the
    # comparison see the same thing. Comparing copies one by one against a
    # last-copy-wins state reported a change on every probe.
    copies: Dict[str, List[str]] = {}
    days: Dict[str, Optional[date]] = {}
    for row_id, day, fingerprint in rows:
        copies.setdefault(row_id, []).append(fingerprint)
        days[row_id] = days.get(row_id) or day
    current = {
        row_id: [distinct[0] if len(distinct := sorted(set(prints))) == 1 else _fingerprint(distinct),
                 days[row_id].isoformat() if days[row_id] else None]
        for row_id, prints in copies.items()
    }
    if previous is None:
        return Reading({"rows": current})
    moved = [(row_id, days[row_id]) for row_id, (fingerprint, _) in current.items()
             if (previous.get(row_id) or [None])[0] != fingerprint]
    if not moved:
        return Reading({"rows": current})
    days = [day for _, day in moved]
    return Reading(
        {"rows": current},
        changed=True,
        evidence=tuple(row_id for row_id, _ in moved),
        hint_dates=tuple(sorted(set(days))) if all(days) else None,
    )


async def read_rows(probe: Probe, state: Dict[str, Any]) -> Reading:
    rows = ROW_EXTRACTORS[probe.vendor](await _fetch_rows_payload(probe))
    return diff_rows(state.get("rows"), rows)


_CIVICPLUS_ROW = re.compile(r"<tr[^>]*catAgendaRow[^>]*>(.*?)</tr>", re.S | re.I)
_CIVICPLUS_ROW_ID = re.compile(r'id="_(\d{2})(\d{2})(\d{4})-(\d+)"')
_CIVICPLUS_HEADING = re.compile(r"<h3\b.*?</h3>", re.S | re.I)


def civicplus_search_rows(html: str) -> List[Row]:
    """One row per AgendaCenter search result. The anchor id encodes the
    meeting date and agenda id (_MMDDYYYY-ID); the heading carries the
    "Posted ..." stamp and the links carry every file version, so a repost,
    a packet, or minutes landing all move the fingerprint."""
    if "AgendaCenter" not in html:
        raise SignalMissing("AgendaCenter search returned no AgendaCenter page")
    rows = []
    for block in _CIVICPLUS_ROW.findall(html):
        anchor = _CIVICPLUS_ROW_ID.search(block)
        if not anchor:
            continue
        month, day, year, agenda_id = anchor.groups()
        try:
            meeting_day: Optional[date] = date(int(year), int(month), int(day))
        except ValueError:
            meeting_day = None
        heading = _CIVICPLUS_HEADING.search(block)
        content = [re.sub(r"\s+", " ", heading.group(0)) if heading else "", *sorted(set(_HREF.findall(block)))]
        rows.append((agenda_id, meeting_day, _fingerprint(content)))
    return rows


async def read_civicplus(probe: Probe, state: Dict[str, Any]) -> Reading:
    """AgendaCenter RSS when the tenant publishes it (3KB), else the
    AgendaCenter date-range search (~30KB gzip'd, every category in one
    request). Some tenants serve an empty All-0 feed and empty category
    feeds; for them the search is the only single-request signal. The mode
    is remembered so a search tenant does not pay the dead RSS request."""
    feed_url, search_url = probe.urls
    if state.get("mode") != "search":
        try:
            reading = await read_feeds(Probe(probe.banana, probe.vendor, "feed", (feed_url,)), state)
            return Reading({**reading.state, "mode": "rss"}, reading.changed, reading.evidence, reading.hint_dates)
        except SignalMissing as exc:
            if "Cannot connect" in str(exc) or "ClientConnector" in str(exc):
                raise
    today = date.today()
    params = {
        "term": "", "CIDs": "all", "dateRange": "", "dateSelector": "",
        "startDate": (today - timedelta(days=ROWS_WINDOW_BACK_DAYS)).strftime("%m/%d/%Y"),
        "endDate": (today + timedelta(days=ROWS_WINDOW_FORWARD_DAYS)).strftime("%m/%d/%Y"),
    }
    status, _, body = await _get(probe.vendor, search_url, params=params)
    _raise_for_status(status, search_url)
    reading = diff_rows(state.get("rows") if state.get("mode") == "search" else None,
                        civicplus_search_rows(body))
    return Reading({**reading.state, "mode": "search"}, reading.changed, reading.evidence, reading.hint_dates)


async def read_signal(probe: Probe, state: Dict[str, Any]) -> Reading:
    if probe.signal == "legistar_delta":
        return await read_legistar_delta(probe, state)
    if probe.signal == "civicclerk_activity":
        return await read_civicclerk_activity(probe, state)
    if probe.signal == "rows":
        return await read_rows(probe, state)
    if probe.signal == "civicplus":
        return await read_civicplus(probe, state)
    return await read_feeds(probe, state)


# --- watcher ----------------------------------------------------------------

def _jittered(seconds: float) -> float:
    return seconds * random.uniform(1 - PROBE_JITTER, 1 + PROBE_JITTER)


def quiet_interval(quiet_streak: int) -> float:
    """Seconds until the next probe after `quiet_streak` unchanged probes."""
    return min(PROBE_CEILING_SECONDS, PROBE_FLOOR_SECONDS + PROBE_STEP_SECONDS * quiet_streak)


def failure_interval(consecutive_failures: int, *, unusable: bool) -> float:
    if unusable:
        return MISSING_RECHECK_SECONDS
    return min(MISSING_RECHECK_SECONDS, PROBE_FLOOR_SECONDS * 2 ** max(0, consecutive_failures - 1))


def is_due(row: Optional[Dict[str, Any]], now: datetime) -> bool:
    return not row or row.get("next_probe_at") is None or row["next_probe_at"] <= now


def targeted_range(dates: List[date]) -> Optional[Tuple[datetime, datetime]]:
    """[start, end) midnights covering the hinted meeting dates, padded a day
    each side for UTC-vs-local date skew; None when there is nothing to
    localize or the span is wide enough that the default window is cheaper."""
    if not dates:
        return None
    start = min(dates) - timedelta(days=1)
    end = max(dates) + timedelta(days=2)
    if (end - start).days > TARGETED_MAX_SPAN_DAYS:
        return None
    return datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())


class PulseWatcher:
    """Per-jurisdiction probe scheduler + sync loop over jurisdiction_pulse.

    The scheduler is continuous, not cycle-based: every tick it launches the
    probes whose next_probe_at has passed and are not already in flight, so a
    slow vendor lane never delays a fast one.
    """

    def __init__(
        self,
        db: Database,
        conductor: Any,
        *,
        sync_enabled: bool = True,
        vendors: Optional[frozenset] = None,
    ):
        self.db = db
        self.conductor = conductor
        self.sync_enabled = sync_enabled
        self.vendors = vendors or PULSE_VENDORS
        self.shutdown = asyncio.Event()
        # One lane per vendor, sized to its rate-limiter slots, so a slow
        # city-hosted vendor (CivicPlus) never starves the SaaS APIs.
        self.lanes = {vendor: asyncio.Semaphore(SLOTS.get(vendor, 1)) for vendor in self.vendors}
        self.in_flight: Dict[str, asyncio.Task] = {}
        self.counts: Dict[str, int] = {}
        self.sync_failures: Dict[str, Tuple[int, float]] = {}  # banana -> (failures, retry at, monotonic)
        self._roster: List[Probe] = []
        self._roster_built_at = 0.0

    async def roster(self) -> List[Probe]:
        if time.monotonic() - self._roster_built_at > ROSTER_REFRESH_SECONDS or not self._roster:
            cities = await self.db.jurisdictions.get_all_cities(status="active")
            self._roster = [
                probe for city in cities
                if city.vendor in self.vendors and (probe := resolve_probe(city))
            ]
            self._roster_built_at = time.monotonic()
        return self._roster

    async def probe_one(self, probe: Probe, row: Optional[Dict[str, Any]]) -> str:
        row = row or {}
        state = row.get("state") or {}
        if row.get("signal") != probe.signal:
            state = {}
        try:
            reading = await read_signal(probe, state)
        except SignalMissing as exc:
            await self.db.pulse.record_failure(
                probe.banana, probe.signal, str(exc), unusable=True,
                next_in_seconds=_jittered(failure_interval(0, unusable=True)),
            )
            logger.info("pulse signal missing", banana=probe.banana, vendor=probe.vendor, reason=str(exc))
            return "missing"
        except ProbeFailed as exc:
            failures = row.get("consecutive_failures", 0) + 1
            await self.db.pulse.record_failure(
                probe.banana, probe.signal, str(exc), unusable=False,
                next_in_seconds=_jittered(failure_interval(failures, unusable=False)),
            )
            logger.warning("pulse probe failed", banana=probe.banana, vendor=probe.vendor,
                           failures=failures, error=str(exc))
            return "failed"

        quiet_streak = 0 if reading.changed else row.get("quiet_streak", 0) + 1
        await self.db.pulse.record_success(
            probe.banana, probe.signal, reading.state, changed=reading.changed,
            quiet_streak=quiet_streak, next_in_seconds=_jittered(quiet_interval(quiet_streak)),
            hint_dates=list(reading.hint_dates) if reading.hint_dates is not None else None,
        )
        if reading.changed:
            logger.info("pulse change", banana=probe.banana, vendor=probe.vendor,
                        evidence=list(reading.evidence[:10]),
                        hint_dates=[d.isoformat() for d in reading.hint_dates or ()])
            return "changed"
        return "baseline" if not state else "unchanged"

    async def _run_probe(self, probe: Probe, row: Optional[Dict[str, Any]]) -> str:
        async with self.lanes[probe.vendor]:
            if self.shutdown.is_set():
                return "skipped"
            try:
                outcome = await self.probe_one(probe, row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Intentionally broad: one bad probe must not stop the scheduler
                logger.exception("pulse probe crashed", banana=probe.banana, error=str(exc))
                outcome = "crashed"
        self.counts[outcome] = self.counts.get(outcome, 0) + 1
        return outcome

    async def launch_due(self, *, force: bool = False) -> List[asyncio.Task]:
        probes = await self.roster()
        rows = await self.db.pulse.load_states([probe.banana for probe in probes])
        now = await self.db.pulse.get_database_now()
        launched = []
        for probe in probes:
            if probe.banana in self.in_flight:
                continue
            if not force and not is_due(rows.get(probe.banana), now):
                continue
            task = asyncio.create_task(self._run_probe(probe, rows.get(probe.banana)))
            self.in_flight[probe.banana] = task
            task.add_done_callback(lambda _, banana=probe.banana: self.in_flight.pop(banana, None))
            launched.append(task)
        return launched

    async def run_probe_cycle(self) -> Dict[str, int]:
        """Probe every jurisdiction once regardless of schedule (CLI --once)."""
        started = time.monotonic()
        self.counts = {}
        tasks = await self.launch_due(force=True)
        await asyncio.gather(*tasks)
        logger.info("pulse probe cycle", probes=len(tasks),
                    duration_seconds=round(time.monotonic() - started, 1), **self.counts)
        return dict(self.counts)

    async def drain_dirty(self) -> int:
        now = time.monotonic()
        dirty = [row for row in await self.db.pulse.list_dirty()
                 if self.sync_failures.get(row["banana"], (0, 0.0))[1] <= now]
        if not dirty:
            return 0
        ranges = {
            row["banana"]: span
            for row in dirty
            if not row["dirty_full"] and (span := targeted_range(row["dirty_dates"]))
        }
        bananas = [row["banana"] for row in dirty]
        started_at = await self.db.pulse.get_database_now()
        started = time.monotonic()
        results = await self.conductor.run_sync_cycle(bananas, command="pulse-sync", ranges=ranges)
        completed = [r.city_banana for r in results if r.status is SyncStatus.COMPLETED]
        cleared = await self.db.pulse.clear_dirty(completed, started_at)
        backed_off = self.record_sync_outcomes(bananas, set(completed))
        logger.info(
            "pulse sync cycle",
            dirty=len(bananas),
            targeted=len(ranges),
            completed=len(completed),
            cleared=cleared,
            backed_off=backed_off,
            meetings_found=sum(r.meetings_found for r in results),
            items_stored=sum(r.items_stored for r in results),
            duration_seconds=round(time.monotonic() - started, 1),
        )
        return len(completed)

    def record_sync_outcomes(self, bananas: List[str], completed: set) -> int:
        """Reset completed jurisdictions, push failed ones back; returns how many are backing off."""
        now = time.monotonic()
        for banana in bananas:
            if banana in completed:
                self.sync_failures.pop(banana, None)
                continue
            failures = self.sync_failures.get(banana, (0, 0.0))[0] + 1
            delay = min(SYNC_RETRY_CEILING_SECONDS, SYNC_RETRY_FLOOR_SECONDS * 2 ** (failures - 1))
            self.sync_failures[banana] = (failures, now + _jittered(delay))
        return len(self.sync_failures)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.shutdown.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def probe_loop(self) -> None:
        last_summary = time.monotonic()
        while not self.shutdown.is_set():
            try:
                await self.launch_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Intentionally broad: daemon supervision
                logger.exception("pulse scheduler tick failed", error=str(exc))
            if time.monotonic() - last_summary >= SUMMARY_LOG_SECONDS:
                logger.info("pulse probe summary", window_seconds=SUMMARY_LOG_SECONDS,
                            in_flight=len(self.in_flight), roster=len(self._roster), **self.counts)
                self.counts = {}
                last_summary = time.monotonic()
            await self._sleep(SCHEDULER_TICK_SECONDS)

    async def sync_loop(self) -> None:
        while not self.shutdown.is_set():
            try:
                await self.drain_dirty()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Intentionally broad: daemon supervision
                logger.exception("pulse sync cycle failed", error=str(exc))
            await self._sleep(SYNC_POLL_SECONDS)

    async def run_forever(self) -> None:
        # Shutdown cancels in-flight work rather than letting probes or a
        # sync finish: dirty_since survives, so an interrupted sync is
        # re-driven on restart and nothing is lost by stopping fast.
        loops = [asyncio.create_task(self.probe_loop())]
        if self.sync_enabled:
            loops.append(asyncio.create_task(self.sync_loop()))
        try:
            await self.shutdown.wait()
        finally:
            for task in [*loops, *self.in_flight.values()]:
                task.cancel()
            await asyncio.gather(*loops, *self.in_flight.values(), return_exceptions=True)
