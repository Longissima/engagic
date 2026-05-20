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


def _civicclerk_identity(att: AttachmentInfo) -> Optional[Tuple[Optional[int], int, Optional[int]]]:
    """Return (agenda_id, attachment_id, event_id) if this looks like a CivicClerk attachment.

    Falls back to parsing portal_url for rows scraped before durable IDs were stored.
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
) -> Dict[int, str]:
    """Hit /v1/Meetings/{agenda_id} and return {attachment_id: fresh pdf URL}.

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
                return {}
            data = await resp.json()
    except (aiohttp.ClientError, OSError) as e:
        logger.warning("meeting lookup error", slug=slug, agenda_id=agenda_id, error=str(e))
        return {}

    out: Dict[int, str] = {}

    def walk(items: List[Dict]) -> None:
        for it in items:
            for a in it.get("attachmentsList", []):
                aid = a.get("id")
                fresh = a.get("pdfVersionFullPath") or a.get("mediaFullPath")
                if aid is not None and fresh:
                    try:
                        out[int(aid)] = fresh
                    except (TypeError, ValueError):
                        continue
            children = it.get("childItems") or []
            if children:
                walk(children)

    walk(data.get("items", []))
    return out


async def refresh_civicclerk_urls(
    slug: str, attachments: Iterable[AttachmentInfo]
) -> int:
    """Mutate matching attachments' .url fields to freshly-signed CivicClerk URLs.

    Returns: number of attachments refreshed.

    Cost: at most one /v1/Meetings/{agenda_id} call per unique agenda_id, plus
    one /v1/Events?$filter call per unique event_id that lacks a stored agenda_id
    (backfill path for pre-fix rows).
    """
    # Snapshot once -- callers may pass a generator
    targets: List[AttachmentInfo] = []
    for att in attachments:
        ident = _civicclerk_identity(att)
        if ident is not None:
            targets.append(att)

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
    fresh_maps: Dict[int, Dict[int, str]] = {}
    if agenda_ids:
        results = await asyncio.gather(
            *[_fetch_fresh_attachment_urls(session, slug, aid) for aid in agenda_ids],
            return_exceptions=True,
        )
        for aid, res in zip(agenda_ids, results):
            if isinstance(res, BaseException):
                logger.warning("agenda fetch raised", slug=slug, agenda_id=aid, error=str(res))
                continue
            fresh_maps[aid] = res

    # Phase 4: assign fresh URLs, mutating attachments in place
    refreshed = 0
    for att in targets:
        ident = _civicclerk_identity(att)
        if ident is None:
            continue
        agenda_id, att_id, event_id = ident
        if agenda_id is None and event_id is not None:
            agenda_id = event_to_agenda.get(event_id)
        if agenda_id is None:
            continue
        fresh_url = fresh_maps.get(agenda_id, {}).get(att_id)
        if fresh_url and fresh_url != att.url:
            att.url = fresh_url
            # Also remember the agenda_id for next time, if we backfilled it
            if att.cc_agenda_id is None:
                att.cc_agenda_id = agenda_id
            if att.cc_attachment_id is None:
                att.cc_attachment_id = att_id
            refreshed += 1

    if refreshed:
        logger.info("refreshed civicclerk urls", slug=slug, refreshed=refreshed, agenda_count=len(agenda_ids))
    return refreshed


async def refresh_attachment_urls(vendor: str, slug: str, attachments: Iterable[AttachmentInfo]) -> int:
    """Vendor-aware dispatch. Pass-through for vendors without a refresher."""
    if vendor == "civicclerk":
        return await refresh_civicclerk_urls(slug, attachments)
    return 0
