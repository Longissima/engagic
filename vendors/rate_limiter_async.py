"""Async vendor-aware rate limiter to be respectful to city websites"""

import asyncio
import time
import random
from collections import defaultdict
from typing import Dict, List, Optional
from urllib.parse import urlparse

from config import get_logger

logger = get_logger(__name__).bind(component="vendor")


# Per-vendor minimum interval between requests on a single slot.
DELAYS: Dict[str, float] = {
    # Vendor-hosted APIs / SaaS endpoints (talking to vendor infra, not city
    # site) -- aggressive pacing OK; these are sized for high-volume traffic.
    "legistar": 0.3,    # webapi.legistar.com (Azure REST API)
    "civicclerk": 0.5,  # Azure-hosted multi-tenant API
    "granicus": 0.6,    # {city}.granicus.com / mccmeetings blob
    "primegov": 0.5,
    "iqm2": 0.6,        # owned by Granicus, similar infra
    "municode": 0.6,    # meetings.municode.com + {tenant}.municodemeetings.com
    "boardbook": 0.5,   # Sparq central host, hundreds of districts
    "novusagenda": 0.8,
    "escribe": 0.8,
    "civicweb": 0.8,
    "agendaonline": 0.8,
    "destiny": 1.0,
    "onbase": 1.0,      # mixed: hylandcloud SaaS + self-hosted city subpaths
    # Hits the city's own site -- be polite to municipal infra.
    "civicplus": 2.7,   # Cloudflare-protected; modest concurrency via slots
    "civicengage": 2.0, # archive.aspx on city domain
    "visioninternet": 2.7,  # Akamai-fronted city sites; blocks aggressively
    "proudcity": 1.8,   # city WordPress
    "wp_events": 1.8,   # city WordPress
    "berkeley": 1.8,
    "menlopark": 1.8,
    "chicago": 1.8,
    "ross": 1.8,
    "unknown": 2.7,
}

# Per-vendor concurrent slots. Each slot is paced independently at DELAYS[vendor],
# so aggregate throughput is approximately SLOTS / DELAYS req/sec.
#
# Default is 1 (strict serialization, the historical behavior). Bump up for
# multi-tenant SaaS vendors where many cities live behind one domain and the
# vendor is sized for high concurrent traffic from real district staff. Keep
# at 1 for vendors that have shown signs of aggressive bot blocking.
SLOTS: Dict[str, int] = {
    # Vendor-hosted multi-tenant SaaS / APIs -- bursts of N concurrent reqs OK
    "legistar": 12,   # webapi.legistar.com one shared Azure host
    "civicclerk": 10,
    "granicus": 8,
    "primegov": 6,
    "iqm2": 6,
    "municode": 6,
    "boardbook": 6,
    "novusagenda": 4,
    "escribe": 4,
    "civicweb": 4,
    "agendaonline": 3,
    "onbase": 12,     # each city is on its own domain, no real cross-city contention
    "destiny": 3,
    "civicplus": 3,   # each city is on its own domain; slots ~ effective parallel hosts
    # City-direct scrapers stay at 1 (default below)
}


