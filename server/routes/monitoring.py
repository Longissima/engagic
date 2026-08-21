"""
Monitoring and health check API routes
"""

from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import JSONResponse, Response

from config import config, get_logger
from database.db_postgres import Database
from server.dependencies import get_db
from server.metrics import metrics, get_metrics_text

logger = get_logger(__name__)


router = APIRouter()


@router.get("/")
async def root():
    """API status and info"""
    return {
        "service": "engagic API",
        "status": "running",
        "version": "2.0.0",
        "description": "Civic engagement made simple - Search and access local government meetings",
        "documentation": "https://github.com/Engagic/engagic#api-documentation",
        "endpoints": {
            "search": "POST /api/search - Search for meetings by zipcode or city name",
            "process": "POST /api/process-agenda - Get cached meeting agenda summary",
            "random_meeting": "GET /api/random-meeting-with-items - Get a random meeting with item summaries",
            "topics": "GET /api/topics - Get all available topics for filtering",
            "topics_popular": "GET /api/topics/popular - Get most common topics across all meetings",
            "search_by_topic": "POST /api/search/by-topic - Search meetings by topic",
            "stats": "GET /api/stats - System statistics and metrics",
            "queue_stats": "GET /api/queue-stats - Processing queue statistics",
            "health": "GET /api/health - Health check with detailed status",
            "metrics": "GET /api/metrics - Detailed system metrics",
            "admin": {
                "city_requests": "GET /api/admin/city-requests - View requested cities",
                "sync_city": "POST /api/admin/sync-city/{city_slug} - Force sync specific city",
                "process_meeting": "POST /api/admin/process-meeting - Force process specific meeting",
            },
        },
        "usage_examples": {
            "search_by_zipcode": {
                "method": "POST",
                "url": "/api/search",
                "body": {"query": "94301"},
                "description": "Search meetings by ZIP code",
            },
            "search_by_city": {
                "method": "POST",
                "url": "/api/search",
                "body": {"query": "Palo Alto, CA"},
                "description": "Search meetings by city and state",
            },
            "search_ambiguous": {
                "method": "POST",
                "url": "/api/search",
                "body": {"query": "Springfield"},
                "description": "Search by city name only (may return multiple options)",
            },
            "get_summary": {
                "method": "POST",
                "url": "/api/process-agenda",
                "body": {
                    "packet_url": "https://example.com/agenda.pdf",
                    "banana": "paloaltoCA",
                    "meeting_name": "City Council Meeting",
                },
                "description": "Get cached AI summary of meeting agenda",
            },
        },
        "rate_limiting": f"{config.RATE_LIMIT_REQUESTS} requests per {config.RATE_LIMIT_WINDOW} seconds per IP",
        "features": [
            "ZIP code and city name search",
            "AI-powered meeting summaries",
            "Ambiguous city name handling",
            "Real-time meeting data caching",
            "Multiple city system adapters",
            "Background data processing",
            "Comprehensive error handling",
            "Request demand tracking",
        ],
        "data_sources": [
            "PrimeGov (city council management)",
            "CivicClerk (municipal systems)",
            "Direct city websites",
        ],
    }


