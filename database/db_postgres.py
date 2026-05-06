"""PostgreSQL Database Layer with Repository Pattern

Clean architecture using async repositories for all data access.
Database class provides connection pooling and convenience facades.
"""

import asyncpg
import json
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
    SearchRepository,
)
from database.repositories_async.deliberation import DeliberationRepository
from database.repositories_async.engagement import EngagementRepository
from database.repositories_async.feedback import FeedbackRepository
from database.repositories_async.userland import UserlandRepository
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
    search: SearchRepository
    userland: UserlandRepository
    deliberation: DeliberationRepository
    happening: HappeningRepository

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.jurisdictions = JurisdictionRepository(pool)
        self.committees = CommitteeRepository(pool)
        self.council_members = CouncilMemberRepository(pool)
        self.happening = HappeningRepository(pool)
        self.meetings = MeetingRepository(pool)
        self.items = ItemRepository(pool)
        self.matters = MatterRepository(pool)
        self.queue = QueueRepository(pool)
        self.search = SearchRepository(pool)
        self.userland = UserlandRepository(pool)
        self.engagement = EngagementRepository(pool)
        self.feedback = FeedbackRepository(pool)
        self.deliberation = DeliberationRepository(pool)

        logger.info("database initialized with repositories", pool_size=f"{pool._minsize}-{pool._maxsize}")

    @classmethod
    async def create(
        cls,
        dsn: Optional[str] = None,
        min_size: int = config.POSTGRES_POOL_MIN_SIZE,
        max_size: int = config.POSTGRES_POOL_MAX_SIZE
    ) -> "Database":
        """Create database with connection pool."""
        if dsn is None:
            dsn = config.get_postgres_dsn()

        async def init_connection(conn):
            await conn.set_type_codec(
                'jsonb',
                encoder=_jsonb_encoder,
                decoder=json.loads,
                schema='pg_catalog'
            )

        try:
            pool = await asyncpg.create_pool(
                dsn,
                min_size=min_size,
                max_size=max_size,
                command_timeout=60,
                init=init_connection,
            )
            logger.info("connection pool created", min_size=min_size, max_size=max_size)
            return cls(pool)
        except (asyncpg.PostgresError, OSError, ConnectionError) as e:
            logger.error("failed to create connection pool", error=str(e))
            raise DatabaseConnectionError(f"Failed to connect to PostgreSQL: {e}") from e

    async def close(self):
        await self.pool.close()
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
        """Get database statistics for monitoring."""
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM jurisdictions WHERE status = 'active') as active_cities,
                    (SELECT COUNT(*) FROM meetings) as total_meetings,
                    (SELECT COUNT(*) FROM meetings WHERE summary IS NOT NULL) as summarized_meetings,
                    (SELECT COUNT(*) FROM meetings WHERE processing_status = 'pending') as pending_meetings
            """)

            stats = dict(result)
            total = stats['total_meetings']
            summarized = stats['summarized_meetings']
            stats['summary_rate'] = f"{summarized / total * 100:.1f}%" if total > 0 else "0%"
            return stats

    async def get_platform_metrics(self) -> dict:
        """Get comprehensive platform metrics for impact/about page."""
        async with self.pool.acquire() as conn:
            result = await conn.fetchrow("""
                SELECT
                    -- Core content
                    (SELECT COUNT(*) FROM jurisdictions) as total_cities,
                    (SELECT COUNT(DISTINCT banana) FROM meetings) as active_cities,
                    (SELECT COUNT(*) FROM meetings) as meetings,
                    (SELECT COUNT(*) FROM items) as agenda_items,
                    (SELECT COUNT(*) FROM city_matters) as matters,
                    (SELECT COUNT(*) FROM matter_appearances) as matter_appearances,
                    -- Civic infrastructure
                    (SELECT COUNT(*) FROM committees) as committees,
                    (SELECT COUNT(*) FROM council_members) as council_members,
                    (SELECT COUNT(*) FROM committee_members) as committee_assignments,
                    -- Accountability data
                    (SELECT COUNT(*) FROM votes) as votes,
                    (SELECT COUNT(*) FROM sponsorships) as sponsorships,
                    (SELECT COUNT(DISTINCT SPLIT_PART(council_member_id, '_', 1)) FROM votes) as cities_with_votes,
                    (SELECT COUNT(DISTINCT council_member_id) FROM votes) as officials_with_votes,
                    -- Processing stats
                    (SELECT COUNT(*) FROM meetings WHERE summary IS NOT NULL) as summarized_meetings,
                    (SELECT COUNT(*) FROM items WHERE summary IS NOT NULL) as summarized_items,
                    (SELECT COUNT(*) FROM items WHERE filter_reason IS NOT NULL) as filtered_items,
                    -- Items from meetings that have actually been processed
                    (SELECT COUNT(*) FROM items i
                     WHERE EXISTS (
                         SELECT 1 FROM items i2
                         WHERE i2.meeting_id = i.meeting_id
                         AND i2.summary IS NOT NULL
                     ) OR EXISTS (
                         SELECT 1 FROM meetings m
                         WHERE m.id = i.meeting_id
                         AND m.summary IS NOT NULL
                     )) as items_analyzed,
                    -- 30-day growth
                    (SELECT COUNT(*) FROM meetings
                     WHERE created_at >= NOW() - INTERVAL '30 days') as meetings_30d,
                    (SELECT COUNT(*) FROM items
                     WHERE created_at >= NOW() - INTERVAL '30 days') as items_30d,
                    (SELECT COUNT(*) FROM city_matters
                     WHERE created_at >= NOW() - INTERVAL '30 days') as matters_30d,
                    (SELECT COUNT(*) FROM votes
                     WHERE created_at >= NOW() - INTERVAL '30 days') as votes_30d,
                    -- Summarization output: summarized meetings whose meeting date
                    -- falls in the last 30 days. Uses meetings.date (immutable) instead
                    -- of updated_at (which bumps on every re-fetch UPSERT).
                    (SELECT COUNT(*) FROM meetings
                     WHERE summary IS NOT NULL AND summary != ''
                       AND date >= NOW() - INTERVAL '30 days'
                       AND date <= NOW()) as meeting_summaries_30d
            """)

            metrics = dict(result)

            # Calculate rates using correct denominator (items from processed meetings)
            if metrics['meetings'] > 0:
                metrics['meeting_summary_rate'] = round(metrics['summarized_meetings'] / metrics['meetings'] * 100, 1)
            else:
                metrics['meeting_summary_rate'] = 0

            if metrics['items_analyzed'] > 0:
                metrics['item_summary_rate'] = round(metrics['summarized_items'] / metrics['items_analyzed'] * 100, 1)
            else:
                metrics['item_summary_rate'] = 0

            # Get vote breakdown by city
            vote_breakdown = await conn.fetch("""
                SELECT
                    SPLIT_PART(council_member_id, '_', 1) as city,
                    COUNT(*) as votes,
                    COUNT(DISTINCT council_member_id) as voters
                FROM votes
                GROUP BY 1
                ORDER BY votes DESC
                LIMIT 10
            """)
            metrics['votes_by_city'] = [dict(row) for row in vote_breakdown]

            # Weekly trends for sparklines (last 8 weeks).
            # `summaries` buckets summarized meetings by meetings.date (the meeting's
            # actual occurrence) — stable, parallels the meeting_summaries_30d badge.
            trends = await conn.fetch("""
                WITH weeks AS (
                    SELECT generate_series(
                        date_trunc('week', NOW() - INTERVAL '7 weeks'),
                        date_trunc('week', NOW()),
                        INTERVAL '1 week'
                    ) AS week_start
                )
                SELECT
                    w.week_start::date as week,
                    COALESCE(m.cnt, 0) as meetings,
                    COALESCE(i.cnt, 0) as items,
                    COALESCE(mat.cnt, 0) as matters,
                    COALESCE(v.cnt, 0) as votes,
                    COALESCE(s.cnt, 0) as summaries
                FROM weeks w
                LEFT JOIN (
                    SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                    FROM meetings WHERE created_at >= NOW() - INTERVAL '8 weeks'
                    GROUP BY 1
                ) m ON m.wk = w.week_start
                LEFT JOIN (
                    SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                    FROM items WHERE created_at >= NOW() - INTERVAL '8 weeks'
                    GROUP BY 1
                ) i ON i.wk = w.week_start
                LEFT JOIN (
                    SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                    FROM city_matters WHERE created_at >= NOW() - INTERVAL '8 weeks'
                    GROUP BY 1
                ) mat ON mat.wk = w.week_start
                LEFT JOIN (
                    SELECT date_trunc('week', created_at) AS wk, COUNT(*) AS cnt
                    FROM votes WHERE created_at >= NOW() - INTERVAL '8 weeks'
                    GROUP BY 1
                ) v ON v.wk = w.week_start
                LEFT JOIN (
                    SELECT date_trunc('week', date) AS wk, COUNT(*) AS cnt
                    FROM meetings
                    WHERE summary IS NOT NULL AND summary != ''
                      AND date >= NOW() - INTERVAL '8 weeks'
                      AND date <= NOW()
                    GROUP BY 1
                ) s ON s.wk = w.week_start
                ORDER BY w.week_start
            """)
            metrics['trends'] = {
                'meetings': [row['meetings'] for row in trends],
                'items': [row['items'] for row in trends],
                'matters': [row['matters'] for row in trends],
                'votes': [row['votes'] for row in trends],
                'summaries': [row['summaries'] for row in trends],
            }

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
            # Single batch query: JOIN items once, GROUP BY banana
            # Uses subquery to identify meetings with summarized items
            rows = await conn.fetch("""
                WITH summarized_meetings AS (
                    SELECT DISTINCT meeting_id
                    FROM items
                    WHERE summary IS NOT NULL
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
