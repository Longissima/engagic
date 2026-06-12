"""URL refresh - re-resolve ephemeral attachment URLs to fresh ones at fetch time.

Some vendors hand out signed-URL artifacts (Azure SAS, S3 presigned, etc.) at
scrape time. Those URLs expire long before our processor or a re-run might
need to fetch them. The durable references are vendor-specific identifiers
that we store on AttachmentInfo (cc_agenda_id, cc_attachment_id, history_id,
meta_id, ...). This module turns those identifiers into a fresh, valid URL
right before extraction.

Today: CivicClerk only. Granicus and OnBase have similar shapes and could
adopt the same pattern; the dispatch table at the bottom is the extension
point.

Confidence: 8/10 -- end-to-end verified for CivicClerk (refetch /v1/Meetings/
{agenda_id} returns freshly-signed pdfVersionFullPath, downloads OK).
"""

import asyncio
import re
from typing import Dict, Iterable, List, Optional, Tuple

import aiohttp

from config import get_logger
from database.models import AttachmentInfo
from vendors.session_manager_async import AsyncSessionManager

logger = get_logger(__name__).bind(component="url_refresh")


# Portal URL shape: https://{slug}.portal.civicclerk.com/event/{event_id}/files/attachment/{att_id}
_CC_PORTAL_RE = re.compile(
    r"https://([^.]+)\.portal\.civicclerk\.com/event/(\d+)/files/attachment/(\d+)"
)


def _civicclerk_identity(att: AttachmentInfo) -> Optional[Tuple[Optional[int], Optional[int], Optional[int]]]:
    """Return (agenda_id, attachment_id, event_id) if this looks like a CivicClerk attachment.

    Falls back to parsing portal_url for rows scraped before durable IDs were stored.
    Reports carry no per-attachment id -- (agenda_id, None, None) marks them as
    refreshable by blob-path match within their re-fetched agenda.
    Returns None if the attachment isn't a CivicClerk one (or can't be identified).
    """
    if att.cc_attachment_id is not None:
        return (att.cc_agenda_id, att.cc_attachment_id, None)

    # Backfill path: parse portal_url for older rows that predate cc_* fields.
    if att.portal_url:
        m = _CC_PORTAL_RE.match(att.portal_url)
        if m:
            event_id = int(m.group(2))
            att_id = int(m.group(3))
            return (None, att_id, event_id)

    # Reports: agenda ref only, renewed by path match.
    if att.cc_agenda_id is not None:
        return (att.cc_agenda_id, None, None)

    return None


async def _resolve_agenda_id_for_event(
    session: aiohttp.ClientSession, slug: str, event_id: int
) -> Optional[int]:
    """One-shot lookup: event_id -> agenda_id via OData filter.

    Used only for old rows that lack cc_agenda_id. New scrapes store agenda_id
    directly so this round-trip is skipped.
    """
    url = f"https://{slug}.api.civicclerk.com/v1/Events?$filter=id%20eq%20{event_id}"
    headers = {
        "Origin": f"https://{slug}.portal.civicclerk.com",
        "Referer": f"https://{slug}.portal.civicclerk.com/",
        "Accept": "application/json",
    }
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.warning("event lookup failed", slug=slug, event_id=event_id, status=resp.status)
                return None
            data = await resp.json()
    except (aiohttp.ClientError, OSError) as e:
        logger.warning("event lookup error", slug=slug, event_id=event_id, error=str(e))
        return None

    rows = data.get("value", [])
    if not rows:
        return None
    agenda_id = rows[0].get("agendaId")
    return int(agenda_id) if agenda_id is not None else None


async def _fetch_fresh_attachment_urls(
    session: aiohttp.ClientSession, slug: str, agenda_id: int
) -> Tuple[Dict[int, str], Dict[str, str]]:
    """Hit /v1/Meetings/{agenda_id} and return fresh-URL maps.

    Returns ({attachment_id: fresh url}, {blob_path: fresh url}). The path map
    covers reportsList entries too -- reports have no per-attachment id, but
    SAS re-signing only rotates the query string, so the bare blob path is a
    stable join key between a stored URL and its freshly-signed counterpart.

    The CivicClerk API re-signs SAS tokens on every request, so the URLs in
    this response are always good for ~7 days from the moment of the call.
    """
    url = f"https://{slug}.api.civicclerk.com/v1/Meetings/{agenda_id}"
    headers = {
        "Origin": f"https://{slug}.portal.civicclerk.com",
        "Referer": f"https://{slug}.portal.civicclerk.com/",
        "Accept": "application/json",
    }
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.warning("meeting lookup failed", slug=slug, agenda_id=agenda_id, status=resp.status)
                return {}, {}
            data = await resp.json()
    except (aiohttp.ClientError, OSError) as e:
        logger.warning("meeting lookup error", slug=slug, agenda_id=agenda_id, error=str(e))
        return {}, {}

    by_id: Dict[int, str] = {}
    by_path: Dict[str, str] = {}

    def walk(items: List[Dict]) -> None:
        for it in items:
            for a in it.get("attachmentsList", []):
                aid = a.get("id")
                fresh = a.get("pdfVersionFullPath") or a.get("mediaFullPath")
                if not fresh:
                    continue
                by_path[fresh.split("?", 1)[0]] = fresh
                if aid is not None:
                    try:
                        by_id[int(aid)] = fresh
                    except (TypeError, ValueError):
                        continue
            for r in it.get("reportsList", []) or []:
                if r.get("isDeleted"):
                    continue
                fresh = r.get("pdfMediaFullPath")
                if fresh:
                    by_path[fresh.split("?", 1)[0]] = fresh
            children = it.get("childItems") or []
            if children:
                walk(children)

    walk(data.get("items", []))
    return by_id, by_path


