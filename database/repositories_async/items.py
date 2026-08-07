"""Async ItemRepository for agenda item operations."""

from collections import defaultdict
from typing import Dict, List, Optional

from asyncpg import Connection

from database.repositories_async.base import BaseRepository
from database.repositories_async.helpers import (
    build_agenda_item,
    fetch_topics_for_ids,
    replace_entity_topics,
    replace_entity_topics_batch,
)
from database.models import AgendaItem
from config import get_logger

logger = get_logger(__name__).bind(component="item_repository")


def _strip_null_bytes(value):
    """Strip null bytes from strings. PostgreSQL text columns reject 0x00."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


class ItemRepository(BaseRepository):
    """Repository for agenda item operations."""

    def _dedupe_items_by_matter(self, items: List[AgendaItem]) -> List[AgendaItem]:
        """Deduplicate items by matter_id within the same meeting.

        When multiple items reference the same matter (e.g., Legistar returning
        duplicate agenda entries), keep only the one with the most data.

        Scoring: prefer items with agenda_number, summary, attachments, topics.
        """
        if not items:
            return items

        # Group items by matter_id (None matter_id items are kept as-is)
        by_matter: Dict[Optional[str], List[AgendaItem]] = defaultdict(list)
        no_matter_items = []

        for item in items:
            if item.matter_id:
                by_matter[item.matter_id].append(item)
            else:
                no_matter_items.append(item)

        # For each matter, keep the item with the most data
        deduped = []
        duplicates_removed = 0

        for matter_id, matter_items in by_matter.items():
            if len(matter_items) == 1:
                deduped.append(matter_items[0])
            else:
                # Score each item by data completeness
                def score_item(item: AgendaItem) -> int:
                    score = 0
                    if item.agenda_number:
                        score += 10
                    if item.summary:
                        score += 5
                    if item.attachments:
                        score += len(item.attachments)
                    if item.topics:
                        score += len(item.topics)
                    if item.sponsors:
                        score += 2
                    return score

                best_item = max(matter_items, key=score_item)
                deduped.append(best_item)
                duplicates_removed += len(matter_items) - 1

        if duplicates_removed > 0:
            logger.info(
                "deduplicated items by matter_id",
                duplicates_removed=duplicates_removed,
                original_count=len(items),
                deduped_count=len(deduped) + len(no_matter_items),
            )

        return deduped + no_matter_items

    def dedupe_items_by_matter(self, items: List[AgendaItem]) -> List[AgendaItem]:
        """Public wrapper for item deduplication. Call before store_agenda_items."""
        return self._dedupe_items_by_matter(items)

    async def store_agenda_items(
        self, meeting_id: str, items: List[AgendaItem], conn: Optional[Connection] = None
    ) -> int:
        """Store multiple agenda items. Items should already be deduped via dedupe_items_by_matter()."""
        if not items:
            return 0

        async with self._ensure_conn(conn) as c:
            # Batch upsert items using executemany()
            item_records = [
                (
                    item.id,
                    item.meeting_id,
                    _strip_null_bytes(item.title),
                    item.sequence,
                    item.attachments,
                    item.attachment_hash,
                    _strip_null_bytes(item.body_text),
                    item.matter_id,
                    item.matter_file,
                    item.matter_type,
                    _strip_null_bytes(item.agenda_number),
                    _strip_null_bytes(item.sponsors),
                    _strip_null_bytes(item.summary),
                    item.topics,
                    _strip_null_bytes(item.filter_reason),
                )
                for item in items
            ]

            # Freeze-on-summary invariant: once items.summary is set, the row
            # becomes a temporal snapshot of that appearance. Re-syncs of the
            # same meeting must not silently mutate attachments, body_text, or
            # ordering on a snapshotted row -- otherwise legislative-timeline
            # reads would drift away from the state that was actually summarized.
            # matter_id / matter_file / matter_type stay mutable so that a later,
            # better matter-link can be attached without reprocessing.
            await c.executemany(
                """
                INSERT INTO items (
                    id, meeting_id, title, sequence, attachments,
                    attachment_hash, body_text, matter_id, matter_file, matter_type,
                    agenda_number, sponsors, summary, topics, filter_reason
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (id) DO UPDATE SET
                    title = CASE WHEN items.summary IS NOT NULL
                        THEN items.title ELSE EXCLUDED.title END,
                    sequence = CASE WHEN items.summary IS NOT NULL
                        THEN items.sequence ELSE EXCLUDED.sequence END,
                    attachments = CASE WHEN items.summary IS NOT NULL
                        THEN items.attachments ELSE EXCLUDED.attachments END,
                    attachment_hash = CASE WHEN items.summary IS NOT NULL
                        THEN items.attachment_hash ELSE EXCLUDED.attachment_hash END,
                    body_text = CASE WHEN items.summary IS NOT NULL
                        THEN items.body_text
                        ELSE COALESCE(EXCLUDED.body_text, items.body_text) END,
                    matter_id = EXCLUDED.matter_id,
                    matter_file = EXCLUDED.matter_file,
                    matter_type = EXCLUDED.matter_type,
                    agenda_number = CASE WHEN items.summary IS NOT NULL
                        THEN items.agenda_number ELSE EXCLUDED.agenda_number END,
                    sponsors = CASE WHEN items.summary IS NOT NULL
                        THEN items.sponsors ELSE EXCLUDED.sponsors END,
                    summary = items.summary,
                    topics = items.topics,
                    filter_reason = COALESCE(EXCLUDED.filter_reason, items.filter_reason)
                """,
                item_records,
            )

            # Batch normalize topics to item_topics table
            items_with_topics = {
                item.id: item.topics
                for item in items
                if item.topics
            }
            if items_with_topics:
                await replace_entity_topics_batch(
                    c, "item_topics", "item_id", items_with_topics
                )

        logger.debug("stored agenda items", count=len(items), meeting_id=meeting_id)
        return len(items)

    async def get_item_matter_links(
        self,
        meeting_id: str,
        *,
        conn: Connection,
    ) -> Dict[str, str]:
        """Read the meeting's current item-to-matter links without row locks.

        Meeting sync calls this only after it owns the meeting row lock. That
        lock serializes writers for one meeting, while the returned old matter
        IDs let the caller acquire the sorted old+new aggregate union before it
        locks any item rows. Taking item locks during discovery would invert the
        global matter -> items order for A -> B relinks.
        """
        rows = await conn.fetch(
            """
            SELECT id, matter_id
            FROM items
            WHERE meeting_id = $1
              AND matter_id IS NOT NULL
            ORDER BY id
            """,
            meeting_id,
        )
        return {row["id"]: row["matter_id"] for row in rows}

    async def get_agenda_items(
        self,
        meeting_id: str,
        load_matters: bool = False,
        conn: Optional[Connection] = None,
        *,
        lock_for_update: bool = False,
    ) -> List[AgendaItem]:
        """Get all agenda items for a meeting."""
        async with self._ensure_conn(conn) as c:
            lock_clause = "FOR UPDATE" if lock_for_update else ""
            rows = await c.fetch(
                f"""
                SELECT
                    id, meeting_id, title, sequence, attachments,
                    attachment_hash, body_text, matter_id, matter_file, matter_type,
                    agenda_number, sponsors, summary, topics, quality_score, rating_count,
                    filter_reason
                FROM items
                WHERE meeting_id = $1
                ORDER BY sequence
                {lock_clause}
                """,
                meeting_id,
            )

            if not rows:
                return []

            item_ids = [row["id"] for row in rows]
            topics_by_item = await fetch_topics_for_ids(
                c, "item_topics", "item_id", item_ids
            )

            return [
                build_agenda_item(row, topics_by_item.get(row["id"], []))
                for row in rows
            ]

    async def get_items_for_meetings(
        self, meeting_ids: List[str]
    ) -> Dict[str, List[AgendaItem]]:
        """Batch fetch items for multiple meetings - eliminates N+1."""
        if not meeting_ids:
            return {}

        async with self.pool.acquire() as conn:
            # Single query for ALL items across ALL meetings
            rows = await conn.fetch(
                """
                SELECT
                    id, meeting_id, title, sequence, attachments,
                    attachment_hash, body_text, matter_id, matter_file, matter_type,
                    agenda_number, sponsors, summary, topics, quality_score, rating_count,
                    filter_reason
                FROM items
                WHERE meeting_id = ANY($1::text[])
                ORDER BY meeting_id, sequence
                """,
                meeting_ids,
            )

            if not rows:
                return {}

            item_ids = [row["id"] for row in rows]
            topics_by_item = await fetch_topics_for_ids(
                conn, "item_topics", "item_id", item_ids
            )

            items_by_meeting: Dict[str, List[AgendaItem]] = defaultdict(list)
            for row in rows:
                item = build_agenda_item(row, topics_by_item.get(row["id"], []))
                items_by_meeting[row["meeting_id"]].append(item)

            return dict(items_by_meeting)

    async def get_has_summarized_items(
        self, meeting_ids: List[str]
    ) -> Dict[str, bool]:
        """Check which meetings have items with summaries - lightweight for listings.

        Returns dict mapping meeting_id -> True if has summarized items.
        Much faster than get_items_for_meetings when you only need to know
        if items exist, not their content.
        """
        if not meeting_ids:
            return {}

        async with self.pool.acquire() as conn:
            # Single aggregate query - no item content loaded
            rows = await conn.fetch(
                """
                SELECT meeting_id, bool_or(summary IS NOT NULL) as has_summary
                FROM items
                WHERE meeting_id = ANY($1::text[])
                GROUP BY meeting_id
                """,
                meeting_ids,
            )

            return {row["meeting_id"]: row["has_summary"] for row in rows}

    async def get_agenda_item(self, item_id: str) -> Optional[AgendaItem]:
        """Get a single agenda item by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    id, meeting_id, title, sequence, attachments,
                    attachment_hash, body_text, matter_id, matter_file, matter_type,
                    agenda_number, sponsors, summary, topics, quality_score, rating_count,
                    filter_reason
                FROM items
                WHERE id = $1
                """,
                item_id,
            )

            if not row:
                return None

            topics_map = await fetch_topics_for_ids(
                conn, "item_topics", "item_id", [item_id]
            )

            return build_agenda_item(row, topics_map.get(item_id, []))

    async def update_agenda_item(
        self,
        item_id: str,
        summary: Optional[str] = None,
        topics: Optional[List[str]] = None,
        conn: Optional[Connection] = None,
        **kwargs
    ) -> None:
        """Update agenda item fields.

        Uses single transaction for both item update and topics update
        to prevent race conditions where item updates but topics fail.
        """
        if summary is not None:
            kwargs["summary"] = summary
        if topics is not None:
            kwargs["topics"] = topics

        if not kwargs:
            return

        set_clauses = []
        values = []
        param_num = 1

        for key, value in kwargs.items():
            if key == "topics":
                continue  # topics handled via normalized table
            set_clauses.append(f"{key} = ${param_num}")
            values.append(value)
            param_num += 1
        if summary is not None:
            set_clauses.append("summary_updated_at = CURRENT_TIMESTAMP")

        async def update(connection: Connection) -> None:
            if set_clauses:
                values.append(item_id)  # WHERE clause parameter
                query = f"""
                    UPDATE items
                    SET {', '.join(set_clauses)}
                    WHERE id = ${param_num}
                """
                await connection.execute(query, *values)

            if "topics" in kwargs:
                await replace_entity_topics(
                    connection,
                    "item_topics",
                    "item_id",
                    item_id,
                    kwargs["topics"] or [],
                )

        if conn is not None:
            await update(conn)
        else:
            # Single transaction for both item update and topics update.
            async with self.transaction() as transaction_conn:
                await update(transaction_conn)

        logger.debug("updated agenda item", item_id=item_id, fields=list(kwargs.keys()))

    async def update_filter_reason(
        self,
        item_id: str,
        reason: str,
        conn: Optional[Connection] = None,
    ) -> None:
        """Update the filter_reason for an item."""
        async with self._ensure_conn(conn) as connection:
            await connection.execute(
                "UPDATE items SET filter_reason = $1 WHERE id = $2",
                reason,
                item_id,
            )

    async def get_all_items_for_matter(
        self,
        matter_id: str,
        conn: Optional[Connection] = None,
        *,
        lock_for_update: bool = False,
    ) -> List[AgendaItem]:
        """Get all agenda items across all meetings for a given matter."""
        if not matter_id:
            return []

        items_by_matter = await self.get_all_items_for_matters(
            [matter_id],
            conn=conn,
            lock_for_update=lock_for_update,
        )
        return items_by_matter.get(matter_id, [])

    async def get_all_items_for_matters(
        self,
        matter_ids: List[str],
        conn: Optional[Connection] = None,
        *,
        lock_for_update: bool = False,
    ) -> Dict[str, List[AgendaItem]]:
        """Batch-load every historical item appearance for several matters.

        When locking is requested, rows are acquired in deterministic
        ``matter_id, meeting_id, sequence, id`` order. Meeting sync calls this
        only after locking the corresponding matter rows, preserving the
        system-wide matter -> items lock hierarchy.
        """
        unique_ids = sorted(set(matter_ids))
        if not unique_ids:
            return {}

        async with self._ensure_conn(conn) as c:
            lock_clause = "FOR UPDATE" if lock_for_update else ""
            rows = await c.fetch(
                f"""
                SELECT
                    id, meeting_id, title, sequence, attachments,
                    attachment_hash, body_text, matter_id, matter_file, matter_type,
                    agenda_number, sponsors, summary, topics, quality_score, rating_count,
                    filter_reason
                FROM items
                WHERE matter_id = ANY($1::text[])
                ORDER BY matter_id, meeting_id, sequence, id
                {lock_clause}
                """,
                unique_ids,
            )

            if not rows:
                return {}

            item_ids = [row["id"] for row in rows]
            topics_by_item = await fetch_topics_for_ids(
                c, "item_topics", "item_id", item_ids
            )

            items_by_matter: Dict[str, List[AgendaItem]] = defaultdict(list)
            for row in rows:
                item = build_agenda_item(row, topics_by_item.get(row["id"], []))
                items_by_matter[row["matter_id"]].append(item)
            return dict(items_by_matter)

    async def bulk_fill_null_item_summaries(
        self,
        item_ids: List[str],
        summary: str,
        topics: List[str],
        prompts_version: Optional[str] = None,
        conn: Optional[Connection] = None,
    ) -> int:
        """Fill in item summaries for items that do not yet have one.

        Temporal-snapshot semantics: items that already have a summary are
        frozen per-appearance records and must not be touched. This helper
        writes the supplied (canonical) summary only to rows in item_ids
        whose summary IS NULL. Used by process_matter so a freshly-computed
        canonical fills any unsnapshotted appearances in the payload without
        clobbering appearances that were already summarized by meeting jobs
        or by prior matter runs.

        Returns the number of rows actually updated.
        """
        if not item_ids:
            return 0

        async def fill(connection: Connection) -> int:
            touched_rows = await connection.fetch(
                """
                UPDATE items
                SET summary = $1, topics = $2, prompts_version = $4,
                    summary_updated_at = CURRENT_TIMESTAMP
                WHERE id = ANY($3::text[])
                  AND summary IS NULL
                RETURNING id
                """,
                summary,
                topics,
                item_ids,
                prompts_version,
            )

            updated_count = len(touched_rows)

            if updated_count > 0:
                touched_ids = [r["id"] for r in touched_rows]
                if touched_ids:
                    entity_topics = {item_id: topics for item_id in touched_ids}
                    await replace_entity_topics_batch(
                        connection, "item_topics", "item_id", entity_topics
                    )

            return updated_count

        if conn is not None:
            updated_count = await fill(conn)
        else:
            async with self.transaction() as transaction_conn:
                updated_count = await fill(transaction_conn)

        logger.debug("bulk filled null item summaries", count=updated_count)
        return updated_count

    async def copy_summary_from_prior_appearance(
        self,
        matter_id: str,
        target_item_id: str,
        target_meeting_id: str,
        conn=None,
    ) -> bool:
        """Copy the latest prior non-null item summary for this matter onto a
        target item row. Used when MatterEnqueueDecider determines that a new
        appearance's substantive attachments are unchanged and reprocessing is
        unnecessary -- the prior appearance's summary is still the correct
        point-in-time description of this appearance's content.

        "Prior" is defined by meeting date: find the latest items.summary for
        this matter whose meeting's date is <= the target meeting's date (and
        excluding the target item itself, so idempotent on retry).

        Returns True only if the target row existed with summary IS NULL and
        was actually updated. Returns False if no prior summary was found,
        the target row does not yet exist, or the target already had a summary.
        """
        async with self._ensure_conn(conn) as c:
            row = await c.fetchrow(
                """
                SELECT i.summary, i.topics, i.prompts_version
                FROM items i
                JOIN meetings m ON m.id = i.meeting_id
                JOIN meetings target_m ON target_m.id = $3
                WHERE i.matter_id = $1
                  AND i.id <> $2
                  AND i.summary IS NOT NULL
                  AND i.summary <> ''
                  AND (
                      m.date IS NULL
                      OR target_m.date IS NULL
                      OR m.date <= target_m.date
                  )
                ORDER BY m.date DESC NULLS LAST, i.meeting_id DESC
                LIMIT 1
                """,
                matter_id,
                target_item_id,
                target_meeting_id,
            )

            if not row or not row["summary"]:
                return False

            prior_summary = row["summary"]
            prior_topics = row["topics"] or []

            # The copy carries its source's provenance — it IS that version's text
            result = await c.execute(
                """
                UPDATE items
                SET summary = $1, topics = $2, prompts_version = $4,
                    summary_updated_at = CURRENT_TIMESTAMP
                WHERE id = $3 AND summary IS NULL
                """,
                prior_summary,
                prior_topics,
                target_item_id,
                row["prompts_version"],
            )

            if self._parse_row_count(result) == 0:
                # Target row didn't exist, or already had a summary. Don't
                # touch item_topics -- inserting would FK-violate if the
                # items row is absent.
                return False

            await replace_entity_topics(
                c, "item_topics", "item_id", target_item_id, prior_topics
            )

            logger.debug(
                "copied prior-appearance summary",
                matter_id=matter_id,
                target_item_id=target_item_id,
            )
            return True

    async def get_items_by_topic(self, meeting_id: str, topic: str) -> List[AgendaItem]:
        """Get agenda items for a meeting filtered by topic."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT i.*
                FROM items i
                JOIN item_topics it ON i.id = it.item_id
                WHERE i.meeting_id = $1 AND it.topic = $2
                ORDER BY i.sequence
                """,
                meeting_id,
                topic,
            )

            if not rows:
                return []

            item_ids = [row["id"] for row in rows]
            topics_by_item = await fetch_topics_for_ids(
                conn, "item_topics", "item_id", item_ids
            )

            return [
                build_agenda_item(row, topics_by_item.get(row["id"], []))
                for row in rows
            ]

    async def search_by_keyword(
        self,
        banana: str,
        keyword: str,
        since_date,
        exclude_cancelled: bool = True
    ) -> List[Dict]:
        """
        Search items by keyword in summary, with meeting context.

        Used by userland matching engine.
        Returns raw dicts with both item and meeting fields for flexibility.

        Args:
            banana: City banana identifier
            keyword: Keyword to search (case-insensitive LIKE match)
            since_date: Only include items from meetings after this date
            exclude_cancelled: Filter out cancelled/postponed meetings

        Returns:
            List of dicts with item and meeting fields
        """
        status_filter = "AND (m.status IS NULL OR m.status NOT IN ('cancelled', 'postponed'))" if exclude_cancelled else ""

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT i.id, i.meeting_id, i.title, i.summary,
                       i.agenda_number, i.matter_file, i.sequence,
                       m.title as meeting_title, m.date, m.banana,
                       m.agenda_url, m.status,
                       c.name as city_name, c.state
                FROM items i
                JOIN meetings m ON i.meeting_id = m.id
                JOIN jurisdictions c ON m.banana = c.banana
                WHERE m.banana = $1
                  AND COALESCE(
                      m.date, i.summary_updated_at, i.created_at
                  ) >= $2
                  {status_filter}
                  AND i.summary LIKE $3
                ORDER BY m.date DESC NULLS LAST,
                         i.summary_updated_at DESC NULLS LAST,
                         i.created_at DESC
                """,
                banana,
                since_date,
                f"%{keyword}%",
            )

            return [dict(row) for row in rows]

    async def search_upcoming_by_keyword(
        self,
        banana: str,
        keyword: str,
        start_date,
        end_date
    ) -> List[Dict]:
        """
        Search items by keyword in upcoming meetings (date range).

        Used by weekly digest for forward-looking keyword matches.

        Args:
            banana: City banana identifier
            keyword: Keyword to search (case-insensitive LIKE match)
            start_date: Start of date range
            end_date: End of date range

        Returns:
            List of dicts with item and meeting fields
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT i.id as item_id, i.meeting_id, i.title as item_title,
                       i.summary, i.agenda_number, i.matter_file, i.sequence,
                       i.sponsors,
                       m.title as meeting_title, m.date, m.banana,
                       m.agenda_url, m.status
                FROM items i
                JOIN meetings m ON i.meeting_id = m.id
                WHERE m.banana = $1
                  AND m.date >= $2
                  AND m.date <= $3
                  AND (m.status IS NULL OR m.status NOT IN ('cancelled', 'postponed'))
                  AND i.summary LIKE $4
                ORDER BY m.date ASC
                """,
                banana,
                start_date,
                end_date,
                f"%{keyword}%",
            )

            return [dict(row) for row in rows]
