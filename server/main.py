"""
Engagic API Server

Clean, modular FastAPI application with separation of concerns.
Routes, services, and utilities are organized into focused modules.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

import stripe
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from config import config, get_logger
from database.db_postgres import Database
from server.rate_limiter import SQLiteRateLimiter
from server.middleware.logging import log_requests
from server.middleware.metrics import metrics_middleware
from server.middleware.request_id import RequestIDMiddleware
from server.routes import search, meetings, topics, admin, monitoring, flyer, matters, donate, auth, dashboard, votes, engagement, feedback, committees
from server.routes import deliberation, happening, events, turnstile
try:
    from server.routes import duckling
except ImportError:
    duckling = None
from userland.auth import init_jwt

logger = get_logger(__name__)

# Configure structured logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(config.LOG_PATH, mode="a")],
)


# Lifespan context manager for database initialization
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup async database connection pool"""
    # Startup: Create PostgreSQL connection pool
    db = await Database.create()
    logger.info("initialized PostgreSQL database with async connection pool")

    # Store in app state
    app.state.db = db

    # Populate the shared analytics/platform snapshot before the first visitor
    # needs it. This runs in the background so health checks and deploy startup
    # are not held hostage by an aggregate refresh.
    async def warm_public_metrics() -> None:
        try:
            await db.get_platform_metrics()
            logger.info("warmed public metrics cache")
        except Exception as exc:
            logger.warning("failed to warm public metrics cache", error=str(exc))

    metrics_warm_task = asyncio.create_task(warm_public_metrics())

    yield

    # Shutdown: Close connection pool
    try:
        if not metrics_warm_task.done():
            metrics_warm_task.cancel()
            with suppress(asyncio.CancelledError):
                await metrics_warm_task

        # Log connection count before closing
        active_connections = db.pool.get_size()
        logger.info(
            "closing connection pool",
            active_connections=active_connections,
            min_size=db.pool.get_min_size(),
            max_size=db.pool.get_max_size(),
        )

        await db.close()
        logger.info("closed PostgreSQL connection pool")

        # Warn if connections were active during shutdown (potential leaks)
        if active_connections > 0:
            logger.warning(
                "connection pool had active connections on shutdown",
                count=active_connections,
            )

    except Exception as e:
        # Don't crash on shutdown - log and continue
        logger.error("error closing connection pool", error=str(e), exc_info=True)
        # Don't re-raise - allow shutdown to proceed gracefully


# Initialize FastAPI app with lifespan
app = FastAPI(title="engagic API", description="EGMI", lifespan=lifespan)

# CORS configuration - explicit methods and headers for security
app.add_middleware(  # type: ignore[arg-type]
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID", "X-Turnstile-Token"],
    allow_credentials=True,
)

# Request ID middleware (must be early in stack for tracing)
app.add_middleware(RequestIDMiddleware)  # type: ignore[arg-type]

# GZip compression for responses > 500 bytes (significant bandwidth savings)
app.add_middleware(GZipMiddleware, minimum_size=500)

# Initialize global instances (non-async)
rate_limiter = SQLiteRateLimiter(
    db_path=os.path.join(config.DB_DIR, "rate_limits.db"),
    requests_limit=config.RATE_LIMIT_REQUESTS,
    window_seconds=config.RATE_LIMIT_WINDOW,
)

# Initialize Stripe at app startup (not per-request)
if config.STRIPE_SECRET_KEY:
    stripe.api_key = config.STRIPE_SECRET_KEY
    logger.info("Stripe payment processing initialized")
else:
    logger.warning("STRIPE_SECRET_KEY not set - payment features disabled")

# Initialize JWT for authentication
if not config.USERLAND_JWT_SECRET:
    logger.warning("WARNING: USERLAND_JWT_SECRET not set. Auth features will not work.")
    logger.warning("Generate with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
else:
    init_jwt(config.USERLAND_JWT_SECRET)
    logger.info("JWT authentication initialized")


# Register middleware. FastAPI/Starlette: last registered wraps outermost,
# so the request flows outer -> inner from the bottom of this block upward.
#
# Desired request flow:
#   log_requests -> metrics -> rate_limit -> turnstile -> endpoint
#
# log_requests and metrics MUST wrap rate_limit/turnstile so that early 403/429
# responses from those middlewares still show up in the access log and in
# Prometheus counters. Previously logging was innermost, which silently
# dropped every rate-limited request from the access log.
@app.middleware("http")
async def turnstile_middleware_wrapper(request, call_next):
    from server.middleware.turnstile import turnstile_middleware
    return await turnstile_middleware(request, call_next)


@app.middleware("http")
async def rate_limit_middleware_wrapper(request, call_next):
    from server.middleware.rate_limiting import rate_limit_middleware
    return await rate_limit_middleware(request, call_next, rate_limiter)


@app.middleware("http")
async def metrics_middleware_wrapper(request, call_next):
    return await metrics_middleware(request, call_next)


@app.middleware("http")
async def log_requests_middleware(request, call_next):
    return await log_requests(request, call_next)


# Mount routers (duckling first -- must match before parametric routes)
if duckling is not None:
    app.include_router(duckling.router)
app.include_router(monitoring.router)  # Root and monitoring endpoints
app.include_router(search.router)      # Search endpoints
app.include_router(meetings.router)    # Meeting endpoints
app.include_router(topics.router)      # Topic endpoints
app.include_router(admin.router)       # Admin endpoints
app.include_router(flyer.router)       # Flyer generation endpoints
app.include_router(matters.router)     # Matter timeline and tracking endpoints
app.include_router(votes.router)       # Votes and council member endpoints
app.include_router(committees.router)  # Committee and membership endpoints
app.include_router(engagement.router)  # User engagement (watches, trending)
app.include_router(feedback.router)    # User feedback (ratings, issues)
app.include_router(donate.router)      # Donation and payment endpoints
app.include_router(auth.router)        # Authentication endpoints (userland)
app.include_router(dashboard.router)   # User dashboard and alerts (userland)
app.include_router(deliberation.router)  # Community deliberation and opinion clustering
app.include_router(happening.router)     # Happening This Week (Claude-analyzed important items)
app.include_router(events.router)        # Frontend analytics events
app.include_router(turnstile.router)     # Turnstile bot verification


if __name__ == "__main__":
    import uvicorn
    import sys

    # Validate critical environment variables on startup
    if not config.get_api_key():
        logger.warning(
            "WARNING: No LLM API key configured. AI features will be disabled."
        )
        logger.warning("Set ANTHROPIC_API_KEY or LLM_API_KEY to enable AI summaries.")

    if not config.ADMIN_TOKEN:
        logger.warning(
            "WARNING: No admin token configured. Admin endpoints will not work."
        )
        logger.warning("Set ENGAGIC_ADMIN_TOKEN to enable admin functionality.")

    logger.info("Starting engagic API server...")
    logger.info("configuration", config_summary=config.summary())

    # Handle command line arguments
    if len(sys.argv) > 1 and sys.argv[1] == "--init-db":
        logger.info("Initializing databases...")
        import asyncio
        from database.db_postgres import Database

        async def init_db():
            db = await Database.create()
            try:
                _ = await db.get_stats()
                logger.info("Database initialized successfully")
            finally:
                await db.close()

        asyncio.run(init_db())
        sys.exit(0)

    uvicorn.run(
        app,
        host=config.API_HOST,
        port=config.API_PORT,
        access_log=False,  # Disable default uvicorn logs (we have custom middleware logging)
    )