async def refresh_civicclerk_urls(
    slug: str, attachments: Iterable[AttachmentInfo]
) -> int:
    """Mutate matching attachments' .url fields to freshly-signed CivicClerk URLs.

    Returns: number of attachments refreshed.

    Two passes: attachments with a stored attachment id are matched directly;
    everything else (reports, pre-fix rows with no durable refs at all) is
    matched by bare blob path against every agenda fetched in this call --
    SAS re-signing only rotates the query string, so path equality is exact.
    A path-pass hit on a row missing cc_agenda_id backfills it for next time.

    Cost: at most one /v1/Meetings/{agenda_id} call per unique agenda_id, plus
    one /v1/Events?$filter call per unique event_id that lacks a stored agenda_id
    (backfill path for pre-fix rows).
    """
    # Snapshot once -- callers may pass a generator
    all_atts: List[AttachmentInfo] = list(attachments)
    targets: List[AttachmentInfo] = [
        att for att in all_atts if _civicclerk_identity(att) is not None
    ]

    if not targets:
        return 0

    session = await AsyncSessionManager.get_session("civicclerk")

    # Phase 1: backfill missing agenda_id via event_id (rare, only for old rows)
    event_to_agenda: Dict[int, Optional[int]] = {}
    for att in targets:
        ident = _civicclerk_identity(att)
        if ident is None:
            continue
        agenda_id, _, event_id = ident
        if agenda_id is None and event_id is not None and event_id not in event_to_agenda:
            event_to_agenda[event_id] = await _resolve_agenda_id_for_event(session, slug, event_id)

    # Phase 2: collect agenda_ids we actually need to fetch
    agenda_ids: set[int] = set()
    for att in targets:
        ident = _civicclerk_identity(att)
        if ident is None:
            continue
        agenda_id, _, event_id = ident
        if agenda_id is None and event_id is not None:
            agenda_id = event_to_agenda.get(event_id)
        if agenda_id is not None:
            agenda_ids.add(agenda_id)

    # Phase 3: fetch fresh URL maps in parallel, one per agenda_id
    fresh_by_id: Dict[int, Dict[int, str]] = {}
    path_to_fresh: Dict[str, Tuple[int, str]] = {}  # blob path -> (agenda_id, fresh url)
    if agenda_ids:
        results = await asyncio.gather(
            *[_fetch_fresh_attachment_urls(session, slug, aid) for aid in agenda_ids],
            return_exceptions=True,
        )
        for aid, res in zip(agenda_ids, results):
            if isinstance(res, BaseException):
                logger.warning("agenda fetch raised", slug=slug, agenda_id=aid, error=str(res))
                continue
            by_id, by_path = res
            fresh_by_id[aid] = by_id
            for path, fresh in by_path.items():
                path_to_fresh[path] = (aid, fresh)

    # Phase 4a: direct id match, mutating attachments in place
    refreshed = 0
    for att in targets:
        ident = _civicclerk_identity(att)
        if ident is None:
            continue
        agenda_id, att_id, event_id = ident
        if att_id is None:
            continue  # id-less rows are handled by the path pass below
        if agenda_id is None and event_id is not None:
            agenda_id = event_to_agenda.get(event_id)
        if agenda_id is None:
            continue
        fresh_url = fresh_by_id.get(agenda_id, {}).get(att_id)
        if fresh_url and fresh_url != att.url:
            att.url = fresh_url
            # Also remember the agenda_id for next time, if we backfilled it
            if att.cc_agenda_id is None:
                att.cc_agenda_id = agenda_id
            if att.cc_attachment_id is None:
                att.cc_attachment_id = att_id
            refreshed += 1

    # Phase 4b: blob-path match for everything still stale. Catches reports
    # (agenda ref but no attachment id) and pre-fix rows with no durable refs
    # at all, as long as a sibling's agenda got fetched above. Rows already
    # refreshed in 4a compare equal to the fresh URL and fall through.
    for att in all_atts:
        if not att.url:
            continue
        hit = path_to_fresh.get(att.url.split("?", 1)[0])
        if hit is None:
            continue
        hit_agenda_id, fresh_url = hit
        if fresh_url != att.url:
            att.url = fresh_url
            if att.cc_agenda_id is None:
                att.cc_agenda_id = hit_agenda_id  # self-heal: durable ref for next refresh
            refreshed += 1

    if refreshed:
        logger.info("refreshed civicclerk urls", slug=slug, refreshed=refreshed, agenda_count=len(agenda_ids))
    return refreshed


async def refresh_attachment_urls(vendor: str, slug: str, attachments: Iterable[AttachmentInfo]) -> int:
    """Vendor-aware dispatch. Pass-through for vendors without a refresher."""
    if vendor == "civicclerk":
        return await refresh_civicclerk_urls(slug, attachments)
    return 0
