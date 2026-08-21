import os
import logging
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import structlog

# Auto-load env files from project root before any config reads
_project_root = Path(__file__).parent
_env_file = _project_root / ".env"
if _env_file.exists():
    try:
        load_dotenv(_env_file)
    except OSError:
        # Sandboxed tooling may traverse the repository without permission to
        # read production secrets. Configuration still resolves from the
        # caller's environment; never make imports depend on secret-file ACLs.
        pass
_secrets_file = _project_root / ".llm_secrets"
if _secrets_file.exists():
    try:
        load_dotenv(_secrets_file, override=True)
    except OSError:
        pass

logger = logging.getLogger("engagic")


def get_logger(name: str = "engagic"):
    """Get a structured logger instance

    Usage:
        logger = get_logger(__name__)
        logger = logger.bind(component="vendor", vendor="legistar")
        logger.info("fetching meetings", days_back=7, mode="api")

    Args:
        name: Logger name (typically __name__ or module path)

    Returns:
        Structured logger instance with context binding support
    """
    return structlog.get_logger(name)


class Config:
    """Configuration management for engagic"""

    def __init__(self):
        # Database configuration - PostgreSQL only (SQLite removed Nov 2025)
        # Data directory still used for rate limiter SQLite, logs, etc.
        vps_path = "/opt/engagic/data"
        local_path = os.path.join(os.getcwd(), "data")
        default_data_dir = vps_path if os.path.exists(vps_path) else local_path
        self.DB_DIR = os.getenv("ENGAGIC_DB_DIR", default_data_dir)

        # PostgreSQL configuration (production database - default enabled)
        self.USE_POSTGRES = os.getenv("ENGAGIC_USE_POSTGRES", "true").lower() == "true"
        self.POSTGRES_HOST = os.getenv("ENGAGIC_POSTGRES_HOST", "localhost")
        self.POSTGRES_PORT = int(os.getenv("ENGAGIC_POSTGRES_PORT", "5432"))
        self.POSTGRES_DB = os.getenv("ENGAGIC_POSTGRES_DB", "engagic")
        self.POSTGRES_USER = os.getenv("ENGAGIC_POSTGRES_USER", "engagic")
        self.POSTGRES_PASSWORD = os.getenv("ENGAGIC_POSTGRES_PASSWORD", "")
        self.POSTGRES_POOL_MIN_SIZE = int(os.getenv("ENGAGIC_POSTGRES_POOL_MIN_SIZE", "2"))
        self.POSTGRES_POOL_MAX_SIZE = int(os.getenv("ENGAGIC_POSTGRES_POOL_MAX_SIZE", "10"))

        # Userland authentication
        self.USERLAND_JWT_SECRET = os.getenv("USERLAND_JWT_SECRET")

        # SSR authentication (prevents X-Forwarded-User-IP spoofing)
        self.SSR_AUTH_SECRET = os.getenv("SSR_AUTH_SECRET")

        # Default log path to repo-relative
        default_log_path = os.path.join(os.getcwd(), "engagic.log")
        self.LOG_PATH = os.getenv("ENGAGIC_LOG_PATH", default_log_path)

        # API configuration
        self.API_HOST = os.getenv("ENGAGIC_HOST", "0.0.0.0")
        self.API_PORT = int(os.getenv("ENGAGIC_PORT", "8000"))
        self.DEBUG = os.getenv("ENGAGIC_DEBUG", "false").lower() == "true"

        # Rate limiting
        self.RATE_LIMIT_REQUESTS = int(os.getenv("ENGAGIC_RATE_LIMIT_REQUESTS", "30"))
        self.RATE_LIMIT_WINDOW = int(os.getenv("ENGAGIC_RATE_LIMIT_WINDOW", "60"))
        self.MAX_QUERY_LENGTH = int(os.getenv("ENGAGIC_MAX_QUERY_LENGTH", "200"))

        # External APIs
        self.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
        self.GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # Google Gemini API
        self.LLM_API_KEY = os.getenv("LLM_API_KEY")  # Fallback

        # LLM concurrency for batch item processing (default 15 concurrent items)
        # Flash Lite: 4K RPM / 4M TPM - plenty of headroom for parallel calls
        self.LLM_CONCURRENCY = int(os.getenv("ENGAGIC_LLM_CONCURRENCY", "15"))

        # Queue job concurrency: how many jobs process in parallel (default 6).
        # Lowered to 3 after 2026-04-10 OOM, then bumped to 6 on 2026-05-20 after
        # swap was doubled (6Gi -> 13Gi) and PDF semaphore went 6 -> 8. Combined
        # with city_concurrency=3, peak goes from 9 to 18 concurrent meetings,
        # which is well under the Gemini 4K RPM ceiling. Watch RSS at 6 before
        # pushing higher; the dominant memory cost is in-flight PDF bytes.
        self.JOB_CONCURRENCY = int(os.getenv("ENGAGIC_JOB_CONCURRENCY", "6"))

        # Per-job wall-clock ceiling. Exists to prevent a hung LLM call or
        # aiohttp cleanup from pinning a queue slot indefinitely. PDF subprocess
        # extraction already caps itself at 620s; a meeting with 2-3 timed-out
        # PDFs plus healthy LLM summarization fits comfortably under 25 min.
        # Exceeding this marks the queue row failed and the worker moves on.
        self.JOB_TIMEOUT_SECONDS = int(os.getenv("ENGAGIC_JOB_TIMEOUT_SECONDS", "1500"))

        # Gemini Batch API lane. Meeting jobs whose date falls outside the
        # urgent window [now - PAST_DAYS, now + FUTURE_DAYS] go through the
        # Batch API: 50% token discount AND a separate quota pool from
        # interactive calls, so bulk processing doesn't compete with fresh
        # meetings for TPM. The 1-day urgent window exists for one reason:
        # special meetings only require ~24h posted notice (Brown Act etc.)
        # and batch's worst-case turnaround is 24h -- those summaries must
        # not land after the meeting happened. Everything else batches.
        self.BATCH_API_ENABLED = os.getenv("ENGAGIC_BATCH_API_ENABLED", "true").lower() == "true"
        self.BATCH_URGENT_PAST_DAYS = int(os.getenv("ENGAGIC_BATCH_URGENT_PAST_DAYS", "0"))
        self.BATCH_URGENT_FUTURE_DAYS = int(os.getenv("ENGAGIC_BATCH_URGENT_FUTURE_DAYS", "1"))
        # Batch lane slots are separate from JOB_CONCURRENCY so parked polls
        # never starve the streaming lane.
        self.BATCH_JOB_CONCURRENCY = int(os.getenv("ENGAGIC_BATCH_JOB_CONCURRENCY", "3"))
        # Wall-clock bound for the batch-lane SUBMIT step only. Submission is
        # fire-and-forget (build JSONL, upload, create the job) and returns in
        # seconds-to-minutes; a decoupled collector polls the running job later
        # and never kills it. This timer just stops a wedged upload from pinning
        # a lane slot -- the job's own multi-hour lifetime no longer lives here.
        self.BATCH_JOB_TIMEOUT_SECONDS = int(os.getenv("ENGAGIC_BATCH_JOB_TIMEOUT_SECONDS", "1800"))

        # Ground-truth corpus (docs/CORPUS_ARCHITECTURE.md): original document
        # bytes and extracted text archived to R2, content-addressed by
        # sha256(bytes). Reuses the R2-scoped Cloudflare token the tile deploy
        # already ships in .llm_secrets. CORPUS_ENABLED is the kill switch for
        # every tee/read path; the corpus degrades to off when creds are absent.
        self.CORPUS_ENABLED = os.getenv("ENGAGIC_CORPUS_ENABLED", "true").lower() == "true"
        self.CORPUS_BUCKET = os.getenv("ENGAGIC_CORPUS_BUCKET", "engagic-corpus")
        self.CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID")
        self.CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN")
        # R2 S3 data-plane credentials (<account>.r2.cloudflarestorage.com).
        # Still Cloudflare: derived from the R2-scoped API token (access key =
        # token id, secret = sha256 of the token value). The data plane exists
        # because the REST management API caps at ~1200 req/5min globally --
        # object traffic at pipeline volume belongs on the S3 endpoint.
        self.R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
        self.R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
        # The Cloudflare REST object endpoint caps uploads around 300MB;
        # oversized originals are indexed (hash, size, sources) but not
        # archived until a multipart path exists.
        self.CORPUS_MAX_ORIGINAL_BYTES = int(
            os.getenv("ENGAGIC_CORPUS_MAX_ORIGINAL_BYTES", str(256 * 1024 * 1024))
        )
        # Archived originals are served without origin traffic until this age.
        # Stale identities use conditional requests; failures serve the archived
        # revision and wait before another attempt so an origin outage cannot
        # turn a corpus hit into a request storm.
        self.CORPUS_REVALIDATE_SECONDS = max(
            60,
            int(os.getenv("ENGAGIC_CORPUS_REVALIDATE_SECONDS", str(24 * 60 * 60))),
        )
        self.CORPUS_REVALIDATE_FAILURE_SECONDS = max(
            60,
            int(os.getenv("ENGAGIC_CORPUS_REVALIDATE_FAILURE_SECONDS", "3600")),
        )

        # Morphology classifier suggestions fill the chunker's hint slot for
        # cities with no sticky routing history. Hints only reorder rungs
        # within a ladder (cascade still runs), so worst case = one wasted
        # attempt. Classification itself always runs and lands in the audit.
        self.CHUNKER_CLASSIFIER_HINTS = os.getenv("ENGAGIC_CHUNKER_CLASSIFIER_HINTS", "true").lower() == "true"

        # Sync chunker guard (parsing/subprocess_guard.py). chunk_pdf runs in
        # a resource-capped subprocess: prod telemetry puts the p99 cascade at
        # ~2.2s and the 2026-06-29 freeze at 902s, so 300s bounds the
        # pathological tail (which now includes the ground-truth text pass on
        # monster packets) without ever touching legitimate work. Concurrency
        # caps simultaneous chunker children across all vendors -- sync fans
        # out to CITY_SYNC_CONCURRENCY per vendor, and without a gate a busy
        # sync could spawn dozens of children on a 3.8GB box.
        self.CHUNKER_TIMEOUT_SECONDS = int(os.getenv("ENGAGIC_CHUNKER_TIMEOUT_SECONDS", "300"))
        self.CHUNKER_SUBPROCESS_CONCURRENCY = int(os.getenv("ENGAGIC_CHUNKER_SUBPROCESS_CONCURRENCY", "4"))

        # Where PDF shape gets manufactured. True (default) = adapters chunk
        # at sync, the historical behavior. False = sync only archives bytes
        # to the corpus (stage 1) and stores the meeting's URLs; the processor
        # manufactures items at claim time from corpus bytes via the same
        # produce_ground_truth stage. Sync becomes pure network breadth --
        # chunking-at-sync only ever existed to satisfy the "processing
        # receives perfect shape" contract, which the corpus dissolved.
        self.SYNC_CHUNKING = os.getenv("ENGAGIC_SYNC_CHUNKING", "true").lower() == "true"

        # Payment processing
        self.STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
        self.STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")

        # Email (Mailgun)
        self.MAILGUN_API_KEY = os.getenv("MAILGUN_API_KEY")
        self.MAILGUN_DOMAIN = os.getenv("MAILGUN_DOMAIN")
        self.MAILGUN_FROM_EMAIL = os.getenv(
            "MAILGUN_FROM_EMAIL",
            f"alerts@{self.MAILGUN_DOMAIN}" if self.MAILGUN_DOMAIN else None
        )

        # Cookie settings (userland auth)
        self.COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
        self.COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")

        # Turnstile bot protection
        self.TURNSTILE_SECRET_KEY = os.getenv("TURNSTILE_SECRET_KEY", "")

        # Frontend URL for payment redirects
        self.FRONTEND_URL = os.getenv(
            "ENGAGIC_FRONTEND_URL",
            "https://engagic.org"
        )

        # CORS settings - production origins only (no localhost in production)
        # For local dev, set ENGAGIC_ALLOWED_ORIGINS env var to include localhost
        self.ALLOWED_ORIGINS = self._parse_origins(
            os.getenv(
                "ENGAGIC_ALLOWED_ORIGINS",
                "https://engagic.org,https://www.engagic.org,https://api.engagic.org,"
                "https://engagic.pages.dev,"
                "https://motioncount.com,https://www.motioncount.com,https://api.motioncount.com",
            )
        )

        # Background processing
        self.BACKGROUND_PROCESSING = (
            os.getenv("ENGAGIC_BACKGROUND_PROCESSING", "true").lower() == "true"
        )
        self.SYNC_INTERVAL_HOURS = int(
            os.getenv("ENGAGIC_SYNC_INTERVAL_HOURS", "24")
        )  # daily
        self.PROCESSING_INTERVAL_HOURS = int(
            os.getenv("ENGAGIC_PROCESSING_INTERVAL_HOURS", "2")
        )

        # Vendor HTTP settings
        self.VENDOR_HTTP_TIMEOUT = int(
            os.getenv("ENGAGIC_VENDOR_HTTP_TIMEOUT", "30")
        )

        # Residential SOCKS proxy for Akamai-protected vendors (e.g. socks5://localhost:9050)
        # Used by vendors like visioninternet where datacenter ASNs are blocked
        self.RESIDENTIAL_PROXY = os.getenv("ENGAGIC_RESIDENTIAL_PROXY", "")

        # Logging
        self.LOG_LEVEL = os.getenv("ENGAGIC_LOG_LEVEL", "INFO").upper()
        # Log format: "json" (default for prod) or "dev" (human-readable key=value)
        self.LOG_FORMAT = os.getenv("ENGAGIC_LOG_FORMAT", "json").lower()

        # Model selection: use Flash-Lite for small docs (cost savings) or Flash for all (quality)
        self.USE_FLASH_LITE = os.getenv("ENGAGIC_USE_FLASH_LITE", "true").lower() == "true"

        # Gemini model IDs (overridable via env for A/B or preview swaps)
        # PRIMARY_MODEL is the default workhorse; SMALL_DOC_MODEL kicks in only
        # when USE_FLASH_LITE is true AND the document is below the size cutoff.
        self.PRIMARY_MODEL = os.getenv("ENGAGIC_PRIMARY_MODEL", "gemini-3.1-flash-lite")
        self.SMALL_DOC_MODEL = os.getenv("ENGAGIC_SMALL_DOC_MODEL", "gemini-2.5-flash-lite")

        # Admin authentication
        self.ADMIN_TOKEN = os.getenv("ENGAGIC_ADMIN_TOKEN", "")
        # Whitelist VPS IP for admin access (uses CF-Connecting-IP from Cloudflare)
        self.ADMIN_WHITELIST_IPS = self._parse_whitelist_ips(
            os.getenv(
                "ENGAGIC_ADMIN_WHITELIST_IPS",
                "5.78.189.81"
            )
        )

        # Vendor API tokens
        self.NYC_LEGISTAR_TOKEN = os.getenv("NYC_LEGISTAR_TOKEN", "")

        # Validate configuration
        self._validate()

    def _parse_origins(self, origins_str: str) -> list:
        """Parse comma-separated origins string"""
        if not origins_str:
            return []
        return [origin.strip() for origin in origins_str.split(",") if origin.strip()]

    def _parse_whitelist_ips(self, ips_str: str) -> set:
        """Parse comma-separated IP whitelist string"""
        if not ips_str:
            return set()
        return {ip.strip() for ip in ips_str.split(",") if ip.strip()}

    def _validate(self):
        """Validate configuration values"""
        if self.RATE_LIMIT_REQUESTS <= 0:
            raise ValueError("ENGAGIC_RATE_LIMIT_REQUESTS must be positive")

        if self.RATE_LIMIT_WINDOW <= 0:
            raise ValueError("ENGAGIC_RATE_LIMIT_WINDOW must be positive")

        if self.MAX_QUERY_LENGTH <= 0:
            raise ValueError("ENGAGIC_MAX_QUERY_LENGTH must be positive")

        if self.API_PORT <= 0 or self.API_PORT > 65535:
            raise ValueError("ENGAGIC_PORT must be between 1 and 65535")

        if not any([self.ANTHROPIC_API_KEY, self.GEMINI_API_KEY, self.LLM_API_KEY]):
            logger.warning("No LLM API key configured - AI features will be disabled")

    def get_api_key(self) -> Optional[str]:
        """Get the appropriate API key for LLM services - prioritize Gemini"""
        return self.GEMINI_API_KEY or self.LLM_API_KEY or self.ANTHROPIC_API_KEY

    def get_postgres_dsn(self) -> str:
        """Build PostgreSQL DSN for asyncpg connection

        Returns:
            PostgreSQL connection string (DSN)

        Example:
            postgresql://engagic:password@localhost:5432/engagic
        """
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    def ensure_data_dir(self) -> str:
        """Lazily create data directory if it doesn't exist

        Returns:
            Path to the data directory

        Note: Only creates directories when actually needed, not at import time
        """
        if not os.path.exists(self.DB_DIR):
            get_logger("engagic.config").info("creating data directory", path=self.DB_DIR)
            os.makedirs(self.DB_DIR, exist_ok=True)
        return self.DB_DIR

    def is_development(self) -> bool:
        """Check if running in development mode"""
        return self.DEBUG or "localhost" in str(self.ALLOWED_ORIGINS)

    def summary(self) -> dict:
        """Get a summary of current configuration (excluding secrets)"""
        return {
            "db_dir": self.DB_DIR,
            "postgres_enabled": self.USE_POSTGRES,
            "postgres_host": self.POSTGRES_HOST if self.USE_POSTGRES else None,
            "postgres_db": self.POSTGRES_DB if self.USE_POSTGRES else None,
            "postgres_pool_size": f"{self.POSTGRES_POOL_MIN_SIZE}-{self.POSTGRES_POOL_MAX_SIZE}" if self.USE_POSTGRES else None,
            "api_host": self.API_HOST,
            "api_port": self.API_PORT,
            "debug": self.DEBUG,
            "rate_limit_requests": self.RATE_LIMIT_REQUESTS,
            "rate_limit_window": self.RATE_LIMIT_WINDOW,
            "max_query_length": self.MAX_QUERY_LENGTH,
            "allowed_origins_count": len(self.ALLOWED_ORIGINS),
            "background_processing": self.BACKGROUND_PROCESSING,
            "sync_interval_hours": self.SYNC_INTERVAL_HOURS,
            "processing_interval_hours": self.PROCESSING_INTERVAL_HOURS,
            "log_level": self.LOG_LEVEL,
            "has_api_key": bool(self.get_api_key()),
            "is_development": self.is_development(),
        }


def configure_structlog(is_development: bool = False, log_level: str = "INFO"):
    """Configure structlog for structured logging

    Args:
        is_development: If True, use human-readable console output. If False, use JSON.
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)

    Confidence: 8/10 - Standard structlog setup with dev/prod modes
    """
    # Convert log level string to logging constant
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors for all modes
    # Note: No timestamp processor - systemd/journald already provides timestamps
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_development:
        # Development: Colored console output with readable formatting
        # ConsoleRenderer owns exception rendering. Pre-formatting exc_info
        # turns the traceback into a string first, which triggers structlog's
        # warning and prevents its single pretty-exception rendering path.
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # Production: JSON output for log aggregation
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard logging (for backward compatibility during migration)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
    )


# Global configuration instance
config = Config()

# Configure structured logging
# Use dev format if LOG_FORMAT=dev, or if in development mode
use_dev_format = config.LOG_FORMAT == "dev" or config.is_development()
configure_structlog(
    is_development=use_dev_format,
    log_level=config.LOG_LEVEL
)