class AsyncRateLimiter:
    """Multi-slot async rate limiter.

    Each vendor gets a pool of slots; each slot enforces its own per-request
    minimum interval. A request reserves the earliest-available slot, computes
    when it can fire, and sleeps outside the lock so other coroutines can
    reserve their own slots in parallel.

    For SLOTS[vendor]=N and DELAYS[vendor]=D, sustained throughput is roughly
    N/D requests per second; bursts of N requests can land back-to-back.
    """

    def __init__(self):
        self._slot_times: Dict[str, List[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def wait_if_needed(self, vendor: str):
        min_delay = DELAYS.get(vendor, DELAYS["unknown"])
        n_slots = SLOTS.get(vendor, 1)
        # Jitter scales with delay: 0-1s of jitter on a 0.3s delay would
        # dominate; cap jitter at half the configured delay so high-throughput
        # API vendors actually pace at their configured rate.
        if vendor == "civicplus":
            jitter = random.uniform(0, 2)
        else:
            jitter = random.uniform(0, min(1.0, min_delay * 0.5))

        # Reserve a slot inside the lock; sleep outside so concurrent callers
        # can serialize their reservations without serializing their waits.
        async with self._lock:
            now = time.time()
            slots = self._slot_times[vendor]
            while len(slots) < n_slots:
                slots.append(0.0)

            earliest_idx = min(range(n_slots), key=lambda i: slots[i])
            earliest_time = slots[earliest_idx]

            fire_at = max(now, earliest_time + min_delay) + jitter
            slots[earliest_idx] = fire_at
            sleep_time = fire_at - now

        if sleep_time > 0.05:
            # Routine pacing waits log at debug; only surface long stalls to
            # info so the console isn't flooded during multi-city syncs.
            log_fn = logger.info if sleep_time >= 30 else logger.debug
            log_fn(
                "vendor rate limit",
                vendor=vendor,
                sleep_seconds=round(sleep_time, 1),
                slots=n_slots,
            )
            await asyncio.sleep(sleep_time)

    async def respect_retry_after(self, vendor: str, seconds: float):
        """Honor a Retry-After header by deferring all of the vendor's slots.

        Caps the deferral so a misbehaving server can't park the sync forever.
        """
        seconds = max(0.0, min(seconds, 120.0))
        async with self._lock:
            n_slots = SLOTS.get(vendor, 1)
            slots = self._slot_times[vendor]
            while len(slots) < n_slots:
                slots.append(0.0)
            target = time.time() + seconds
            for i in range(n_slots):
                if slots[i] < target:
                    slots[i] = target
        logger.info("honoring retry-after", vendor=vendor, defer_seconds=round(seconds, 1))


# Process-wide singleton: every adapter and the fetcher share one limiter so
# per-request gating, per-city gating, and Retry-After deferrals all serialize
# against the same per-vendor slot tracking.
_GLOBAL_RATE_LIMITER: Optional[AsyncRateLimiter] = None


def get_rate_limiter() -> AsyncRateLimiter:
    global _GLOBAL_RATE_LIMITER
    if _GLOBAL_RATE_LIMITER is None:
        _GLOBAL_RATE_LIMITER = AsyncRateLimiter()
    return _GLOBAL_RATE_LIMITER


# Host-substring → vendor name. Order matters: more-specific entries first
# (e.g. legistar.granicus.com before granicus.com).
_HOST_VENDOR_PATTERNS = [
    ("meetings.boardbook.org", "boardbook"),
    ("legistar.granicus.com", "legistar"),
    ("legistar1.granicus.com", "legistar"),
    ("legistar2.granicus.com", "legistar"),
    ("legistar3.granicus.com", "legistar"),
    (".legistar.com", "legistar"),
    (".legistar1.com", "legistar"),
    (".granicus.com", "granicus"),
    ("granicus_production_attachments", "granicus"),
    (".primegov.com", "primegov"),
    (".api.civicclerk.com", "civicclerk"),
    (".novusagenda.com", "novusagenda"),
    (".civicplus.com", "civicplus"),
    (".civicweb.net", "civicweb"),
    (".iqm2.com", "iqm2"),
    ("municodemeetings.com", "municode"),
    (".escribemeetings.com", "escribe"),
    ("public.destinyhosted.com", "destiny"),
    ("destinyhosted.com", "destiny"),
    ("hylandcloud.com", "onbase"),
    ("berkeleyca.gov", "berkeley"),
    ("menlopark.gov", "menlopark"),
    ("chicityclerkelms.chicago.gov", "chicago"),
]


def vendor_for_url(url: str) -> str:
    """Map a URL to its vendor name for rate-limiting purposes.

    Returns "unknown" for hosts we don't recognize (gets conservative pacing)
    or for shared CDNs (S3, CloudFront) where the vendor is ambiguous.
    """
    if not url:
        return "unknown"
    try:
        parsed = urlparse(url)
    except (ValueError, AttributeError):
        return "unknown"
    host = (parsed.netloc or "").lower()
    if not host:
        return "unknown"

    # Granicus's staff reports live on S3 with a vendor-tagged bucket name in
    # the path; the host alone is just `s3.amazonaws.com`. Check the path too.
    if "granicus_production_attachments" in (parsed.path or "").lower():
        return "granicus"

    for pattern, vendor in _HOST_VENDOR_PATTERNS:
        if pattern in host:
            return vendor
    return "unknown"
