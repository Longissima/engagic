"""
Async Session Manager for Vendor Adapters

Centralized HTTP session pooling using aiohttp for all vendor adapters.

Benefits:
- Connection reuse across all adapters (2-5x faster syncs)
- One session per vendor (not per city)
- Async I/O for concurrent city fetching
- Automatic cleanup on shutdown

Replaces: vendors/session_manager.py (sync version with requests)
"""

import asyncio
import os
import ssl
import aiohttp
from typing import Any, Awaitable, Callable, Dict, List, Optional

from config import config, get_logger

logger = get_logger(__name__).bind(component="vendor")

# Vendors whose TLS chain anchors in a root that the system ca-certificates
# package has dropped. We add those roots (data/ca_supplement.pem) on top of the
# system defaults for these vendors only -- proper verification, not disabled.
# eScribe: ca-certificates 20260601 removed AAA Certificate Services, which its
# Cloudflare/SSL.com chain still anchors to.
_VENDORS_NEEDING_CA_SUPPLEMENT = frozenset({"escribe"})
_CA_SUPPLEMENT_PATH = os.path.join(config.DB_DIR, "ca_supplement.pem")
_supplemented_ssl_context: Optional[ssl.SSLContext] = None


def _ca_supplemented_context() -> ssl.SSLContext:
    """System trust store plus the supplemental roots in data/ca_supplement.pem.

    Cached process-wide. Falls back to the plain default context if the file is
    missing so a packaging slip degrades to standard trust rather than breaking
    TLS outright.
    """
    global _supplemented_ssl_context
    if _supplemented_ssl_context is None:
        ctx = ssl.create_default_context()
        if os.path.exists(_CA_SUPPLEMENT_PATH):
            ctx.load_verify_locations(cafile=_CA_SUPPLEMENT_PATH)
        else:
            logger.warning("ca supplement file missing", path=_CA_SUPPLEMENT_PATH)
        _supplemented_ssl_context = ctx
    return _supplemented_ssl_context


class AsyncSessionManager:
    """
    Manages aiohttp client sessions for vendor adapters.

    Creates one shared session per vendor with connection pooling.
    Sessions are created lazily and reused for process lifetime.
    """

    _sessions: Dict[str, aiohttp.ClientSession] = {}
    # Adapters that hold non-aiohttp clients (curl_cffi, etc.) register an
    # async closer here so close_all() can tear them down too. Without this,
    # libcurl handles in adapters like municode/visioninternet stay open past
    # process shutdown, leaving CLOSE_WAIT sockets and deadlocking asyncpg's
    # Pool.close() teardown path.
    _extra_closers: List[Callable[[], Awaitable[None]]] = []
    _closed = False

    # Budget for any single foreign-session close. curl_cffi sessions can hold
    # in-flight requests that won't unblock; we'd rather log+move on than hang
    # the event loop.
    _FOREIGN_CLOSE_TIMEOUT_SECONDS = 10

    @classmethod
    async def get_session(cls, vendor: str, timeout_total: int = 30) -> aiohttp.ClientSession:
        """
        Get or create aiohttp session for vendor.

        Args:
            vendor: Vendor name (e.g., "legistar", "primegov", "granicus")
            timeout_total: Total timeout in seconds (default: 30s)

        Returns:
            Shared aiohttp.ClientSession for vendor
        """
        if cls._closed:
            raise RuntimeError("AsyncSessionManager has been closed")

        if vendor not in cls._sessions or cls._sessions[vendor].closed:
            # Create new session with connection pooling
            timeout = aiohttp.ClientTimeout(
                total=timeout_total,
                connect=10,  # 10s to establish connection
                sock_read=timeout_total  # Total time to read response
            )

            # Connection pooling configuration
            connector_kwargs: Dict[str, Any] = {
                "limit": 20,  # Max 20 total connections per vendor
                "limit_per_host": 5,  # Max 5 connections per host
                "ttl_dns_cache": 300,  # Cache DNS for 5 minutes
                "enable_cleanup_closed": True,  # Clean up closed connections
            }
            if vendor in _VENDORS_NEEDING_CA_SUPPLEMENT:
                connector_kwargs["ssl"] = _ca_supplemented_context()
            connector = aiohttp.TCPConnector(**connector_kwargs)

            # Browser-like headers to avoid bot detection
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive"
            }

            cls._sessions[vendor] = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers=headers,
                raise_for_status=False  # Don't raise on 4xx/5xx (handle in adapters)
            )

            logger.debug(
                "created async session",
                vendor=vendor,
                max_connections=20,
                timeout_seconds=timeout_total
            )

        return cls._sessions[vendor]

    @classmethod
    def register_closer(cls, closer: Callable[[], Awaitable[None]]) -> None:
        """Register an async cleanup callback for non-aiohttp clients.

        Adapters that lazy-init curl_cffi (or any other client outside this
        manager's bookkeeping) must call this so close_all() can tear them
        down. The callback should be idempotent.
        """
        if cls._closed:
            logger.warning("register_closer called after close_all; resource may leak")
            return
        cls._extra_closers.append(closer)

    @classmethod
    async def close_all(cls):
        """
        Close all active sessions (cleanup on shutdown).

        Call this when application is shutting down to properly
        close all HTTP connections.
        """
        if cls._closed:
            return

        logger.info(
            "closing async sessions",
            session_count=len(cls._sessions),
            extra_closer_count=len(cls._extra_closers),
        )

        for vendor, session in cls._sessions.items():
            if not session.closed:
                await session.close()
                logger.debug("closed async session", vendor=vendor)

        # Close non-aiohttp clients (e.g. curl_cffi). Bound each call so a
        # stuck libcurl handle can't deadlock the whole shutdown -- we'd rather
        # leak one fd than hang asyncpg's Pool.close().
        for closer in cls._extra_closers:
            try:
                await asyncio.wait_for(closer(), timeout=cls._FOREIGN_CLOSE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("foreign session close timed out", timeout_seconds=cls._FOREIGN_CLOSE_TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning("foreign session close failed", error=str(e), error_type=type(e).__name__)

        cls._sessions.clear()
        cls._extra_closers.clear()
        cls._closed = True

    @classmethod
    async def close_session(cls, vendor: str):
        """
        Close session for specific vendor.

        Args:
            vendor: Vendor name to close session for
        """
        if vendor in cls._sessions:
            session = cls._sessions[vendor]
            if not session.closed:
                await session.close()
                logger.debug("closed async session", vendor=vendor)
            del cls._sessions[vendor]

    @classmethod
    def get_stats(cls) -> Dict[str, Any]:
        """Get statistics about active sessions"""
        stats: Dict[str, Any] = {
            "total_sessions": len(cls._sessions),
            "closed": cls._closed,
            "vendors": {}
        }

        for vendor, session in cls._sessions.items():
            connector = session.connector
            if connector and hasattr(connector, "_conns"):
                stats["vendors"][vendor] = {
                    "closed": session.closed,
                    "active_connections": len(connector._conns) if hasattr(connector, "_conns") else 0
                }

        return stats
