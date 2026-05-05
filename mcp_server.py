#!/usr/bin/env python3
"""Engagic MCP Server -- civic data tools for Claude clients.

Exposes the Engagic database conversationally to any MCP-compatible client
(browser Claude, desktop Claude, Claude Code). Runs on port 8003 behind nginx.
Uses streamable-http transport (POST+GET on single endpoint).
"""

import os
import re
import json
import asyncio
from typing import Optional

import uvicorn
from starlette.responses import Response

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from database.db_postgres import Database
from config import get_logger

logger = get_logger("mcp_server")

MCP_TOKEN = os.environ.get("ENGAGIC_MCP_TOKEN", "")

WRITE_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|CALL)\b",
    re.IGNORECASE,
)

db: Optional[Database] = None

mcp = FastMCP(
    "engagic",
    instructions=(
        "Civic data search across US city councils. "
        "Cities use 'banana' IDs (e.g. 'paloaltoCA', 'jacksonvilleFL'). "
        "Use list_cities to discover available cities and their banana IDs."
    ),
    # Endpoint at / since nginx strips /mcp/ prefix
    streamable_http_path="/",
    # Behind nginx -- host validation happens there, not here
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def validate_readonly_sql(sql: str) -> Optional[str]:
    """Return error message if SQL is not a safe read-only query, None if OK."""
    cleaned = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    cleaned = re.sub(r"--.*$", "", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()
    if not cleaned.upper().startswith("SELECT"):
        return "Only SELECT queries are allowed"
    if WRITE_SQL.search(cleaned):
        return "Query contains disallowed keywords"
    # Reject multi-statement injection
    if ";" in cleaned.rstrip(";"):
        return "Multiple statements not allowed"
    return None


# -- Tools --


@mcp.tool()
async def search_items(query: str, city: str, limit: int = 20) -> str:
    """Search agenda items by keyword in a specific city.

    Args:
        query: Search terms (e.g. 'rezoning', 'water infrastructure', 'budget')
        city: Banana ID (e.g. 'paloaltoCA', 'jacksonvilleFL'). Use list_cities to find IDs.
        limit: Max results (default 20, max 100)
    """
    limit = min(limit, 100)
    results = await db.search.search_items_fulltext(query, city, limit=limit)
    return json.dumps(results, default=str)


@mcp.tool()
async def search_matters(query: str, city: str, limit: int = 20) -> str:
    """Search legislative matters (bills, ordinances, resolutions) in a city.

    Args:
        query: Search terms
        city: Banana ID
        limit: Max results (default 20, max 100)
    """
    limit = min(limit, 100)
    matters = await db.matters.search_matters_fulltext(query, city, limit=limit)
    return json.dumps([m.to_dict() for m in matters], default=str)


@mcp.tool()
async def list_cities(state: Optional[str] = None) -> str:
    """List all covered cities, optionally filtered by US state.

    Args:
        state: Two-letter state code (e.g. 'CA', 'TX'). Omit for all.
    """
    cities = await db.jurisdictions.get_cities(state=state)
    return json.dumps([c.to_dict() for c in cities], default=str)


@mcp.tool()
async def get_meeting(meeting_id: str) -> str:
    """Get full meeting details with all agenda items.

    Args:
        meeting_id: Meeting UUID
    """
    meeting = await db.meetings.get_meeting(meeting_id)
    if not meeting:
        return json.dumps({"error": "Meeting not found"})
    result = meeting.to_dict()
    items = await db.get_agenda_items(meeting_id, load_matters=True)
    result["items"] = [item.to_dict() for item in items]
    return json.dumps(result, default=str)


@mcp.tool()
async def get_matter_timeline(matter_id: str) -> str:
    """Track a legislative matter's journey across meetings over time.

    Args:
        matter_id: Matter UUID
    """
    timeline = await db.matters.get_timeline(matter_id)
    if not timeline:
        return json.dumps({"error": "Matter not found or has no timeline"})
    return json.dumps(timeline, default=str)


@mcp.tool()
async def get_city_meetings(city: str, limit: int = 20) -> str:
    """Get recent meetings for a city, newest first.

    Args:
        city: Banana ID
        limit: Max results (default 20, max 100)
    """
    limit = min(limit, 100)
    meetings = await db.meetings.get_meetings_for_city(city, limit=limit)
    return json.dumps([m.to_dict() for m in meetings], default=str)


@mcp.tool()
async def run_query(sql: str) -> str:
    """Execute a read-only SQL query against the civic database.

    Only SELECT statements allowed. Useful for aggregations and custom analysis.

    Key tables: cities, meetings, agenda_items, matters, meeting_topics,
    item_topics, matter_votes. Cities have 'banana' as primary key.

    Args:
        sql: A SELECT query (include LIMIT for large tables)
    """
    error = validate_readonly_sql(sql)
    if error:
        return json.dumps({"error": error})
    try:
        rows = await db.pool.fetch(sql)
        return json.dumps([dict(r) for r in rows], default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# -- Auth middleware (ASGI-level, streaming-safe -- no response buffering) --


class BearerAuthMiddleware:
    """ASGI middleware that checks Authorization: Bearer <token> on HTTP requests."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            headers = dict(scope.get("headers", []))
            auth_value = headers.get(b"authorization", b"").decode()
            if auth_value != f"Bearer {self.token}":
                response = Response("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# -- Entrypoint --


async def run():
    global db
    db = await Database.create(min_size=2, max_size=5)
    logger.info("mcp.started", port=8003)

    http_app = mcp.streamable_http_app()
    app = BearerAuthMiddleware(http_app, MCP_TOKEN)

    server_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=8003,
        log_level="info",
    )
    server = uvicorn.Server(server_config)
    try:
        await server.serve()
    finally:
        await db.close()
        logger.info("mcp.stopped")


if __name__ == "__main__":
    asyncio.run(run())