@router.get("/api/map-stats")
async def get_map_stats(db: Database = Depends(get_db)):
    """Compact per-city coverage tier for map feature-state overlay.

    Returns {banana: {t: coverage_type, c: summary_count}} so the map can paint
    cities whose tier advanced since the last tile regen. Mirrors /api/city-coverage.
    """
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("""
                WITH
                    matter_counts AS (
                        SELECT banana, COUNT(*) AS cnt
                        FROM city_matters
                        WHERE canonical_summary IS NOT NULL AND canonical_summary != ''
                        GROUP BY banana
                    ),
                    item_counts AS (
                        SELECT m.banana, COUNT(*) AS cnt
                        FROM items i
                        JOIN meetings m ON i.meeting_id = m.id
                        WHERE i.summary IS NOT NULL AND i.summary != ''
                          AND (i.matter_id IS NULL OR i.matter_id NOT IN (
                              SELECT id FROM city_matters
                              WHERE canonical_summary IS NOT NULL AND canonical_summary != ''
                          ))
                        GROUP BY m.banana
                    ),
                    meeting_counts AS (
                        SELECT banana, COUNT(*) AS cnt
                        FROM meetings
                        WHERE summary IS NOT NULL AND summary != ''
                        GROUP BY banana
                    ),
                    synced_counts AS (
                        SELECT banana, COUNT(*) AS cnt
                        FROM meetings
                        WHERE title IS NOT NULL AND title != ''
                          AND date IS NOT NULL
                        GROUP BY banana
                    )
                SELECT
                    c.banana,
                    CASE
                        WHEN COALESCE(mc.cnt, 0) > 0 THEN 'matter'
                        WHEN COALESCE(ic.cnt, 0) > 0 THEN 'item'
                        WHEN COALESCE(mtg.cnt, 0) > 0 THEN 'monolithic'
                        WHEN COALESCE(sc.cnt, 0) > 0 THEN 'synced'
                        ELSE 'pending'
                    END AS coverage_type,
                    CASE
                        WHEN COALESCE(mc.cnt, 0) > 0 THEN mc.cnt + COALESCE(ic.cnt, 0)
                        WHEN COALESCE(ic.cnt, 0) > 0 THEN ic.cnt
                        WHEN COALESCE(mtg.cnt, 0) > 0 THEN mtg.cnt
                        WHEN COALESCE(sc.cnt, 0) > 0 THEN sc.cnt
                        ELSE 0
                    END AS summary_count
                FROM jurisdictions c
                LEFT JOIN matter_counts mc ON c.banana = mc.banana
                LEFT JOIN item_counts ic ON c.banana = ic.banana
                LEFT JOIN meeting_counts mtg ON c.banana = mtg.banana
                LEFT JOIN synced_counts sc ON c.banana = sc.banana
                WHERE c.geom IS NOT NULL
                  AND NOT (COALESCE(mc.cnt, 0) = 0
                           AND COALESCE(ic.cnt, 0) = 0
                           AND COALESCE(mtg.cnt, 0) = 0
                           AND COALESCE(sc.cnt, 0) = 0)
            """)

        stats = {
            row["banana"]: {"t": row["coverage_type"], "c": row["summary_count"]}
            for row in rows
        }

        return JSONResponse(
            content=stats,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception as e:
        logger.error("map stats endpoint failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to fetch map stats")


@router.get("/api/health")
async def health_check(db: Database = Depends(get_db)):
    """Health check endpoint"""
    health_status: Dict[str, Any] = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0.0",
        "checks": {},
    }

    try:
        # Database health check (async PostgreSQL)
        async with db.pool.acquire() as conn:
            await conn.fetchrow("SELECT 1")

        stats = await db.get_stats()
        health_status["checks"]["databases"] = {
            "status": "healthy",
            "cities": stats["active_cities"],
            "meetings": stats["total_meetings"],
        }

        # Queue health check (detect backlog)
        queue_stats = await db.get_queue_stats()
        pending_count = queue_stats.get("pending_count", 0)
        dead_letter_count = queue_stats.get("dead_letter_count", 0)

        queue_status = "healthy"
        if pending_count > 10000:
            queue_status = "backlogged"
            health_status["status"] = "degraded"
        elif dead_letter_count > 50:
            queue_status = "degraded"
            health_status["status"] = "degraded"

        health_status["checks"]["queue"] = {
            "status": queue_status,
            "pending": pending_count,
            "dead_letter": dead_letter_count,
        }

        # Add basic stats
        health_status["checks"]["data_summary"] = {
            "cities": stats["active_cities"],
            "meetings": stats["total_meetings"],
            "processed": stats["summarized_meetings"],
        }
    except Exception as e:
        health_status["checks"]["databases"] = {"status": "unhealthy", "error": str(e)}
        health_status["status"] = "unhealthy"

    # LLM analyzer check
    health_status["checks"]["llm_analyzer"] = {
        "status": "available" if config.get_api_key() else "disabled",
        "has_api_key": bool(config.get_api_key()),
    }

    # Configuration check
    health_status["checks"]["configuration"] = {
        "status": "healthy",
        "is_development": config.is_development(),
        "rate_limiting": f"{config.RATE_LIMIT_REQUESTS} req/{config.RATE_LIMIT_WINDOW}s",
        "background_processing": config.BACKGROUND_PROCESSING,
    }

    # Background processor check (separate service)
    health_status["checks"]["background_processor"] = {
        "status": "separate_service",
        "note": "Background processing runs as independent daemon",
        "check_command": "systemctl status engagic-daemon",
    }

    # Set overall status based on critical services
    if health_status["checks"]["databases"].get("overall_status") == "error":
        health_status["status"] = "unhealthy"

    return health_status


