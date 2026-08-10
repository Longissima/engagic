"""Async MatterRepository for matter operations."""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from asyncpg import Connection

from database.repositories_async.base import BaseRepository
from database.repositories_async.helpers import build_matter, fetch_topics_for_ids, replace_entity_topics
from database.models import Matter, AttachmentInfo
from config import get_logger

logger = get_logger(__name__).bind(component="matter_repository")


class MatterRepository(BaseRepository):
    """Repository for matter operations."""

    async def store_matter(self, matter: Matter, conn: Optional[Connection] = None) -> None:
        """Store or update a matter with topic normalization."""
        async with self._ensure_conn(conn) as c:
            await c.execute(
                """
                INSERT INTO city_matters (
                    id, banana, matter_id, matter_file, matter_type,
                    title, sponsors, canonical_summary, canonical_topics,
                    attachments, metadata, first_seen, last_seen,
                    appearance_count, status
                )
                VALUES ($1, $2, $3::text, $4::text, $5::text, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                ON CONFLICT (id) DO UPDATE SET
                    matter_file = EXCLUDED.matter_file,
                    matter_type = EXCLUDED.matter_type,
                    title = EXCLUDED.title,
                    sponsors = EXCLUDED.sponsors,
                    canonical_summary = COALESCE(EXCLUDED.canonical_summary, city_matters.canonical_summary),
                    canonical_topics = COALESCE(EXCLUDED.canonical_topics, city_matters.canonical_topics),
                    attachments = EXCLUDED.attachments,
                    metadata = EXCLUDED.metadata,
                    first_seen = CASE
                        WHEN city_matters.first_seen IS NULL THEN EXCLUDED.first_seen
                        WHEN EXCLUDED.first_seen IS NULL THEN city_matters.first_seen
                        ELSE LEAST(city_matters.first_seen, EXCLUDED.first_seen)
                    END,
                    last_seen = CASE
                        WHEN city_matters.last_seen IS NULL THEN EXCLUDED.last_seen
                        WHEN EXCLUDED.last_seen IS NULL THEN city_matters.last_seen
                        ELSE GREATEST(city_matters.last_seen, EXCLUDED.last_seen)
                    END,
                    appearance_count = GREATEST(city_matters.appearance_count, EXCLUDED.appearance_count),
                    status = EXCLUDED.status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                matter.id,
                matter.banana,
                matter.matter_id,
                matter.matter_file,
                matter.matter_type,
                matter.title,
                matter.sponsors,
                matter.canonical_summary,
                matter.canonical_topics,
                matter.attachments,
                matter.metadata,
                matter.first_seen,
                matter.last_seen,
                matter.appearance_count or 1,
                matter.status or "active",
            )

            if matter.canonical_topics is not None:
                await replace_entity_topics(
                    c, "matter_topics", "matter_id", matter.id, matter.canonical_topics
                )

        logger.debug("stored matter", matter_id=matter.id, banana=matter.banana)

    async def get_matter(
        self,
        matter_id: str,
        conn: Optional[Connection] = None,
        *,
        lock_for_update: bool = False,
    ) -> Optional[Matter]:
        """Get a matter by ID with accurate appearance count."""
        async with self._ensure_conn(conn) as c:
            lock_clause = "FOR UPDATE OF cm" if lock_for_update else ""
            row = await c.fetchrow(
                f"""
                SELECT
                    cm.id, cm.banana, cm.matter_id, cm.matter_file, cm.matter_type,
                    cm.title, cm.sponsors, cm.canonical_summary, cm.canonical_topics,
                    cm.attachments, cm.metadata, cm.first_seen, cm.last_seen,
                    GREATEST(1, (SELECT COUNT(*) FROM items i WHERE i.matter_id = cm.id)) as appearance_count,
                    cm.status, cm.created_at, cm.updated_at,
                    cm.final_vote_date, cm.quality_score, cm.rating_count,
                    (SELECT COUNT(*) FROM items i WHERE i.matter_id = cm.id) as actual_item_count
                FROM city_matters cm
                WHERE cm.id = $1
                {lock_clause}
                """,
                matter_id,
            )

            if not row:
                return None

            # Orphan matters (0 items) without summaries are treated as non-existent
            # Orphans WITH summaries are preserved (appearance_count clamped to 1)
            if row["actual_item_count"] == 0:
                if row["canonical_summary"]:
                    logger.debug("orphan matter with summary preserved", matter_id=matter_id)
                else:
                    logger.warning("orphan matter without summary skipped", matter_id=matter_id)
                    return None

            topics_map = await fetch_topics_for_ids(
                c, "matter_topics", "matter_id", [matter_id]
            )
            topics = topics_map.get(matter_id, [])

            return build_matter(row, topics or None)

    async def get_matters_batch(self, matter_ids: List[str]) -> Dict[str, Matter]:
        """Batch fetch multiple matters by ID - eliminates N+1.

        Computes actual appearance_count from items table instead of using
        stored value, which can drift due to race conditions in sync logic.
        """
        if not matter_ids:
            return {}

        unique_ids = list(set(matter_ids))

        async with self.pool.acquire() as conn:
            # Compute accurate appearance_count from items table
            rows = await conn.fetch(
                """
                SELECT
                    cm.id, cm.banana, cm.matter_id, cm.matter_file, cm.matter_type,
                    cm.title, cm.sponsors, cm.canonical_summary, cm.canonical_topics,
                    cm.attachments, cm.metadata, cm.first_seen, cm.last_seen,
                    (SELECT COUNT(*) FROM items i WHERE i.matter_id = cm.id) as appearance_count,
                    cm.status, cm.created_at, cm.updated_at,
                    cm.final_vote_date, cm.quality_score, cm.rating_count
                FROM city_matters cm
                WHERE cm.id = ANY($1::text[])
                """,
                unique_ids,
            )

            if not rows:
                return {}

            # Filter out orphan matters (0 items)
            valid_rows = [r for r in rows if r["appearance_count"] > 0]
            orphan_count = len(rows) - len(valid_rows)
            if orphan_count > 0:
                logger.warning("orphan matters detected in batch", count=orphan_count)

            if not valid_rows:
                return {}

            topics_by_matter = await fetch_topics_for_ids(
                conn, "matter_topics", "matter_id", [r["id"] for r in valid_rows]
            )

            return {
                row["id"]: build_matter(
                    row, topics_by_matter.get(row["id"]) or None
                )
                for row in valid_rows
            }

    async def get_matters_for_sync_snapshot(
        self,
        matter_ids: List[str],
        *,
        conn: Connection,
        include_unsummarized_orphans: bool = False,
    ) -> Dict[str, Matter]:
        """Load and lock the matter portion of one meeting-sync snapshot.

        All rows are locked in stable ID order before the sync reads any item
        rows. This preserves the global matter -> items lock order while
        replacing one ``get_matter`` query pair per agenda item with one
        set-wise query pair for the meeting unit of work.

        The orphan behavior deliberately matches :meth:`get_matter`: an orphan
        with a canonical summary remains authoritative, while an unsummarized
        orphan is treated as absent and can be rebuilt by sync.
        """
        unique_ids = sorted(set(matter_ids))
        if not unique_ids:
            return {}

        rows = await conn.fetch(
            """
            WITH requested AS MATERIALIZED (
                SELECT matter_id,
                       pg_advisory_xact_lock(
                           hashtextextended(matter_id, 0)
                       ) AS identity_lock
                FROM unnest($1::text[]) AS ids(matter_id)
                ORDER BY matter_id
            )
            SELECT
                cm.id, cm.banana, cm.matter_id, cm.matter_file, cm.matter_type,
                cm.title, cm.sponsors, cm.canonical_summary, cm.canonical_topics,
                cm.attachments, cm.metadata, cm.first_seen, cm.last_seen,
                GREATEST(
                    1,
                    (SELECT COUNT(*) FROM items i WHERE i.matter_id = cm.id)
                ) AS appearance_count,
                cm.status, cm.created_at, cm.updated_at,
                cm.final_vote_date, cm.quality_score, cm.rating_count,
                (SELECT COUNT(*) FROM items i WHERE i.matter_id = cm.id)
                    AS actual_item_count
            FROM requested
            JOIN city_matters cm ON cm.id = requested.matter_id
            ORDER BY cm.id
            FOR UPDATE OF cm
            """,
            unique_ids,
        )
        if not rows:
            return {}

        valid_rows = []
        for row in rows:
            if (
                not include_unsummarized_orphans
                and row["actual_item_count"] == 0
                and not row["canonical_summary"]
            ):
                logger.warning(
                    "orphan matter without summary skipped",
                    matter_id=row["id"],
                )
                continue
            valid_rows.append(row)
        if not valid_rows:
            return {}

        valid_ids = [row["id"] for row in valid_rows]
        topics_by_matter = await fetch_topics_for_ids(
            conn, "matter_topics", "matter_id", valid_ids
        )
        return {
            row["id"]: build_matter(
                row, topics_by_matter.get(row["id"]) or None
            )
            for row in valid_rows
        }

    async def reconcile_meeting_appearances(
        self,
        *,
        meeting_id: str,
        appeared_at: Optional[datetime],
        committee: Optional[str],
        committee_id: Optional[str],
        conn: Connection,
    ) -> Dict[str, int]:
        """Make appearance relationships exactly match retained item links.

        The schema uniqueness key includes matter_id, so an A -> B item relink
        can otherwise leave both relationships alive. Delete every relationship
        whose item no longer points at that matter, then insert the one current
        relationship for each linked item. Callers hold the meeting row, sorted
        affected matter rows, and their item rows before entering this method.
        """
        deleted = await conn.execute(
            """
            DELETE FROM matter_appearances ma
            WHERE ma.meeting_id = $1
              AND NOT EXISTS (
                  SELECT 1
                  FROM items i
                  WHERE i.id = ma.item_id
                    AND i.meeting_id = ma.meeting_id
                    AND i.matter_id = ma.matter_id
              )
            """,
            meeting_id,
        )
        inserted = await conn.execute(
            """
            INSERT INTO matter_appearances (
                matter_id, meeting_id, item_id, appeared_at,
                committee, committee_id, sequence
            )
            SELECT
                i.matter_id, i.meeting_id, i.id, $2,
                $3, $4, i.sequence
            FROM items i
            WHERE i.meeting_id = $1
              AND i.matter_id IS NOT NULL
            ORDER BY i.matter_id, i.sequence, i.id
            ON CONFLICT (matter_id, meeting_id, item_id) DO NOTHING
            """,
            meeting_id,
            appeared_at,
            committee,
            committee_id,
        )
        return {
            "deleted": self._parse_row_count(deleted),
            "inserted": self._parse_row_count(inserted),
        }

    async def get_authoritative_tracking_for_matters(
        self,
        matter_ids: List[str],
        *,
        conn: Connection,
    ) -> Dict[str, Dict[str, Any]]:
        """Return exact retained appearance count and meeting-date bounds."""
        unique_ids = sorted(set(matter_ids))
        if not unique_ids:
            return {}
        rows = await conn.fetch(
            """
            SELECT
                requested.matter_id,
                COUNT(i.id)::int AS appearance_count,
                MIN(m.date) AS first_seen,
                MAX(m.date) AS last_seen
            FROM unnest($1::text[]) AS requested(matter_id)
            LEFT JOIN items i ON i.matter_id = requested.matter_id
            LEFT JOIN meetings m ON m.id = i.meeting_id
            GROUP BY requested.matter_id
            ORDER BY requested.matter_id
            """,
            unique_ids,
        )
        return {
            row["matter_id"]: {
                "appearance_count": int(row["appearance_count"] or 0),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
            }
            for row in rows
        }

    async def refresh_matter_tracking(
        self,
        *,
        matter_id: str,
        attachments: List[AttachmentInfo],
        appearance_count: int,
        first_seen: Optional[datetime],
        last_seen: Optional[datetime],
        sponsors: List[str],
        title: str,
        attachment_hash: Optional[str],
        work_version: Optional[str],
        conn: Connection,
    ) -> None:
        """Replace tracking from one locked authoritative appearance view.

        The representative title and denormalized sponsor list come from the
        same retained rows as normalized relationships. A zero-appearance
        aggregate has no source left for its canonical projection, so sponsors,
        summary, topic projections, and processing-verdict metadata are
        invalidated in the same transaction; its stable identity title remains.
        """
        await conn.execute(
            """
            UPDATE city_matters
            SET attachments = $2::jsonb,
                appearance_count = $3,
                first_seen = $4,
                last_seen = $5,
                metadata = CASE
                    WHEN $3::int = 0 THEN '{}'::jsonb
                    WHEN $6::text IS NULL AND $7::text IS NULL THEN metadata
                    ELSE COALESCE(metadata, '{}'::jsonb)
                        || CASE WHEN $6::text IS NULL THEN '{}'::jsonb
                                ELSE jsonb_build_object(
                                    'attachment_hash', $6::text
                                ) END
                        || CASE WHEN $7::text IS NULL THEN '{}'::jsonb
                                ELSE jsonb_build_object(
                                    'work_version', $7::text
                                ) END
                END,
                canonical_summary = CASE
                    WHEN $3::int = 0 THEN NULL
                    ELSE canonical_summary
                END,
                canonical_topics = CASE
                    WHEN $3::int = 0 THEN NULL
                    ELSE canonical_topics
                END,
                sponsors = CASE
                    WHEN $3::int = 0 THEN '[]'::jsonb
                    ELSE $8::jsonb
                END,
                title = CASE
                    WHEN $3::int = 0 THEN title
                    ELSE $9::text
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = $1
            """,
            matter_id,
            attachments,
            appearance_count,
            first_seen,
            last_seen,
            attachment_hash,
            work_version,
            sponsors,
            title,
        )
        if appearance_count == 0:
            await conn.execute(
                "DELETE FROM matter_topics WHERE matter_id = $1",
                matter_id,
            )

    async def update_matter_summary(
        self,
        matter_id: str,
        canonical_summary: str,
        canonical_topics: List[str],
        attachment_hash: str
    ) -> None:
        """Update matter with canonical summary, topics, and attachment hash."""
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE city_matters
                SET canonical_summary = $2,
                    metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('attachment_hash', $3),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                matter_id,
                canonical_summary,
                attachment_hash,
            )

            await replace_entity_topics(
                conn, "matter_topics", "matter_id", matter_id, canonical_topics
            )

        logger.debug("updated matter with canonical summary", matter_id=matter_id)

    async def update_attachment_hash(
        self,
        matter_id: str,
        attachment_hash: str,
        work_version: Optional[str] = None,
        conn: Optional[Connection] = None,
    ) -> None:
        """Rewrite the confirmed projection versions (format upgrades).

        Used when a stored legacy-format hash is confirmed equal to the
        current-format hash of the same attachments: a semantic no-op that
        retires the legacy value without touching summary or tracking fields.
        """
        async with self._ensure_conn(conn) as connection:
            await connection.execute(
                """
                UPDATE city_matters
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object('attachment_hash', $2::text)
                        || CASE WHEN $3::text IS NULL THEN '{}'::jsonb
                                ELSE jsonb_build_object('work_version', $3::text) END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                matter_id,
                attachment_hash,
                work_version,
            )

    async def invalidate_canonical_summary(
        self,
        matter_id: str,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Clear the canonical summary so the matter lane must re-summarize.

        Both matter-lane currency gates (the reuse CAS and the
        skip-summarization path in the processor) require a non-null
        canonical_summary before trusting the stored metadata versions, so
        clearing the summary alone forces one full aggregate re-summarization
        under the current prompt. Metadata versions, canonical_topics, and
        matter_topics deliberately survive: versions keep scoping outcome
        attempts to the unchanged appearance inputs, and topics stay visible
        until the replacement projection lands -- mirroring the item-side
        unfreeze, which nulls summary provenance but preserves topics.
        Callers must hold the matter row lock (processor lock order).
        """
        async with self._ensure_conn(conn) as c:
            result = await c.execute(
                """
                UPDATE city_matters
                SET canonical_summary = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                matter_id,
            )
        invalidated = self._parse_row_count(result) > 0
        if invalidated:
            logger.info("invalidated canonical summary", matter_id=matter_id)
        return invalidated

    async def record_matter_outcome(
        self,
        matter_id: str,
        attachment_hash: str,
        work_version: str,
        disposition: Optional[str] = None,
        increment_attempts: bool = False,
        conn: Optional[Connection] = None,
    ) -> None:
        """Record a non-success processing outcome in matter metadata.

        Stores the artifact and desired-work versions so the enqueue decider can scope
        the verdict: a disposition or exhausted attempt count only suppresses
        re-enqueueing while the attachments still hash to this value. The
        attempt counter restarts at 1 when the hash changed since the last
        recorded outcome (new content deserves a fresh budget). A later
        successful store_matter replaces metadata wholesale, clearing both
        fields.
        """
        async with self._ensure_conn(conn) as connection:
            await connection.execute(
                """
                UPDATE city_matters
                SET metadata = (COALESCE(metadata, '{}'::jsonb) - 'disposition')
                        || jsonb_build_object(
                            'attachment_hash', $2::text,
                            'work_version', $3::text,
                            'attempts',
                            CASE
                                WHEN $5::bool THEN
                                    CASE WHEN COALESCE(metadata->>'work_version', '') = $3::text
                                         THEN COALESCE((metadata->>'attempts')::int, 0) + 1
                                         ELSE 1 END
                                WHEN COALESCE(metadata->>'work_version', '') = $3::text
                                    THEN COALESCE((metadata->>'attempts')::int, 0)
                                ELSE 0
                            END
                        )
                        || CASE WHEN $4::text IS NULL THEN '{}'::jsonb
                                ELSE jsonb_build_object('disposition', $4::text) END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                matter_id,
                attachment_hash,
                work_version,
                disposition,
                increment_attempts,
            )

    async def update_matter_tracking(
        self,
        matter_id: str,
        meeting_date: Optional[datetime],
        attachments: Optional[List[AttachmentInfo]],
        attachment_hash: Optional[str],
        work_version: Optional[str] = None,
        increment_appearance_count: bool = False,
        conn: Optional[Connection] = None
    ) -> Optional[int]:
        """Update matter tracking fields with atomic increment to prevent race conditions.

        attachment_hash=None leaves metadata.attachment_hash untouched. The stored
        hash records what the canonical summary was computed from; sync passes a
        value only on a confirmed-unchanged scrape (format upgrade), never on a
        changed one -- otherwise a failed matter job would erase the change signal
        and every later sync would skip as "unchanged" with a stale summary.
        """
        async with self._ensure_conn(conn) as c:
            if increment_appearance_count:
                new_count = await c.fetchval(
                    """
                    UPDATE city_matters
                    SET first_seen = CASE
                            WHEN first_seen IS NULL THEN $2
                            WHEN $2::timestamp IS NULL THEN first_seen
                            ELSE LEAST(first_seen, $2)
                        END,
                        last_seen = CASE
                            WHEN last_seen IS NULL THEN $2
                            WHEN $2::timestamp IS NULL THEN last_seen
                            ELSE GREATEST(last_seen, $2)
                        END,
                        attachments = $3::jsonb,
                        metadata = CASE
                            WHEN $4::text IS NULL AND $5::text IS NULL THEN metadata
                            ELSE COALESCE(metadata, '{}'::jsonb)
                                || CASE WHEN $4::text IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('attachment_hash', $4::text) END
                                || CASE WHEN $5::text IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('work_version', $5::text) END
                        END,
                        updated_at = CURRENT_TIMESTAMP,
                        appearance_count = appearance_count + 1
                    WHERE id = $1
                    RETURNING appearance_count
                    """,
                    matter_id,
                    meeting_date,
                    attachments,
                    attachment_hash,
                    work_version,
                )
                logger.debug("updated matter tracking", matter_id=matter_id, new_count=new_count)
                return new_count
            else:
                await c.execute(
                    """
                    UPDATE city_matters
                    SET first_seen = CASE
                            WHEN first_seen IS NULL THEN $2
                            WHEN $2::timestamp IS NULL THEN first_seen
                            ELSE LEAST(first_seen, $2)
                        END,
                        last_seen = CASE
                            WHEN last_seen IS NULL THEN $2
                            WHEN $2::timestamp IS NULL THEN last_seen
                            ELSE GREATEST(last_seen, $2)
                        END,
                        attachments = $3::jsonb,
                        metadata = CASE
                            WHEN $4::text IS NULL AND $5::text IS NULL THEN metadata
                            ELSE COALESCE(metadata, '{}'::jsonb)
                                || CASE WHEN $4::text IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('attachment_hash', $4::text) END
                                || CASE WHEN $5::text IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('work_version', $5::text) END
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    matter_id,
                    meeting_date,
                    attachments,
                    attachment_hash,
                    work_version,
                )
                logger.debug("updated matter tracking", matter_id=matter_id, increment=False)
                return None

    async def has_appearance(
        self,
        matter_id: str,
        meeting_id: str,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Check if a matter already has an appearance record for a specific meeting."""
        async with self._ensure_conn(conn) as c:
            exists = await c.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM matter_appearances
                    WHERE matter_id = $1 AND meeting_id = $2
                )
                """,
                matter_id,
                meeting_id,
            )
            return bool(exists)

    async def get_existing_appearance_matter_ids(
        self,
        matter_ids: List[str],
        meeting_id: str,
        *,
        conn: Connection,
    ) -> Set[str]:
        """Return set-wise membership for matter appearances at one meeting."""
        unique_ids = sorted(set(matter_ids))
        if not unique_ids:
            return set()

        rows = await conn.fetch(
            """
            SELECT DISTINCT matter_id
            FROM matter_appearances
            WHERE matter_id = ANY($1::text[])
              AND meeting_id = $2
            ORDER BY matter_id
            """,
            unique_ids,
            meeting_id,
        )
        return {row["matter_id"] for row in rows}

    async def create_appearance(
        self,
        matter_id: str,
        meeting_id: str,
        item_id: str,
        appeared_at: Optional[datetime],
        committee: Optional[str] = None,
        committee_id: Optional[str] = None,
        sequence: Optional[int] = None,
        conn: Optional[Connection] = None
    ) -> None:
        """Create a matter appearance record."""
        async with self._ensure_conn(conn) as c:
            await c.execute(
                """
                INSERT INTO matter_appearances (
                    matter_id, meeting_id, item_id, appeared_at, committee, committee_id, sequence
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (matter_id, meeting_id, item_id) DO NOTHING
                """,
                matter_id,
                meeting_id,
                item_id,
                appeared_at,
                committee,
                committee_id,
                sequence,
            )

        logger.debug("created matter appearance", matter_id=matter_id, meeting_id=meeting_id)

    async def search_matters_fulltext(
        self,
        query: str,
        banana: str,
        limit: int = 50
    ) -> List[Matter]:
        """Full-text search on matters using PostgreSQL FTS."""
        # Uses search_vector stored column (requires migration 012_fts_optimization)
        # Filters orphan matters (0 items) to prevent stale duplicates from old ID generation
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    cm.id, cm.banana, cm.matter_id, cm.matter_file, cm.matter_type,
                    cm.title, cm.sponsors, cm.canonical_summary, cm.canonical_topics,
                    cm.attachments, cm.metadata, cm.first_seen, cm.last_seen,
                    cm.appearance_count, cm.status, cm.final_vote_date, cm.quality_score, cm.rating_count,
                    ts_rank(cm.search_vector, plainto_tsquery('english', $1)) AS rank
                FROM city_matters cm
                WHERE cm.banana = $2
                  AND (
                      cm.search_vector @@ plainto_tsquery('english', $1)
                      OR cm.matter_file ILIKE '%' || $1 || '%'
                  )
                  AND EXISTS (SELECT 1 FROM items i WHERE i.matter_id = cm.id)
                ORDER BY rank DESC, cm.last_seen DESC
                LIMIT $3
                """,
                query,
                banana,
                limit,
            )

            if not rows:
                return []

            matter_ids = [row["id"] for row in rows]
            topics_by_matter = await fetch_topics_for_ids(
                conn, "matter_topics", "matter_id", matter_ids
            )

            return [
                build_matter(row, topics_by_matter.get(row["id"]) or None)
                for row in rows
            ]

    async def update_appearance_outcome(
        self,
        matter_id: str,
        meeting_id: str,
        item_id: str,
        vote_outcome: str,
        vote_tally: dict,
        conn: Optional[Connection] = None
    ) -> None:
        """Update matter appearance with vote outcome and tally."""
        async with self._ensure_conn(conn) as c:
            await c.execute(
                """
                UPDATE matter_appearances
                SET vote_outcome = $1, vote_tally = $2
                WHERE matter_id = $3 AND meeting_id = $4 AND item_id = $5
                """,
                vote_outcome,
                vote_tally,
                matter_id,
                meeting_id,
                item_id,
            )

        logger.debug(
            "updated appearance outcome",
            matter_id=matter_id,
            meeting_id=meeting_id,
            outcome=vote_outcome
        )

    async def update_status(
        self,
        matter_id: str,
        status: str,
        final_vote_date: Optional[datetime] = None
    ) -> None:
        """Update matter disposition status when reaching a terminal state."""
        async with self.transaction() as conn:
            await conn.execute(
                """
                UPDATE city_matters
                SET status = $1,
                    final_vote_date = COALESCE($2, final_vote_date),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $3
                """,
                status,
                final_vote_date,
                matter_id,
            )

        logger.info(
            "updated matter status",
            matter_id=matter_id,
            status=status,
            final_vote_date=final_vote_date
        )

    async def get_matter_with_votes(self, matter_id: str) -> Optional[dict]:
        """Get matter with full vote history across all meetings."""
        async with self.pool.acquire() as conn:
            matter = await self.get_matter(matter_id)
            if not matter:
                return None

            appearances = await conn.fetch(
                """
                SELECT
                    ma.meeting_id,
                    ma.appeared_at,
                    ma.committee,
                    ma.vote_outcome,
                    ma.vote_tally,
                    m.title as meeting_title
                FROM matter_appearances ma
                JOIN meetings m ON m.id = ma.meeting_id
                WHERE ma.matter_id = $1
                ORDER BY ma.appeared_at DESC
                """,
                matter_id,
            )

            vote_history = [
                {
                    "meeting_id": row["meeting_id"],
                    "meeting_title": row["meeting_title"],
                    "date": row["appeared_at"].isoformat() if row["appeared_at"] else None,
                    "committee": row["committee"],
                    "outcome": row["vote_outcome"],
                    "tally": row["vote_tally"],
                }
                for row in appearances
            ]

            return {
                "matter": matter,
                "vote_history": vote_history,
            }

    async def get_matter_vote_outcomes(self, matter_id: str) -> List[dict]:
        """Get vote outcomes for a matter across all meetings where votes were recorded."""
        rows = await self._fetch(
            """
            SELECT
                ma.meeting_id,
                ma.vote_outcome,
                ma.vote_tally,
                ma.appeared_at,
                m.title as meeting_title
            FROM matter_appearances ma
            JOIN meetings m ON m.id = ma.meeting_id
            WHERE ma.matter_id = $1 AND ma.vote_outcome IS NOT NULL
            ORDER BY ma.appeared_at DESC
            """,
            matter_id
        )

        return [
            {
                "meeting_id": row["meeting_id"],
                "meeting_title": row["meeting_title"],
                "date": row["appeared_at"].isoformat() if row["appeared_at"] else None,
                "outcome": row["vote_outcome"],
                "tally": row["vote_tally"],
            }
            for row in rows
        ]

    async def search_by_keyword(
        self,
        bananas: List[str],
        keyword: str,
        since_date
    ) -> List[Dict]:
        """
        Search matters by keyword in canonical_summary.

        Used by userland matching engine for matter-level deduplication.

        Args:
            bananas: List of city banana identifiers
            keyword: Keyword to search (case-insensitive LIKE match)
            since_date: Only include matters seen after this date

        Returns:
            List of dicts with matter and city fields
        """
        if not bananas:
            return []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT cm.id, cm.banana, cm.matter_file, cm.matter_type,
                       cm.title, cm.canonical_summary, cm.sponsors,
                       cm.canonical_topics, cm.first_seen, cm.last_seen,
                       cm.appearance_count,
                       c.name as city_name, c.state
                FROM city_matters cm
                JOIN jurisdictions c ON cm.banana = c.banana
                JOIN LATERAL (
                    SELECT MAX(
                        COALESCE(ma.appeared_at, appearance_meeting.created_at)
                    ) AS latest_activity_at
                    FROM matter_appearances ma
                    JOIN meetings appearance_meeting
                      ON appearance_meeting.id = ma.meeting_id
                    WHERE ma.matter_id = cm.id
                ) freshness ON freshness.latest_activity_at >= $2
                WHERE cm.banana = ANY($1::text[])
                  AND cm.canonical_summary LIKE $3
                ORDER BY freshness.latest_activity_at DESC
                """,
                bananas,
                since_date,
                f"%{keyword}%",
            )

            return [dict(row) for row in rows]

    async def get_timeline(self, matter_id: str) -> List[Dict]:
        """
        Get chronological timeline of a matter's appearances.

        Used by userland matching to show matter evolution.

        Args:
            matter_id: Matter ID

        Returns:
            List of appearance dicts with meeting context, ordered by date
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ma.appeared_at, ma.committee, ma.action,
                       ma.item_id, ma.meeting_id,
                       m.title as meeting_title
                FROM matter_appearances ma
                JOIN meetings m ON ma.meeting_id = m.id
                WHERE ma.matter_id = $1
                  AND (m.status IS NULL OR m.status NOT IN ('cancelled', 'postponed'))
                ORDER BY ma.appeared_at
                """,
                matter_id,
            )

            return [dict(row) for row in rows]

    async def check_existing_match(self, alert_id: str, matter_id: str) -> bool:
        """
        Check if a matter was already matched for an alert.

        Used by userland matching to prevent duplicate notifications.

        Args:
            alert_id: Alert ID
            matter_id: Matter ID

        Returns:
            True if match already exists
        """
        async with self.pool.acquire() as conn:
            exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM userland.alert_matches
                    WHERE alert_id = $1
                      AND matched_criteria->>'matter_id' = $2
                )
                """,
                alert_id,
                matter_id,
            )
            return exists
