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
    """Get comprehensive analytics for public dashboard"""
    try:
        # Consolidated query: combine simple counts into single round-trip
        async with db.pool.acquire() as conn:
            # Single query for all basic counts (scalar subqueries execute in parallel)
            counts = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM jurisdictions) as total_cities,
                    (SELECT COUNT(DISTINCT banana) FROM meetings) as active_cities,
                    (SELECT COUNT(*) FROM jurisdictions WHERE type = 'city') as total_cities_only,
                    (SELECT COUNT(*) FROM jurisdictions WHERE type = 'county') as total_counties,
                    (SELECT COUNT(*) FROM jurisdictions WHERE type = 'school_district') as total_school_districts,
                    (SELECT COUNT(DISTINCT m.banana) FROM meetings m JOIN jurisdictions j ON m.banana = j.banana WHERE j.type = 'city') as active_cities_only,
                    (SELECT COUNT(DISTINCT m.banana) FROM meetings m JOIN jurisdictions j ON m.banana = j.banana WHERE j.type = 'county') as active_counties,
                    (SELECT COUNT(DISTINCT m.banana) FROM meetings m JOIN jurisdictions j ON m.banana = j.banana WHERE j.type = 'school_district') as active_school_districts,
                    (SELECT COUNT(*) FROM meetings) as meetings_count,
                    (SELECT COUNT(DISTINCT meeting_id) FROM items) as meetings_with_items,
                    (SELECT COUNT(*) FROM meetings WHERE packet_url IS NOT NULL AND packet_url != '') as packets_count,
                    (SELECT COUNT(*) FROM meetings WHERE summary IS NOT NULL AND summary != '') as summaries_count,
                    (SELECT COUNT(*) FROM items) as items_count,
                    (SELECT COUNT(*) FROM city_matters) as matters_count,
                    (SELECT COUNT(*) FROM city_matters WHERE canonical_summary IS NOT NULL AND canonical_summary != '') as matters_with_summary,
                    (SELECT COUNT(*) FROM items WHERE matter_id IS NULL AND summary IS NOT NULL AND summary != '') as standalone_items
            """)

            unique_summaries = counts["matters_with_summary"] + counts["standalone_items"]

            # Complex aggregations that need CTEs - batch together
            # "Live" / "switched on" = has a summary at meeting, item, or matter level.
            # We surface live counts so dashboards reflect coverage with content,
            # not naive table totals (empty shells skew the picture).
            complex_stats = await conn.fetchrow("""
                WITH
                    summarized_meetings AS (
                        SELECT DISTINCT m.id, m.banana
                        FROM meetings m
                        LEFT JOIN items i ON i.meeting_id = m.id
                        LEFT JOIN city_matters cm ON cm.id = i.matter_id
                        WHERE (m.summary IS NOT NULL AND m.summary != '')
                           OR (i.summary IS NOT NULL AND i.summary != '')
                           OR (cm.canonical_summary IS NOT NULL AND cm.canonical_summary != '')
                    ),
                    summarized_bananas AS (
                        SELECT banana FROM summarized_meetings
                        UNION
                        SELECT banana FROM city_matters
                        WHERE canonical_summary IS NOT NULL AND canonical_summary != ''
                    ),
                    live_jurisdictions AS (
                        SELECT j.banana, j.type, j.population
                        FROM jurisdictions j
                        JOIN summarized_bananas sb ON sb.banana = j.banana
                    ),
                    frequently_updated AS (
                        SELECT sm.banana, c.population as pop
                        FROM summarized_meetings sm
                        JOIN jurisdictions c ON sm.banana = c.banana
                        GROUP BY sm.banana, c.population
                        HAVING COUNT(*) >= 7
                    ),
                    active_bananas AS (
                        SELECT DISTINCT banana FROM meetings
                    )
                SELECT
                    (SELECT COUNT(*) FROM summarized_meetings) as live_meetings,
                    (SELECT COUNT(*) FROM live_jurisdictions WHERE type = 'city') as live_cities,
                    (SELECT COUNT(*) FROM live_jurisdictions WHERE type = 'county') as live_counties,
                    (SELECT COUNT(*) FROM live_jurisdictions WHERE type = 'school_district') as live_school_districts,
                    (SELECT COUNT(*) FROM live_jurisdictions) as live_jurisdictions_total,
                    (SELECT COUNT(*) FROM frequently_updated) as frequently_updated,
                    (SELECT COALESCE(SUM(pop), 0) FROM frequently_updated) as frequently_updated_pop,
                    (SELECT COALESCE(SUM(population), 0) FROM jurisdictions WHERE geom IS NOT NULL) as total_pop,
                    (SELECT COALESCE(SUM(c.population), 0) FROM jurisdictions c JOIN active_bananas ab ON c.banana = ab.banana) as pop_with_data,
                    (SELECT COALESCE(SUM(c.population), 0) FROM jurisdictions c JOIN summarized_bananas sb ON c.banana = sb.banana) as pop_with_summaries
            """)

        return {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "real_metrics": {
                "cities_covered": counts["total_cities"],
                "active_cities": counts["active_cities"],
                "by_type": {
                    "total": {
                        "city": counts["total_cities_only"],
                        "county": counts["total_counties"],
                        "school_district": counts["total_school_districts"],
                    },
                    "active": {
                        "city": counts["active_cities_only"],
                        "county": counts["active_counties"],
                        "school_district": counts["active_school_districts"],
                    },
                    "live": {
                        "city": complex_stats["live_cities"],
                        "county": complex_stats["live_counties"],
                        "school_district": complex_stats["live_school_districts"],
                    },
                },
                "live_jurisdictions": complex_stats["live_jurisdictions_total"],
                "frequently_updated_cities": complex_stats["frequently_updated"],
                "frequently_updated_population": complex_stats["frequently_updated_pop"],
                "meetings_tracked": counts["meetings_count"],
                "meetings_with_summary": complex_stats["live_meetings"],
                "meetings_with_items": counts["meetings_with_items"],
                "meetings_with_packet": counts["packets_count"],
                "agendas_summarized": counts["summaries_count"],
                "agenda_items_processed": counts["items_count"],
                "matters_tracked": counts["matters_count"],
                "unique_item_summaries": unique_summaries,
                "population_total": complex_stats["total_pop"],
                "population_with_data": complex_stats["pop_with_data"],
                "population_with_summaries": complex_stats["pop_with_summaries"],
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
