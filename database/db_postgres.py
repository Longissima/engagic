"""PostgreSQL Database Layer with Repository Pattern

Clean architecture using async repositories for all data access.
Database class provides connection pooling and convenience facades.
"""

import asyncio
import asyncpg
import json
import time
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

from config import get_logger, config
from database.models import Jurisdiction, Meeting, AgendaItem
from database.repositories_async import (
    JurisdictionRepository,
    CommitteeRepository,
    CouncilMemberRepository,
    HappeningRepository,
    MeetingRepository,
    ItemRepository,
    MatterRepository,
    QueueRepository,
    BatchJobRepository,
    DocumentBlobRepository,
    PipelineLifecycleRepository,
    SearchRepository,
)
from database.repositories_async.deliberation import DeliberationRepository
from database.repositories_async.engagement import EngagementRepository
from database.repositories_async.feedback import FeedbackRepository
from database.repositories_async.userland import UserlandRepository
from database.migrate import assert_schema_current
from corpus.store import close_corpus, init_corpus
from exceptions import DatabaseConnectionError

logger = get_logger(__name__).bind(component="database_postgres")


def _jsonb_encoder(obj):
    """JSONB encoder with Pydantic model support."""
    def default(o):
        if hasattr(o, 'model_dump'):
            return o.model_dump()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")
    return json.dumps(obj, default=default)


