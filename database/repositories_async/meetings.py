"""Async MeetingRepository for meeting operations."""

from typing import List, Optional

from asyncpg import Connection

from database.repositories_async.base import BaseRepository
from database.repositories_async.helpers import build_meeting, fetch_topics_for_ids, replace_entity_topics
from database.models import Meeting, ParticipationInfo
from config import get_logger

logger = get_logger(__name__).bind(component="meeting_repository")


class MeetingRepository(BaseRepository):
    """Repository for meeting operations."""

    async def store_meeting(self, meeting: Meeting, conn: Optional[Connection] = None) -> None:
        """Store or update a meeting with topic normalization."""
        async with self._ensure_conn(conn) as c:
            await c.execute(
                """
                INSERT INTO meetings (
                    id, banana, title, date, agenda_url, agenda_sources, packet_url,
                    summary, participation, status, processing_status,
                    processing_method, processing_time, committee_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    date = EXCLUDED.date,
                    agenda_url = EXCLUDED.agenda_url,
                    agenda_sources = COALESCE(EXCLUDED.agenda_sources, meetings.agenda_sources),
                    packet_url = EXCLUDED.packet_url,
                    summary = COALESCE(EXCLUDED.summary, meetings.summary),
                    participation = COALESCE(EXCLUDED.participation, meetings.participation),
                    status = EXCLUDED.status,
                    processing_status = COALESCE(EXCLUDED.processing_status, meetings.processing_status),
                    processing_method = COALESCE(EXCLUDED.processing_method, meetings.processing_method),
                    processing_time = COALESCE(EXCLUDED.processing_time, meetings.processing_time),
                    committee_id = COALESCE(EXCLUDED.committee_id, meetings.committee_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                meeting.id,
                meeting.banana,
                meeting.title,
                meeting.date,
                meeting.agenda_url,
                meeting.agenda_sources,
                meeting.packet_url,
                meeting.summary,
                meeting.participation,
                meeting.status,
                meeting.processing_status or "pending",
                meeting.processing_method,
                meeting.processing_time,
                meeting.committee_id,
            )

            if meeting.topics:
                await replace_entity_topics(
                    c, "meeting_topics", "meeting_id", meeting.id, meeting.topics
                )

        logger.info("meeting stored", meeting_id=meeting.id, banana=meeting.banana)

    async def get_meeting(self, meeting_id: str) -> Optional[Meeting]:
        """Get a meeting by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, banana, title, date, agenda_url, agenda_sources, packet_url,
                    summary, participation, status, processing_status,
                    processing_method, processing_time, committee_id,
                    created_at, updated_at
                FROM meetings
                WHERE id = $1
                """,
                meeting_id,
            )

            if not row:
                return None

            topics_map = await fetch_topics_for_ids(
                conn, "meeting_topics", "meeting_id", [meeting_id]
            )

            return build_meeting(row, topics_map.get(meeting_id, []))

    async def get_meetings_for_city(
        self,
        banana: str,
        limit: int = 50,
        offset: int = 0
    ) -> List[Meeting]:
        """Get meetings for a city, newest first, undated meetings last.

        NULLS LAST matches idx_meetings_banana_date_nl (migration 021) so
        the timeline stays an ordered index scan with LIMIT early-exit.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, banana, title, date, agenda_url, agenda_sources, packet_url,
                    summary, participation, status, processing_status,
                    processing_method, processing_time, committee_id
                FROM meetings
                WHERE banana = $1
                ORDER BY date DESC NULLS LAST
                LIMIT $2 OFFSET $3
                """,
                banana,
                limit,
                offset,
            )

            if not rows:
                return []

            meeting_ids = [row["id"] for row in rows]
            topics_by_meeting = await fetch_topics_for_ids(
                conn, "meeting_topics", "meeting_id", meeting_ids
            )

            return [
                build_meeting(row, topics_by_meeting.get(row["id"], []))
                for row in rows
            ]

    async def get_meeting_by_packet_url(self, packet_url: str) -> Optional[Meeting]:
        """Get meeting by packet_url for cache lookup."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, banana, title, date, agenda_url, agenda_sources, packet_url,
                    summary, participation, status, processing_status,
                    processing_method, processing_time, committee_id,
                    created_at, updated_at
                FROM meetings
                WHERE packet_url = $1
                LIMIT 1
                """,
                packet_url,
            )

            if not row:
                return None

            topics_map = await fetch_topics_for_ids(
                conn, "meeting_topics", "meeting_id", [row["id"]]
            )

            return build_meeting(row, topics_map.get(row["id"], []))

    async def update_meeting_summary(
        self,
        meeting_id: str,
        summary: Optional[str],
        processing_method: str,
        processing_time: float,
        participation: Optional[ParticipationInfo] = None,
        topics: Optional[List[str]] = None,
    ) -> None:
        """Update meeting summary and processing metadata."""
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE meetings
                SET summary = $2,
                    processing_status = 'completed',
                    processing_method = $3,
                    processing_time = $4,
                    participation = $5,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                meeting_id,
                summary,
                processing_method,
                processing_time,
                participation,
            )

            if topics:
                await replace_entity_topics(
                    conn, "meeting_topics", "meeting_id", meeting_id, topics
                )

        logger.info("updated meeting summary", meeting_id=meeting_id, topic_count=len(topics) if topics else 0)

    async def get_recent_meetings(self, limit: int = 50) -> List[Meeting]:
        """Get most recent meetings across all cities."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT m.*
                FROM meetings m
                WHERE m.date IS NOT NULL
                ORDER BY m.date DESC
                LIMIT $1
            """, limit)

            if not rows:
                return []

            meeting_ids = [row["id"] for row in rows]
            topics_by_meeting = await fetch_topics_for_ids(
                conn, "meeting_topics", "meeting_id", meeting_ids
            )

            return [
                build_meeting(row, topics_by_meeting.get(row["id"], []))
                for row in rows
            ]

    async def get_random_meeting_with_items(self) -> Optional[Meeting]:
        """Get a random meeting that has items and a summary (for testing)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT m.*
                FROM meetings m
                WHERE m.summary IS NOT NULL
                AND EXISTS (SELECT 1 FROM items WHERE meeting_id = m.id)
                ORDER BY RANDOM()
                LIMIT 1
            """)

            if not row:
                return None

            topics_map = await fetch_topics_for_ids(
                conn, "meeting_topics", "meeting_id", [row["id"]]
            )

            return build_meeting(row, topics_map.get(row["id"], []))

    async def get_upcoming_meetings(
        self,
        banana: str,
        start_date,
        end_date,
        limit: int = 50
    ) -> List[Meeting]:
        """
        Get upcoming meetings for a city in a date range.

        Filters out cancelled/postponed meetings.
        Used by userland weekly digest.

        Args:
            banana: City banana identifier
            start_date: Start of date range
            end_date: End of date range
            limit: Maximum meetings to return

        Returns:
            List of Meeting objects ordered by date ascending
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, banana, title, date, agenda_url, agenda_sources, packet_url,
                    summary, participation, status, processing_status,
                    processing_method, processing_time, committee_id
                FROM meetings
                WHERE banana = $1
                  AND date >= $2
                  AND date <= $3
                  AND (status IS NULL OR status NOT IN ('cancelled', 'postponed'))
                ORDER BY date ASC
                LIMIT $4
                """,
                banana,
                start_date,
                end_date,
                limit,
            )

            if not rows:
                return []

            meeting_ids = [row["id"] for row in rows]
            topics_by_meeting = await fetch_topics_for_ids(
                conn, "meeting_topics", "meeting_id", meeting_ids
            )

            return [
                build_meeting(row, topics_by_meeting.get(row["id"], []))
                for row in rows
            ]