@router.get("/api/stats")
async def get_stats(db: Database = Depends(get_db)):
    """Get system statistics"""
    try:
        stats = await db.get_stats()

        return {
            "status": "healthy",
            "active_cities": stats.get("active_cities", 0),
            "total_meetings": stats.get("total_meetings", 0),
            "summarized_meetings": stats.get("summarized_meetings", 0),
            "pending_meetings": stats.get("pending_meetings", 0),
            "summary_rate": stats.get("summary_rate", "0%"),
            "background_processing": {
                "service_status": "separate_daemon",
                "note": "Check daemon status: systemctl status engagic-daemon",
            },
        }
    except Exception as e:
        logger.error("error fetching stats", error=str(e))
        raise HTTPException(
            status_code=500, detail="We humbly thank you for your patience"
        )


@router.get("/api/platform-metrics")
async def get_platform_metrics(db: Database = Depends(get_db)):
    """Get comprehensive platform metrics for impact/about page."""
    try:
        metrics = await db.get_platform_metrics()
        return {
            "status": "ok",
            "content": {
                "total_cities": metrics["total_cities"],
                "active_cities": metrics["active_cities"],
                "meetings": metrics["meetings"],
                "agenda_items": metrics["agenda_items"],
                "matters": metrics["matters"],
                "matter_appearances": metrics["matter_appearances"],
            },
            "civic_infrastructure": {
                "committees": metrics["committees"],
                "council_members": metrics["council_members"],
                "committee_assignments": metrics["committee_assignments"],
            },
            "accountability": {
                "votes": metrics["votes"],
                "sponsorships": metrics["sponsorships"],
                "cities_with_votes": metrics["cities_with_votes"],
                "officials_with_votes": metrics["officials_with_votes"],
                "votes_by_city": metrics["votes_by_city"],
            },
            "processing": {
                "summarized_meetings": metrics["summarized_meetings"],
                "summarized_items": metrics["summarized_items"],
                "filtered_items": metrics["filtered_items"],
                "items_analyzed": metrics["items_analyzed"],
                "meeting_summary_rate": metrics["meeting_summary_rate"],
                "item_summary_rate": metrics["item_summary_rate"],
            },
            "growth": {
                "meetings_30d": metrics["meetings_30d"],
                "items_30d": metrics["items_30d"],
                "matters_30d": metrics["matters_30d"],
                "votes_30d": metrics["votes_30d"],
                # Summarized meetings whose date fell in the last 30 days.
                "meeting_summaries_30d": metrics["meeting_summaries_30d"],
            },
            "trends": metrics["trends"],
        }
    except Exception as e:
        logger.error("error fetching platform metrics", error=str(e))
        raise HTTPException(status_code=500, detail="Error fetching metrics")


@router.get("/api/queue-stats")
async def get_queue_stats(db: Database = Depends(get_db)):
    """Get processing queue statistics (Phase 4)"""
    try:
        queue_stats = await db.get_queue_stats()

        return {
            "status": "healthy",
            "queue": {
                "pending": queue_stats.get("pending_count", 0),
                "processing": queue_stats.get("processing_count", 0),
                "completed": queue_stats.get("completed_count", 0),
                "failed": queue_stats.get("failed_count", 0),
                "dead_letter": queue_stats.get("dead_letter_count", 0),
                "avg_processing_seconds": round(
                    queue_stats.get("avg_processing_seconds", 0), 2
                ),
            },
            "note": "Queue is processed continuously by background daemon. Failed jobs retry 3 times before moving to dead_letter.",
        }
    except Exception as e:
        logger.error("error fetching queue stats", error=str(e))
        raise HTTPException(status_code=500, detail="Error fetching queue statistics")