class Database:
    """Async PostgreSQL database with repository pattern.

    Provides connection pooling and convenience facades.
    Use Database.create() classmethod for instantiation.
    """

    pool: asyncpg.Pool

    # Repository attributes
    jurisdictions: JurisdictionRepository
    council_members: CouncilMemberRepository
    meetings: MeetingRepository
    items: ItemRepository
    matters: MatterRepository
    queue: QueueRepository
    batch_jobs: BatchJobRepository
    document_blobs: DocumentBlobRepository
    pipeline_lifecycle: PipelineLifecycleRepository
    search: SearchRepository
    userland: UserlandRepository
    deliberation: DeliberationRepository
    happening: HappeningRepository

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        initialize_corpus: bool = True,
    ):
        self.pool = pool
        self.jurisdictions = JurisdictionRepository(pool)
        self.committees = CommitteeRepository(pool)
        self.council_members = CouncilMemberRepository(pool)
        self.happening = HappeningRepository(pool)
        self.meetings = MeetingRepository(pool)
        self.items = ItemRepository(pool)
        self.matters = MatterRepository(pool)
        self.queue = QueueRepository(pool)
        self.batch_jobs = BatchJobRepository(pool)
        self.document_blobs = DocumentBlobRepository(pool)
        self.pipeline_lifecycle = PipelineLifecycleRepository(pool)
        self.search = SearchRepository(pool)
        self.userland = UserlandRepository(pool)
        self.engagement = EngagementRepository(pool)
        self.feedback = FeedbackRepository(pool)
        self.deliberation = DeliberationRepository(pool)

        # The corpus singleton normally rides the DB lifecycle: adapters and
        # the analyzer reach it via corpus.get_corpus() since neither holds a
        # DB. Relational-only inspection callers can opt out without changing
        # the default runtime boundary.
        self._manages_corpus_lifecycle = initialize_corpus
        if initialize_corpus:
            init_corpus(self.document_blobs)

        # Platform metrics are whole-table aggregates over 700k+ items and the
        # numbers move on the sync cadence, not per request. Cache with
        # single-flight so concurrent analytics/platform callers cost one compact
        # snapshot scan, not one scan per endpoint or visitor.
        self._platform_metrics_cache: Optional[tuple[float, dict]] = None
        self._platform_metrics_lock = asyncio.Lock()

        logger.info("database initialized with repositories", pool_size=f"{pool._minsize}-{pool._maxsize}")

    @classmethod
    async def create(
        cls,
        dsn: Optional[str] = None,
        min_size: int = config.POSTGRES_POOL_MIN_SIZE,
        max_size: int = config.POSTGRES_POOL_MAX_SIZE,
        *,
        require_current_schema: bool = True,
        initialize_corpus: bool = True,
    ) -> "Database":
        """Create a database pool and optionally attach the corpus lifecycle."""
        if dsn is None:
            dsn = config.get_postgres_dsn()

        async def init_connection(conn):
            # Server-side guard for genuinely stuck statements. Postgres aborts the
            # statement and sends a clean error asyncpg fully consumes, so the
            # follow-up ROLLBACK runs safely - unlike client-side command_timeout.
            await conn.execute("SET statement_timeout = '300s'")
            await conn.set_type_codec(
                'jsonb',
                encoder=_jsonb_encoder,
                decoder=json.loads,
                schema='pg_catalog'
            )

        try:
            # No blanket command_timeout: a per-statement timeout firing mid-sync
            # cancels the in-flight query, which can leave the connection draining
            # an error (PROTOCOL_ERROR_CONSUME) when the transaction's ROLLBACK or
            # the pool's reset-on-release runs -> "another operation is in progress",
            # and that poisoned connection then stalls pool.close(). Guard genuinely
            # stuck statements with a Postgres-side statement_timeout instead.
            pool = await asyncpg.create_pool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                init=init_connection,
            )
            if require_current_schema:
                try:
                    async with pool.acquire() as conn:
                        await assert_schema_current(conn)
                except Exception:
                    await pool.close()
                    raise
            logger.info("connection pool created", min_size=min_size, max_size=max_size)
            return cls(pool, initialize_corpus=initialize_corpus)
        except (asyncpg.PostgresError, OSError, ConnectionError) as e:
            logger.error("failed to create connection pool", error=str(e))
            raise DatabaseConnectionError(f"Failed to connect to PostgreSQL: {e}") from e

    async def close(self):
        # The corpus store closes first: it holds an aiohttp session, not pool
        # connections, but its repository dies with this pool either way.
        # A relational-only instance must not close a singleton it did not own.
        if self._manages_corpus_lifecycle:
            await close_corpus()
        # Graceful close waits to reset each connection; one left mid-operation
        # can't be reset and blocks indefinitely. Bound it, then force-terminate.
        try:
            await asyncio.wait_for(self.pool.close(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("pool close timed out; terminating connections")
            self.pool.terminate()
        logger.info("connection pool closed")

    async def init_schema(self):
        """Initialize database schema from SQL files. Safe to call multiple times."""
        schema_path = Path(__file__).parent / "schema_postgres.sql"
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema file not found: {schema_path}")

        async with self.pool.acquire() as conn:
            await conn.execute(schema_path.read_text())
        logger.info("main schema initialized")

        userland_schema_path = Path(__file__).parent / "schema_userland.sql"
        if not userland_schema_path.exists():
            raise FileNotFoundError(f"Userland schema file not found: {userland_schema_path}")

        async with self.pool.acquire() as conn:
            await conn.execute(userland_schema_path.read_text())
        logger.info("userland schema initialized")

    async def get_stats(self) -> dict:
        """Get database statistics for monitoring.

        A meeting counts as "summarized" if it has either a meeting-level summary
        (legacy pymupdf_gemini path that summarizes the whole packet PDF) OR at
        least one item-level summary (new item-first pipeline). Counting only
        meetings.summary undercounts ~5x because the modern pipeline never writes
        that column -- it writes per-item summaries to items.summary.
        """
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM jurisdictions WHERE status = 'active') as active_cities,
                    (SELECT COUNT(*) FROM meetings) as total_meetings,
                    (SELECT COUNT(*) FROM meetings m WHERE
                         m.summary IS NOT NULL
                         OR EXISTS (SELECT 1 FROM items i WHERE i.meeting_id = m.id AND i.summary IS NOT NULL)
                    ) as summarized_meetings,
                    (SELECT COUNT(*) FROM meetings WHERE processing_status = 'pending') as pending_meetings
            """)

            stats = dict(result)
            total = stats['total_meetings']
            summarized = stats['summarized_meetings']
            stats['summary_rate'] = f"{summarized / total * 100:.1f}%" if total > 0 else "0%"
            return stats

    # Platform metrics are read on every /about/metrics render (SSR, plus the
    # client's retries). Recomputing costs a full pass over items/meetings/votes,
    # so results are cached for this long. Numbers advance on the sync cadence --
    # minutes of staleness is invisible on a stats page.
    PLATFORM_METRICS_TTL_SECONDS = 300

    # A cache miss used to launch five queries which independently scanned the
    # 2 GB items relation. Under production I/O pressure the scans amplified one
    # another and a nominal 3-5 second query became a 20 second request. Project
    # the wide text rows to compact flags once, then derive totals, growth, weekly
    # trends, and the analytics-page rollups from that shared projection.
    _PLATFORM_METRICS_CONTENT = """
        WITH
            weeks AS (
                SELECT generate_series(
                    date_trunc('week', NOW() - INTERVAL '7 weeks'),
                    date_trunc('week', NOW()),
                    INTERVAL '1 week'
                ) AS week_start
            ),
            matter_flags AS MATERIALIZED (
                SELECT
                    id,
                    banana,
                    created_at,
                    canonical_summary IS NOT NULL
                        AND canonical_summary != '' AS has_content
                FROM city_matters
            ),
            item_flags AS MATERIALIZED (
                SELECT
                    i.meeting_id,
                    i.created_at,
                    i.matter_id IS NULL AS is_standalone,
                    i.summary IS NOT NULL AS has_summary,
                    i.summary IS NOT NULL AND i.summary != '' AS has_content,
                    i.filter_reason IS NOT NULL AS has_filter,
                    COALESCE(mf.has_content, FALSE) AS has_matter_content
                FROM items i
                LEFT JOIN matter_flags mf ON mf.id = i.matter_id
            ),
            item_stats AS MATERIALIZED (
                SELECT
                    meeting_id,
                    COUNT(*) AS item_count,
                    BOOL_OR(has_summary) AS has_summary,
                    BOOL_OR(has_content) AS has_content,
                    BOOL_OR(has_matter_content) AS has_matter_content
                FROM item_flags
                GROUP BY meeting_id
            ),
            item_rollup AS (
                SELECT
                    COUNT(*) AS agenda_items,
                    COUNT(*) FILTER (WHERE has_summary) AS summarized_items,
                    COUNT(*) FILTER (WHERE has_filter) AS filtered_items,
                    COUNT(*) FILTER (
                        WHERE is_standalone AND has_content
                    ) AS standalone_items,
                    COUNT(*) FILTER (
                        WHERE created_at >= NOW() - INTERVAL '30 days'
                    ) AS items_30d
                FROM item_flags
            ),
            item_weekly AS (
                SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                FROM item_flags
                WHERE created_at >= NOW() - INTERVAL '8 weeks'
                GROUP BY 1
            ),
            matter_rollup AS (
                SELECT
                    COUNT(*) AS matters,
                    COUNT(*) FILTER (WHERE has_content) AS matters_with_summary,
                    COUNT(*) FILTER (
                        WHERE created_at >= NOW() - INTERVAL '30 days'
                    ) AS matters_30d
                FROM matter_flags
            ),
            matter_weekly AS (
                SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                FROM matter_flags
                WHERE created_at >= NOW() - INTERVAL '8 weeks'
                GROUP BY 1
            ),
            meeting_flags AS MATERIALIZED (
                SELECT
                    m.id,
                    m.banana,
                    m.created_at,
                    m.date,
                    m.packet_url IS NOT NULL AND m.packet_url != '' AS has_packet,
                    m.summary IS NOT NULL AND m.summary != '' AS has_meeting_content,
                    m.summary IS NOT NULL
                        OR COALESCE(s.has_summary, FALSE) AS is_processed,
                    (m.summary IS NOT NULL AND m.summary != '')
                        OR COALESCE(s.has_content, FALSE)
                        OR COALESCE(s.has_matter_content, FALSE) AS has_content,
                    COALESCE(s.item_count, 0) AS item_count
                FROM meetings m
                LEFT JOIN item_stats s ON s.meeting_id = m.id
            ),
            meeting_rollup AS (
                SELECT
                    COUNT(*) AS meetings,
                    COUNT(DISTINCT banana) AS active_cities,
                    COUNT(*) FILTER (WHERE item_count > 0) AS meetings_with_items,
                    COUNT(*) FILTER (WHERE has_packet) AS packets_count,
                    COUNT(*) FILTER (WHERE has_meeting_content) AS summaries_count,
                    COUNT(*) FILTER (WHERE is_processed) AS summarized_meetings,
                    COUNT(*) FILTER (WHERE has_content) AS live_meetings,
                    COALESCE(
                        SUM(item_count) FILTER (WHERE is_processed), 0
                    )::BIGINT AS items_analyzed,
                    COUNT(*) FILTER (
                        WHERE created_at >= NOW() - INTERVAL '30 days'
                    ) AS meetings_30d,
                    COUNT(*) FILTER (
                        WHERE date >= NOW() - INTERVAL '30 days'
                          AND has_content
                    ) AS meeting_summaries_30d
                FROM meeting_flags
            ),
            meeting_weekly AS (
                SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                FROM meeting_flags
                WHERE created_at >= NOW() - INTERVAL '8 weeks'
                GROUP BY 1
            ),
            summary_weekly AS (
                SELECT date_trunc('week', date) AS wk, COUNT(*) AS cnt
                FROM meeting_flags
                WHERE date >= NOW() - INTERVAL '8 weeks' AND has_content
                GROUP BY 1
            ),
            active_bananas AS (
                SELECT DISTINCT banana FROM meeting_flags
            ),
            live_bananas AS (
                SELECT banana FROM meeting_flags WHERE has_content
                UNION
                SELECT banana FROM matter_flags WHERE has_content
            ),
            frequently_updated AS (
                SELECT banana
                FROM meeting_flags
                WHERE has_content
                GROUP BY banana
                HAVING COUNT(*) >= 7
            ),
            jurisdiction_rollup AS (
                SELECT
                    COUNT(*) AS total_cities,
                    COUNT(*) FILTER (WHERE j.type = 'city') AS total_cities_only,
                    COUNT(*) FILTER (WHERE j.type = 'county') AS total_counties,
                    COUNT(*) FILTER (
                        WHERE j.type = 'school_district'
                    ) AS total_school_districts,
                    COUNT(*) FILTER (
                        WHERE ab.banana IS NOT NULL AND j.type = 'city'
                    ) AS active_cities_only,
                    COUNT(*) FILTER (
                        WHERE ab.banana IS NOT NULL AND j.type = 'county'
                    ) AS active_counties,
                    COUNT(*) FILTER (
                        WHERE ab.banana IS NOT NULL
                          AND j.type = 'school_district'
                    ) AS active_school_districts,
                    COUNT(*) FILTER (
                        WHERE lb.banana IS NOT NULL AND j.type = 'city'
                    ) AS live_cities,
                    COUNT(*) FILTER (
                        WHERE lb.banana IS NOT NULL AND j.type = 'county'
                    ) AS live_counties,
                    COUNT(*) FILTER (
                        WHERE lb.banana IS NOT NULL
                          AND j.type = 'school_district'
                    ) AS live_school_districts,
                    COUNT(*) FILTER (
                        WHERE lb.banana IS NOT NULL
                    ) AS live_jurisdictions_total,
                    COUNT(*) FILTER (
                        WHERE fu.banana IS NOT NULL
                    ) AS frequently_updated,
                    COALESCE(SUM(j.population) FILTER (
                        WHERE fu.banana IS NOT NULL
                    ), 0) AS frequently_updated_pop,
                    COALESCE(SUM(j.population) FILTER (
                        WHERE j.geom IS NOT NULL
                    ), 0) AS total_pop,
                    COALESCE(SUM(j.population) FILTER (
                        WHERE ab.banana IS NOT NULL
                    ), 0) AS pop_with_data,
                    COALESCE(SUM(j.population) FILTER (
                        WHERE lb.banana IS NOT NULL
                    ), 0) AS pop_with_summaries
                FROM jurisdictions j
                LEFT JOIN active_bananas ab ON ab.banana = j.banana
                LEFT JOIN live_bananas lb ON lb.banana = j.banana
                LEFT JOIN frequently_updated fu ON fu.banana = j.banana
            )
        SELECT
            ir.*,
            mr.*,
            mar.*,
            jr.*,
            (
                SELECT ARRAY_AGG(COALESCE(iw.cnt, 0) ORDER BY w.week_start)
                FROM weeks w LEFT JOIN item_weekly iw ON iw.wk = w.week_start
            ) AS item_trend,
            (
                SELECT ARRAY_AGG(COALESCE(mw.cnt, 0) ORDER BY w.week_start)
                FROM weeks w LEFT JOIN meeting_weekly mw ON mw.wk = w.week_start
            ) AS meeting_trend,
            (
                SELECT ARRAY_AGG(COALESCE(matw.cnt, 0) ORDER BY w.week_start)
                FROM weeks w LEFT JOIN matter_weekly matw ON matw.wk = w.week_start
            ) AS matter_trend,
            (
                SELECT ARRAY_AGG(COALESCE(sw.cnt, 0) ORDER BY w.week_start)
                FROM weeks w LEFT JOIN summary_weekly sw ON sw.wk = w.week_start
            ) AS summary_trend
        FROM item_rollup ir
        CROSS JOIN meeting_rollup mr
        CROSS JOIN matter_rollup mar
        CROSS JOIN jurisdiction_rollup jr
    """

    _PLATFORM_METRICS_INFRASTRUCTURE = """
        SELECT
            (SELECT COUNT(*) FROM matter_appearances) AS matter_appearances,
            (SELECT COUNT(*) FROM committees) AS committees,
            (SELECT COUNT(*) FROM council_members) AS council_members,
            (SELECT COUNT(*) FROM committee_members) AS committee_assignments,
            (SELECT COUNT(*) FROM sponsorships) AS sponsorships
    """

    # Votes use their own compact projection so the total, growth, city ranking,
    # and weekly sparkline need one pass over votes instead of four.
    _PLATFORM_METRICS_VOTES = """
        WITH
            weeks AS (
                SELECT generate_series(
                    date_trunc('week', NOW() - INTERVAL '7 weeks'),
                    date_trunc('week', NOW()),
                    INTERVAL '1 week'
                ) AS week_start
            ),
            vote_flags AS MATERIALIZED (
                SELECT council_member_id, created_at FROM votes
            ),
            vote_rollup AS (
                SELECT
                    COUNT(*) AS votes,
                    COUNT(DISTINCT SPLIT_PART(council_member_id, '_', 1))
                        AS cities_with_votes,
                    COUNT(DISTINCT council_member_id) AS officials_with_votes,
                    COUNT(*) FILTER (
                        WHERE created_at >= NOW() - INTERVAL '30 days'
                    ) AS votes_30d
                FROM vote_flags
            ),
            vote_weekly AS (
                SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                FROM vote_flags
                WHERE created_at >= NOW() - INTERVAL '8 weeks'
                GROUP BY 1
            ),
            vote_by_city AS (
                SELECT
                    SPLIT_PART(council_member_id, '_', 1) AS city,
                    COUNT(*) AS votes,
                    COUNT(DISTINCT council_member_id) AS voters
                FROM vote_flags
                GROUP BY 1
                ORDER BY votes DESC
                LIMIT 10
            )
        SELECT
            vr.*,
            (
                SELECT ARRAY_AGG(COALESCE(vw.cnt, 0) ORDER BY w.week_start)
                FROM weeks w LEFT JOIN vote_weekly vw ON vw.wk = w.week_start
            ) AS vote_trend,
            (
                SELECT COALESCE(
                    JSONB_AGG(JSONB_BUILD_OBJECT(
                        'city', city,
                        'votes', votes,
                        'voters', voters
                    ) ORDER BY votes DESC),
                    '[]'::JSONB
                )
                FROM vote_by_city
            ) AS votes_by_city
        FROM vote_rollup vr
    """

    async def _fetchrow_on_own_connection(self, query: str) -> asyncpg.Record:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query)

    async def get_platform_metrics(self, *, force_refresh: bool = False) -> dict:
        """Get comprehensive platform metrics for impact/about page.

        Served from a process-local TTL cache. On a miss, exactly one caller
        computes while the rest wait on the lock and then read the fresh entry --
        a retry storm from an SSR timeout must not multiply the scan cost.
        """
        cached = self._platform_metrics_cache
        if not force_refresh and cached and time.monotonic() - cached[0] < self.PLATFORM_METRICS_TTL_SECONDS:
            return cached[1]

        async with self._platform_metrics_lock:
            cached = self._platform_metrics_cache
            if not force_refresh and cached and time.monotonic() - cached[0] < self.PLATFORM_METRICS_TTL_SECONDS:
                return cached[1]

            started = time.monotonic()
            content, infrastructure, votes = await asyncio.gather(
                self._fetchrow_on_own_connection(self._PLATFORM_METRICS_CONTENT),
                self._fetchrow_on_own_connection(self._PLATFORM_METRICS_INFRASTRUCTURE),
                self._fetchrow_on_own_connection(self._PLATFORM_METRICS_VOTES),
            )

            metrics = {**dict(content), **dict(infrastructure), **dict(votes)}

            # Rates use the processed-meeting denominator, not every item ever seen
            if metrics['meetings'] > 0:
                metrics['meeting_summary_rate'] = round(metrics['summarized_meetings'] / metrics['meetings'] * 100, 1)
            else:
                metrics['meeting_summary_rate'] = 0

            if metrics['items_analyzed'] > 0:
                metrics['item_summary_rate'] = round(metrics['summarized_items'] / metrics['items_analyzed'] * 100, 1)
            else:
                metrics['item_summary_rate'] = 0

            metrics['trends'] = {
                'meetings': metrics.pop('meeting_trend'),
                'items': metrics.pop('item_trend'),
                'matters': metrics.pop('matter_trend'),
                'votes': metrics.pop('vote_trend'),
                'summaries': metrics.pop('summary_trend'),
            }

            elapsed_ms = round((time.monotonic() - started) * 1000)
            logger.info("platform metrics computed", elapsed_ms=elapsed_ms)
            self._platform_metrics_cache = (time.monotonic(), metrics)
            return metrics

    async def get_city(
        self,
        banana: Optional[str] = None,
        name: Optional[str] = None,
        state: Optional[str] = None,
        zipcode: Optional[str] = None
    ) -> Optional[Jurisdiction]:
        """Get jurisdiction by banana, name+state, or zipcode."""
        if banana:
            return await self.jurisdictions.get_city(banana)
        elif zipcode:
            return await self.jurisdictions.get_city_by_zipcode(zipcode)
        elif name and state:
            cities = await self.jurisdictions.get_cities(name=name, state=state, limit=1)
            return cities[0] if cities else None
        return None

    async def get_cities(
        self,
        state: Optional[str] = None,
        name: Optional[str] = None,
        vendor: Optional[str] = None,
        status: str = "active",
        limit: Optional[int] = None,
        include_zipcodes: bool = False,
    ) -> List[Jurisdiction]:
        """Get jurisdictions with optional filtering."""
        return await self.jurisdictions.get_cities(
            state=state,
            name=name,
            vendor=vendor,
            status=status,
            limit=limit,
            include_zipcodes=include_zipcodes,
        )

    async def get_city_names(self, status: str = "active") -> List[str]:
        """Get jurisdiction names for fuzzy matching."""
        return await self.jurisdictions.get_city_names(status=status)

    async def get_meeting(self, meeting_id: str) -> Optional[Meeting]:
        return await self.meetings.get_meeting(meeting_id)

    async def get_meetings(
        self,
        bananas: Optional[List[str]] = None,
        limit: int = 50,
        exclude_cancelled: bool = False
    ) -> List[Meeting]:
        """Get meetings for multiple cities."""
        if not bananas:
            return await self.meetings.get_recent_meetings(limit=limit)

        all_meetings = []
        for banana in bananas:
            all_meetings.extend(await self.meetings.get_meetings_for_city(banana, limit=limit))

        all_meetings.sort(key=lambda m: m.date if m.date else datetime.min, reverse=True)
        return all_meetings[:limit]

    async def get_agenda_items(
        self,
        meeting_id: str,
        load_matters: bool = False
    ) -> List[AgendaItem]:
        """Get agenda items for meeting. Use get_items_for_meetings() for batch loading."""
        items = await self.items.get_agenda_items(meeting_id)

        if load_matters and items:
            matter_ids = [item.matter_id for item in items if item.matter_id]
            if matter_ids:
                matters = await self.matters.get_matters_batch(matter_ids)
                for item in items:
                    if item.matter_id and item.matter_id in matters:
                        item.matter = matters[item.matter_id]

        return items

    async def get_items_for_meetings(
        self,
        meeting_ids: List[str],
        load_matters: bool = False
    ) -> Dict[str, List[AgendaItem]]:
        """Batch fetch items for multiple meetings - eliminates N+1."""
        if not meeting_ids:
            return {}

        items_by_meeting = await self.items.get_items_for_meetings(meeting_ids)

        if load_matters:
            all_matter_ids = [
                item.matter_id
                for items in items_by_meeting.values()
                for item in items
                if item.matter_id
            ]
            if all_matter_ids:
                matters = await self.matters.get_matters_batch(all_matter_ids)
                for items in items_by_meeting.values():
                    for item in items:
                        if item.matter_id and item.matter_id in matters:
                            item.matter = matters[item.matter_id]

        return items_by_meeting

    async def get_has_summarized_items(
        self, meeting_ids: List[str]
    ) -> Dict[str, bool]:
        """Check which meetings have items with summaries - lightweight for listings."""
        return await self.items.get_has_summarized_items(meeting_ids)

    async def get_matters_batch(self, matter_ids: List[str]) -> Dict[str, Any]:
        return await self.matters.get_matters_batch(matter_ids)

    async def search_meetings_by_topic(
        self,
        topic: str,
        city_banana: Optional[str] = None,
        limit: int = 50
    ) -> List[Meeting]:
        return await self.search.search_meetings_by_topic(topic, city_banana, limit)

    async def get_popular_topics(self, limit: int = 20) -> List[dict]:
        return await self.search.get_popular_topics(limit)

    async def get_items_by_topic(
        self,
        meeting_id: str,
        topic: str
    ) -> List[AgendaItem]:
        return await self.items.get_items_by_topic(meeting_id, topic)

    async def get_random_meeting_with_items(self) -> Optional[Meeting]:
        return await self.meetings.get_random_meeting_with_items()

    async def get_matter(self, matter_id: str) -> Optional[Any]:
        return await self.matters.get_matter(matter_id)

    async def get_queue_stats(self) -> dict:
        return await self.queue.get_queue_stats()

    async def get_city_meeting_stats(self, bananas: List[str]) -> dict:
        """Get meeting statistics for multiple cities (batch query)."""
        if not bananas:
            return {}

        stats = {
            b: {"total_meetings": 0, "meetings_with_packet": 0, "summarized_meetings": 0}
            for b in bananas
        }

        async with self.pool.acquire() as conn:
            # Single batch query: identify meetings with any form of summarization.
            # A meeting is "summarized" if EITHER it has a meeting-level summary
            # (legacy packet-PDF path) OR any of its items has a summary
            # (modern item-first path). Counting only one side undercounts.
            rows = await conn.fetch("""
                WITH summarized_meetings AS (
                    SELECT DISTINCT meeting_id FROM items WHERE summary IS NOT NULL
                    UNION
                    SELECT id AS meeting_id FROM meetings WHERE summary IS NOT NULL
                )
                SELECT
                    m.banana,
                    COUNT(*) as total_meetings,
                    COUNT(CASE WHEN m.packet_url IS NOT NULL OR m.agenda_url IS NOT NULL THEN 1 END) as meetings_with_packet,
                    COUNT(sm.meeting_id) as summarized_meetings
                FROM meetings m
                LEFT JOIN summarized_meetings sm ON sm.meeting_id = m.id
                WHERE m.banana = ANY($1::text[])
                GROUP BY m.banana
            """, bananas)

            for row in rows:
                stats[row['banana']] = {
                    "total_meetings": row['total_meetings'],
                    "meetings_with_packet": row['meetings_with_packet'],
                    "summarized_meetings": row['summarized_meetings'],
                }

        return stats

    async def get_states_for_city_name(self, city_name: str) -> List[str]:
        """Get state abbreviations for a city name via census_places.

        Fast PostgreSQL lookup (~10ms) replacing slow uszipcode SQLite (~2s).
        Returns list of state abbreviations sorted by frequency.
        """
        # FIPS state codes to abbreviations (continental US)
        fips_to_state = {
            "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
            "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
            "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
            "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
            "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
            "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
            "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
            "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
            "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
            "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
            "56": "WY",
        }

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT statefp, COUNT(*) as cnt
                FROM census_places
                WHERE UPPER(name) = UPPER($1)
                GROUP BY statefp
                ORDER BY cnt DESC
            """, city_name)

        return [fips_to_state[row['statefp']] for row in rows if row['statefp'] in fips_to_state]