@router.get("/api/metrics")
async def get_metrics(db: Database = Depends(get_db)):
    """Basic metrics endpoint for monitoring"""
    try:
        stats = await db.get_stats()

        return {
            "timestamp": datetime.now().isoformat(),
            "database": {
                "active_cities": stats.get("active_cities", 0),
                "total_meetings": stats.get("total_meetings", 0),
                "summarized_meetings": stats.get("summarized_meetings", 0),
                "pending_meetings": stats.get("pending_meetings", 0),
            },
            "configuration": {
                "rate_limit_window": config.RATE_LIMIT_WINDOW,
                "rate_limit_requests": config.RATE_LIMIT_REQUESTS,
                "background_processing": config.BACKGROUND_PROCESSING,
            },
        }
    except Exception as e:
        logger.error("metrics endpoint failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="We humbly thank you for your patience"
        )


@router.get("/metrics")
async def prometheus_metrics(db: Database = Depends(get_db)):
    """Prometheus metrics endpoint

    Returns metrics in Prometheus text format for scraping.
    Updated with real-time queue statistics.
    """
    try:
        # Update queue size gauges with current stats
        queue_stats = await db.get_queue_stats()
        metrics.update_queue_sizes(queue_stats)

        # Return Prometheus text format
        return Response(content=get_metrics_text(), media_type="text/plain")
    except Exception as e:
        logger.error("prometheus metrics endpoint failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to generate metrics")


@router.get("/api/analytics")
async def get_analytics(db: Database = Depends(get_db)):
    """Get public dashboard analytics from the shared metrics snapshot.

    Analytics and /api/platform-metrics used to run separate whole-table scans
    even though they expose the same underlying facts. Sharing the database
    layer's single-flight cache makes concurrent SSR calls pay for one snapshot.
    """
    try:
        metrics = await db.get_platform_metrics()

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "real_metrics": {
                "cities_covered": metrics["total_cities"],
                "active_cities": metrics["active_cities"],
                "by_type": {
                    "total": {
                        "city": metrics["total_cities_only"],
                        "county": metrics["total_counties"],
                        "school_district": metrics["total_school_districts"],
                    },
                    "active": {
                        "city": metrics["active_cities_only"],
                        "county": metrics["active_counties"],
                        "school_district": metrics["active_school_districts"],
                    },
                    "live": {
                        "city": metrics["live_cities"],
                        "county": metrics["live_counties"],
                        "school_district": metrics["live_school_districts"],
                    },
                },
                "live_jurisdictions": metrics["live_jurisdictions_total"],
                "frequently_updated_cities": metrics["frequently_updated"],
                "frequently_updated_population": metrics["frequently_updated_pop"],
                "meetings_tracked": metrics["meetings"],
                "meetings_with_summary": metrics["live_meetings"],
                "meetings_with_items": metrics["meetings_with_items"],
                "meetings_with_packet": metrics["packets_count"],
                "agendas_summarized": metrics["summaries_count"],
                "agenda_items_processed": metrics["agenda_items"],
                "matters_tracked": metrics["matters"],
                "unique_item_summaries": (
                    metrics["matters_with_summary"] + metrics["standalone_items"]
                ),
                "population_total": metrics["total_pop"],
                "population_with_data": metrics["pop_with_data"],
                "population_with_summaries": metrics["pop_with_summaries"],
            },
        }

    except Exception as e:
        logger.error("analytics endpoint failed", error=str(e))
        raise HTTPException(
            status_code=500, detail="We humbly thank you for your patience"
        )


@router.get("/api/city-coverage")
async def get_city_coverage(db: Database = Depends(get_db)):
    """Get city coverage breakdown: name, coverage type, summary count, population"""
    try:
        async with db.pool.acquire() as conn:
            # Determine coverage type and count summaries per city:
            # - matter: count city_matters with canonical_summary
            # - item: count items with summary
            # - monolithic: count meetings with summary
            # Use pre-aggregated CTEs + JOINs instead of correlated subqueries (O(3) vs O(3n))
            rows = await conn.fetch("""
                WITH
                    matter_counts AS (
                        SELECT banana, COUNT(*) AS cnt
                        FROM city_matters
                        WHERE canonical_summary IS NOT NULL AND canonical_summary != ''
                        GROUP BY banana
                    ),
                    item_counts AS (
                        SELECT m.banana, COUNT(*) AS cnt
                        FROM items i
                        JOIN meetings m ON i.meeting_id = m.id
                        WHERE i.summary IS NOT NULL AND i.summary != ''
                          AND (i.matter_id IS NULL OR i.matter_id NOT IN (
                              SELECT id FROM city_matters
                              WHERE canonical_summary IS NOT NULL AND canonical_summary != ''
                          ))
                        GROUP BY m.banana
                    ),
                    meeting_counts AS (
                        SELECT banana, COUNT(*) AS cnt
                        FROM meetings
                        WHERE summary IS NOT NULL AND summary != ''
                        GROUP BY banana
                    ),
                    synced_counts AS (
                        SELECT banana, COUNT(*) AS cnt
                        FROM meetings
                        WHERE title IS NOT NULL AND title != ''
                          AND date IS NOT NULL
                        GROUP BY banana
                    )
                SELECT
                    c.name,
                    c.state,
                    c.type AS jurisdiction_type,
                    COALESCE(c.population, 0) AS population,
                    CASE
                        WHEN COALESCE(mc.cnt, 0) > 0 THEN 'matter'
                        WHEN COALESCE(ic.cnt, 0) > 0 THEN 'item'
                        WHEN COALESCE(mtg.cnt, 0) > 0 THEN 'monolithic'
                        WHEN COALESCE(sc.cnt, 0) > 0 THEN 'synced'
                        ELSE 'pending'
                    END AS coverage_type,
                    CASE
                        WHEN COALESCE(mc.cnt, 0) > 0 THEN mc.cnt + COALESCE(ic.cnt, 0)
                        WHEN COALESCE(ic.cnt, 0) > 0 THEN ic.cnt
                        WHEN COALESCE(mtg.cnt, 0) > 0 THEN mtg.cnt
                        WHEN COALESCE(sc.cnt, 0) > 0 THEN sc.cnt
                        ELSE 0
                    END AS summary_count
                FROM jurisdictions c
                JOIN synced_counts sc ON c.banana = sc.banana
                LEFT JOIN matter_counts mc ON c.banana = mc.banana
                LEFT JOIN item_counts ic ON c.banana = ic.banana
                LEFT JOIN meeting_counts mtg ON c.banana = mtg.banana
                ORDER BY c.population DESC NULLS LAST
            """)

            cities = [
                {
                    "name": row["name"],
                    "state": row["state"],
                    "type": row["jurisdiction_type"],
                    "population": row["population"],
                    "coverage_type": row["coverage_type"],
                    "summary_count": row["summary_count"],
                }
                for row in rows
            ]

            # Summary counts
            summary = {
                "matter": sum(1 for c in cities if c["coverage_type"] == "matter"),
                "item": sum(1 for c in cities if c["coverage_type"] == "item"),
                "monolithic": sum(1 for c in cities if c["coverage_type"] == "monolithic"),
                "synced": sum(1 for c in cities if c["coverage_type"] == "synced"),
                "total": len(cities),
                "by_type": {
                    "city": sum(1 for c in cities if c["type"] == "city"),
                    "county": sum(1 for c in cities if c["type"] == "county"),
                    "school_district": sum(1 for c in cities if c["type"] == "school_district"),
                },
            }

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
            "cities": cities,
        }

    except Exception as e:
        logger.error("city coverage endpoint failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to fetch city coverage")


@router.get("/api/civic-infrastructure/cities")
async def get_civic_infrastructure_by_city(db: Database = Depends(get_db)):
    """Get per-city breakdown of civic infrastructure data (council members, committees)."""
    try:
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("""
                WITH
                    council_counts AS (
                        SELECT banana,
                               COUNT(*) AS council_member_count,
                               COALESCE(SUM(vote_count), 0) AS vote_count
                        FROM council_members
                        GROUP BY banana
                    ),
                    committee_counts AS (
                        SELECT banana,
                               COUNT(*) AS committee_count
                        FROM committees
                        GROUP BY banana
                    ),
                    assignment_counts AS (
                        SELECT c.banana,
                               COUNT(*) AS assignment_count
                        FROM committee_members cm
                        JOIN committees c ON cm.committee_id = c.id
                        GROUP BY c.banana
                    )
                SELECT
                    ci.banana,
                    ci.name AS city_name,
                    ci.state,
                    COALESCE(ci.population, 0) AS population,
                    COALESCE(cc.council_member_count, 0) AS council_member_count,
                    COALESCE(cc.vote_count, 0) AS vote_count,
                    COALESCE(cmt.committee_count, 0) AS committee_count,
                    COALESCE(ac.assignment_count, 0) AS assignment_count
                FROM jurisdictions ci
                LEFT JOIN council_counts cc ON ci.banana = cc.banana
                LEFT JOIN committee_counts cmt ON ci.banana = cmt.banana
                LEFT JOIN assignment_counts ac ON ci.banana = ac.banana
                WHERE cc.council_member_count > 0 OR cmt.committee_count > 0
                ORDER BY ci.population DESC NULLS LAST
            """)

            cities = [
                {
                    "banana": row["banana"],
                    "city_name": row["city_name"],
                    "state": row["state"],
                    "population": row["population"],
                    "council_member_count": row["council_member_count"],
                    "vote_count": row["vote_count"],
                    "committee_count": row["committee_count"],
                    "assignment_count": row["assignment_count"],
                }
                for row in rows
            ]

            totals = {
                "cities_with_council_members": sum(1 for c in cities if c["council_member_count"] > 0),
                "cities_with_committees": sum(1 for c in cities if c["committee_count"] > 0),
                "total_council_members": sum(c["council_member_count"] for c in cities),
                "total_votes": sum(c["vote_count"] for c in cities),
                "total_committees": sum(c["committee_count"] for c in cities),
                "total_assignments": sum(c["assignment_count"] for c in cities),
            }

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "cities": cities,
            "totals": totals,
        }

    except Exception as e:
        logger.error("civic infrastructure endpoint failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to fetch civic infrastructure")


@router.get("/api/extraction-scorecard")
async def get_extraction_scorecard(db: Database = Depends(get_db)):
    """Per-vendor extraction health from the persisted chunk/html audits.

    The cross-vendor machine-readability ranking: which platforms publish
    structured agendas and which publish blobs. All numbers fall out of
    queue.processing_metadata — no new instrumentation.
    """
    try:
        async with db.pool.acquire() as conn:
            vendor_rows = await conn.fetch("""
                SELECT j.vendor,
                       COUNT(DISTINCT q.banana) AS cities,
                       COUNT(*) AS chunk_runs,
                       COUNT(*) FILTER (
                           WHERE q.processing_metadata->'chunk'->>'winning_rung' IS NOT NULL
                       ) AS wins,
                       SUM(COALESCE((q.processing_metadata->'chunk'->'quality'->>'matter_files')::int, 0))
                           AS matter_files_captured,
                       SUM(COALESCE((q.processing_metadata->'chunk'->'quality'->>'repaired_titles')::int, 0))
                           AS titles_repaired,
                       COUNT(*) FILTER (
                           WHERE q.processing_metadata->'chunk'->'quality'->>'seg_smell' = 'under_split'
                       ) AS under_split
                FROM queue q
                JOIN jurisdictions j USING (banana)
                WHERE q.processing_metadata ? 'chunk'
                GROUP BY j.vendor
                ORDER BY chunk_runs DESC
            """)

            failure_rows = await conn.fetch("""
                SELECT j.vendor,
                       q.processing_metadata->'chunk'->>'failure_reason' AS reason,
                       COUNT(*) AS cnt
                FROM queue q
                JOIN jurisdictions j USING (banana)
                WHERE q.processing_metadata->'chunk'->>'failure_reason' IS NOT NULL
                GROUP BY j.vendor, reason
            """)

            html_rows = await conn.fetch("""
                SELECT j.vendor,
                       q.processing_metadata->'html'->>'pattern' AS pattern,
                       COUNT(*) AS cnt
                FROM queue q
                JOIN jurisdictions j USING (banana)
                WHERE q.processing_metadata->'html'->>'pattern' IS NOT NULL
                GROUP BY j.vendor, pattern
            """)

        failures: Dict[str, Dict[str, int]] = {}
        for r in failure_rows:
            failures.setdefault(r["vendor"], {})[r["reason"]] = r["cnt"]
        html_patterns: Dict[str, Dict[str, int]] = {}
        for r in html_rows:
            html_patterns.setdefault(r["vendor"], {})[r["pattern"]] = r["cnt"]

        vendors = []
        for r in vendor_rows:
            runs = r["chunk_runs"] or 0
            vendors.append({
                "vendor": r["vendor"],
                "cities": r["cities"],
                "chunk_runs": runs,
                "wins": r["wins"],
                "win_rate": round(r["wins"] / runs, 3) if runs else None,
                "failure_reasons": failures.get(r["vendor"], {}),
                "html_patterns": html_patterns.get(r["vendor"], {}),
                "matter_files_captured": r["matter_files_captured"],
                "titles_repaired": r["titles_repaired"],
                "under_split_meetings": r["under_split"],
            })

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "vendors": vendors,
        }

    except Exception as e:
        logger.error("extraction scorecard failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to build extraction scorecard")


@router.get("/api/extraction-drift")
async def get_extraction_drift(db: Database = Depends(get_db)):
    """Cities whose extraction shape just changed — redesign detection
    before silent breakage.

    Compares each city's two most recent audits: html dialect pattern, and
    winning chunk rung per ladder. A smell list, not an alarm — committees
    within one city can legitimately alternate shapes.
    """
    try:
        async with db.pool.acquire() as conn:
            html_drift = await conn.fetch("""
                WITH ranked AS (
                    SELECT j.vendor, j.slug, q.banana,
                           q.processing_metadata->'html'->>'pattern' AS pattern,
                           q.created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY q.banana ORDER BY q.created_at DESC
                           ) AS rn
                    FROM queue q
                    JOIN jurisdictions j USING (banana)
                    WHERE q.processing_metadata->'html'->>'pattern' IS NOT NULL
                )
                SELECT a.vendor, a.slug, a.banana,
                       b.pattern AS previous, a.pattern AS latest,
                       a.created_at AS seen_at
                FROM ranked a
                JOIN ranked b ON a.banana = b.banana AND b.rn = 2
                WHERE a.rn = 1 AND a.pattern IS DISTINCT FROM b.pattern
                ORDER BY a.created_at DESC
            """)

            rung_drift = await conn.fetch("""
                WITH ranked AS (
                    SELECT j.vendor, j.slug, q.banana,
                           q.processing_metadata->'chunk'->>'winning_ladder' AS ladder,
                           q.processing_metadata->'chunk'->>'winning_rung' AS rung,
                           q.created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY q.banana,
                                            q.processing_metadata->'chunk'->>'winning_ladder'
                               ORDER BY q.created_at DESC
                           ) AS rn
                    FROM queue q
                    JOIN jurisdictions j USING (banana)
                    WHERE q.processing_metadata->'chunk'->>'winning_rung' IS NOT NULL
                )
                SELECT a.vendor, a.slug, a.banana, a.ladder,
                       b.rung AS previous, a.rung AS latest,
                       a.created_at AS seen_at
                FROM ranked a
                JOIN ranked b ON a.banana = b.banana AND a.ladder = b.ladder AND b.rn = 2
                WHERE a.rn = 1 AND a.rung IS DISTINCT FROM b.rung
                ORDER BY a.created_at DESC
            """)

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "html_pattern_drift": [dict(r) for r in html_drift],
            "winning_rung_drift": [dict(r) for r in rung_drift],
        }

    except Exception as e:
        logger.error("extraction drift endpoint failed", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to compute extraction drift")
