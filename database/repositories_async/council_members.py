"""Async CouncilMemberRepository for council member and sponsorship operations

Handles CRUD operations for council members (elected officials):
- Find or create council members from sponsor names
- Link council members to matters via sponsorships
- Retrieve sponsorship history for members and matters
- Update member statistics (sponsorship_count, last_seen)

Design:
- Normalizes sponsor names for matching across vendor variations
- ID includes city_banana to prevent cross-city collisions
- Denormalized sponsorship_count for quick stats queries
"""

from typing import Any, Dict, List, Mapping, Optional
from datetime import datetime

from asyncpg import Connection

from database.repositories_async.base import BaseRepository
from database.models import CouncilMember, Vote
from database.id_generation import (
    generate_council_member_id,
    normalize_sponsor_name,
)
from config import get_logger

logger = get_logger(__name__).bind(component="council_member_repository")


class CouncilMemberRepository(BaseRepository):
    """Repository for council member and sponsorship operations

    Provides:
    - Find or create council members by name
    - Link members to matters via sponsorships
    - Retrieve members by city
    - Get sponsorship history
    """

    @staticmethod
    def _normalize_vote_value(value: object) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        normalized = value.lower().strip()
        valid_votes = {
            "yes",
            "no",
            "abstain",
            "absent",
            "present",
            "recused",
            "not_voting",
        }
        if normalized in valid_votes:
            return normalized
        return {
            "aye": "yes",
            "yea": "yes",
            "nay": "no",
            "abstained": "abstain",
            "excused": "absent",
            "not present": "absent",
            "recuse": "recused",
        }.get(normalized, "not_voting")

    @staticmethod
    async def _lock_attribution_scope(
        banana: str,
        conn: Connection,
    ) -> None:
        """Serialize exact sponsor/vote projections for one jurisdiction."""
        await conn.execute(
            """
            SELECT pg_advisory_xact_lock(
                hashtextextended('council-attribution:' || $1, 0)
            )
            """,
            banana,
        )

    @staticmethod
    async def _recompute_attribution_counts(
        member_ids: set[str],
        conn: Connection,
    ) -> None:
        """Replace both denormalized counters from retained relationships."""
        ordered_ids = sorted(member_ids)
        if not ordered_ids:
            return
        await conn.fetch(
            """
            SELECT id
            FROM council_members
            WHERE id = ANY($1::text[])
            ORDER BY id
            FOR UPDATE
            """,
            ordered_ids,
        )
        await conn.execute(
            """
            UPDATE council_members cm
            SET sponsorship_count = (
                    SELECT COUNT(*)::int
                    FROM sponsorships s
                    WHERE s.council_member_id = cm.id
                ),
                vote_count = (
                    SELECT COUNT(*)::int
                    FROM votes v
                    WHERE v.council_member_id = cm.id
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE cm.id = ANY($1::text[])
            """,
            ordered_ids,
        )

    async def find_or_create_member(
        self,
        banana: str,
        name: str,
        appeared_at: Optional[datetime] = None,
        conn: Optional[Connection] = None,
    ) -> CouncilMember:
        """Find existing council member or create new one.

        Uses normalized name for matching. Updates last_seen if existing member found.
        """
        normalized = normalize_sponsor_name(name)
        member_id = generate_council_member_id(banana, name)

        async with self._ensure_conn(conn) as c:
            # Try to find existing member
            row = await c.fetchrow(
                """
                SELECT id, banana, name, normalized_name, title, district,
                       status, first_seen, last_seen, sponsorship_count, vote_count, metadata
                FROM council_members
                WHERE id = $1
                """,
                member_id,
            )

            if row:
                # Update last_seen if newer
                if appeared_at and (not row["last_seen"] or appeared_at > row["last_seen"]):
                    await c.execute(
                        """
                        UPDATE council_members
                        SET last_seen = $2, updated_at = CURRENT_TIMESTAMP
                        WHERE id = $1
                        """,
                        member_id,
                        appeared_at,
                    )

                raw_meta = row["metadata"]
                if raw_meta is not None and not isinstance(raw_meta, dict):
                    logger.warning("corrupted council member metadata, coercing to dict", member_id=row["id"], raw_type=type(raw_meta).__name__)
                    raw_meta = {} if not isinstance(raw_meta, dict) else raw_meta

                return CouncilMember(
                    id=row["id"],
                    banana=row["banana"],
                    name=row["name"],
                    normalized_name=row["normalized_name"],
                    title=row["title"],
                    district=row["district"],
                    status=row["status"],
                    first_seen=row["first_seen"],
                    last_seen=appeared_at or row["last_seen"],
                    sponsorship_count=row["sponsorship_count"],
                    vote_count=row["vote_count"],
                    metadata=raw_meta,
                )

            # Create new member
            await c.execute(
                """
                INSERT INTO council_members (
                    id, banana, name, normalized_name, status,
                    first_seen, last_seen, sponsorship_count
                )
                VALUES ($1, $2, $3, $4, 'active', $5, $5, 0)
                """,
                member_id,
                banana,
                name,
                normalized,
                appeared_at,
            )

            logger.info("created council member", member_id=member_id, name=name, banana=banana)

            return CouncilMember(
                id=member_id,
                banana=banana,
                name=name,
                normalized_name=normalized,
                status="active",
                first_seen=appeared_at,
                last_seen=appeared_at,
                sponsorship_count=0,
                vote_count=0,
            )

    async def update_member_metadata(
        self,
        member_id: str,
        title: Optional[str] = None,
        district: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> bool:
        """Update council member with additional metadata from roster sync.

        Args:
            member_id: Council member ID
            title: Role title (e.g., "Council Member", "CHAIRPERSON")
            district: District name (e.g., "District 35")
            status: Member status (active, former, unknown)
            metadata: Additional data (email, phone, url, etc.)

        Returns:
            True if member was updated, False if member not found
        """
        import json

        async with self.transaction() as conn:
            # Build dynamic SET clause - only update non-None fields
            updates = []
            params = [member_id]
            param_idx = 2

            if title is not None:
                updates.append(f"title = ${param_idx}")
                params.append(title)
                param_idx += 1

            if district is not None:
                updates.append(f"district = ${param_idx}")
                params.append(district)
                param_idx += 1

            if status is not None:
                updates.append(f"status = ${param_idx}")
                params.append(status)
                param_idx += 1

            if metadata is not None:
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                # Use JSONB merge to preserve existing keys while adding new ones
                updates.append(f"metadata = COALESCE(metadata, '{{}}'::jsonb) || ${param_idx}::jsonb")
                params.append(json.dumps(metadata))
                param_idx += 1

            if not updates:
                return False

            updates.append("updated_at = CURRENT_TIMESTAMP")

            query = f"""
                UPDATE council_members
                SET {', '.join(updates)}
                WHERE id = $1
            """

            result = await conn.execute(query, *params)

            if self._parse_row_count(result) == 0:
                return False

            logger.debug(
                "updated council member metadata",
                member_id=member_id,
                title=title,
                district=district,
            )

            return True

    async def create_sponsorship(
        self,
        council_member_id: str,
        matter_id: str,
        is_primary: bool = False,
        sponsor_order: Optional[int] = None,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Create sponsorship link between council member and matter.

        Uses UPSERT with RETURNING - only increments sponsorship_count on actual insert.
        Returns True if new sponsorship created, False if already exists.
        """
        async with self._ensure_conn(conn) as c:
            # Use RETURNING to detect if insert succeeded (no redundant SELECT)
            result = await c.fetchval(
                """
                INSERT INTO sponsorships (council_member_id, matter_id, is_primary, sponsor_order)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (council_member_id, matter_id) DO NOTHING
                RETURNING id
                """,
                council_member_id,
                matter_id,
                is_primary,
                sponsor_order,
            )

            if not result:
                # ON CONFLICT triggered - sponsorship already exists
                return False

            # Only increment count when INSERT actually succeeded
            await c.execute(
                """
                UPDATE council_members
                SET sponsorship_count = sponsorship_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                council_member_id,
            )

            logger.debug(
                "created sponsorship",
                council_member_id=council_member_id,
                matter_id=matter_id,
                is_primary=is_primary,
            )

            return True

    async def get_members_by_city(
        self,
        banana: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[CouncilMember]:
        """Get all council members for a city

        Args:
            banana: City identifier
            status: Filter by status (active, former, unknown)
            limit: Maximum results

        Returns:
            List of CouncilMember objects sorted by sponsorship_count desc
        """
        if status:
            rows = await self._fetch(
                """
                SELECT id, banana, name, normalized_name, title, district,
                       status, first_seen, last_seen, sponsorship_count, vote_count, metadata
                FROM council_members
                WHERE banana = $1 AND status = $2
                ORDER BY sponsorship_count DESC
                LIMIT $3
                """,
                banana,
                status,
                limit,
            )
        else:
            rows = await self._fetch(
                """
                SELECT id, banana, name, normalized_name, title, district,
                       status, first_seen, last_seen, sponsorship_count, vote_count, metadata
                FROM council_members
                WHERE banana = $1
                ORDER BY sponsorship_count DESC
                LIMIT $2
                """,
                banana,
                limit,
            )

        return [
            CouncilMember(
                id=row["id"],
                banana=row["banana"],
                name=row["name"],
                normalized_name=row["normalized_name"],
                title=row["title"],
                district=row["district"],
                status=row["status"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                sponsorship_count=row["sponsorship_count"],
                vote_count=row["vote_count"],
                metadata=row["metadata"],
            )
            for row in rows
        ]

    async def get_member_by_id(self, member_id: str) -> Optional[CouncilMember]:
        """Get council member by ID

        Args:
            member_id: Council member ID

        Returns:
            CouncilMember object or None
        """
        row = await self._fetchrow(
            """
            SELECT id, banana, name, normalized_name, title, district,
                   status, first_seen, last_seen, sponsorship_count, vote_count, metadata
            FROM council_members
            WHERE id = $1
            """,
            member_id,
        )

        if not row:
            return None

        return CouncilMember(
            id=row["id"],
            banana=row["banana"],
            name=row["name"],
            normalized_name=row["normalized_name"],
            title=row["title"],
            district=row["district"],
            status=row["status"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            sponsorship_count=row["sponsorship_count"],
            vote_count=row["vote_count"],
            metadata=row["metadata"],
        )

    async def get_sponsors_for_matter(self, matter_id: str) -> List[CouncilMember]:
        """Get all sponsors for a matter

        Args:
            matter_id: Matter ID

        Returns:
            List of CouncilMember objects, ordered by sponsor_order
        """
        rows = await self._fetch(
            """
            SELECT cm.id, cm.banana, cm.name, cm.normalized_name, cm.title,
                   cm.district, cm.status, cm.first_seen, cm.last_seen,
                   cm.sponsorship_count, cm.vote_count, cm.metadata, s.is_primary, s.sponsor_order
            FROM council_members cm
            JOIN sponsorships s ON cm.id = s.council_member_id
            WHERE s.matter_id = $1
            ORDER BY s.sponsor_order ASC NULLS LAST, s.is_primary DESC
            """,
            matter_id,
        )

        return [
            CouncilMember(
                id=row["id"],
                banana=row["banana"],
                name=row["name"],
                normalized_name=row["normalized_name"],
                title=row["title"],
                district=row["district"],
                status=row["status"],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                sponsorship_count=row["sponsorship_count"],
                vote_count=row["vote_count"],
                metadata=row["metadata"],
            )
            for row in rows
        ]

    async def get_matters_by_sponsor(
        self,
        council_member_id: str,
        limit: int = 50,
    ) -> List[Dict]:
        """Get all matters sponsored by a council member

        Args:
            council_member_id: Council member ID
            limit: Maximum results

        Returns:
            List of matter dicts with sponsorship info
        """
        rows = await self._fetch(
            """
            SELECT m.id, m.banana, m.matter_file, m.title, m.matter_type,
                   m.canonical_summary, m.first_seen, m.last_seen,
                   s.is_primary, s.sponsor_order
            FROM city_matters m
            JOIN sponsorships s ON m.id = s.matter_id
            WHERE s.council_member_id = $1
            ORDER BY m.last_seen DESC NULLS LAST
            LIMIT $2
            """,
            council_member_id,
            limit,
        )

        return [
            {
                "id": row["id"],
                "banana": row["banana"],
                "matter_file": row["matter_file"],
                "title": row["title"],
                "matter_type": row["matter_type"],
                "canonical_summary": row["canonical_summary"],
                "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
                "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
                "is_primary": row["is_primary"],
                "sponsor_order": row["sponsor_order"],
            }
            for row in rows
        ]

    async def link_sponsors_to_matter(
        self,
        banana: str,
        matter_id: str,
        sponsor_names: List[str],
        appeared_at: Optional[datetime] = None,
        conn: Optional[Connection] = None,
    ) -> int:
        """Link multiple sponsors to a matter. Creates members if needed.

        Returns number of new sponsorships created.
        """
        created_count = 0

        for order, name in enumerate(sponsor_names, start=1):
            if not name or not name.strip():
                continue

            # Find or create council member
            member = await self.find_or_create_member(banana, name, appeared_at, conn=conn)

            # Create sponsorship (first sponsor is primary)
            is_primary = (order == 1)
            if await self.create_sponsorship(member.id, matter_id, is_primary, order, conn=conn):
                created_count += 1

        if created_count > 0:
            logger.info(
                "linked sponsors to matter",
                matter_id=matter_id,
                sponsor_count=len(sponsor_names),
                new_sponsorships=created_count,
            )

        return created_count

    async def reconcile_matter_sponsorships(
        self,
        *,
        banana: str,
        affected_matter_ids: List[str],
        conn: Connection,
    ) -> Dict[str, int]:
        """Make aggregate sponsorships equal retained item sponsor evidence.

        Appearances use the same stable ``meeting_id, sequence, item_id`` order
        as matter work snapshots. Sponsor list order is preserved within each
        appearance. The first unique normalized sponsor in that total order is
        the one aggregate primary;
        older appearances contribute any additional still-supported sponsors.
        Thus an A -> B relink removes unsupported A relationships without
        discarding a sponsor that another retained A appearance still names.

        Exact sponsor and vote reconciliation share one city advisory lock.
        This keeps overlapping member rows from being acquired in inverse
        order by concurrent meeting transactions.
        """
        matter_ids = sorted(set(affected_matter_ids))
        if not matter_ids:
            return {"desired": 0, "deleted": 0, "members_recounted": 0}

        await self._lock_attribution_scope(banana, conn)
        rows = await conn.fetch(
            """
            SELECT i.matter_id, i.meeting_id, i.id AS item_id, i.sequence,
                   i.sponsors, m.date AS meeting_date
            FROM items i
            JOIN meetings m ON m.id = i.meeting_id
            WHERE i.matter_id = ANY($1::text[])
            ORDER BY i.matter_id, i.meeting_id, i.sequence, i.id
            """,
            matter_ids,
        )
        existing_rows = await conn.fetch(
            """
            SELECT council_member_id, matter_id
            FROM sponsorships
            WHERE matter_id = ANY($1::text[])
            ORDER BY council_member_id, matter_id
            FOR UPDATE
            """,
            matter_ids,
        )

        desired_names: Dict[str, str] = {}
        desired_last_seen: Dict[str, Optional[datetime]] = {}
        desired_by_matter: Dict[str, List[str]] = {
            matter_id: [] for matter_id in matter_ids
        }
        seen_by_matter: Dict[str, set[str]] = {
            matter_id: set() for matter_id in matter_ids
        }
        for row in rows:
            matter_id = row["matter_id"]
            raw_sponsors = row["sponsors"] or []
            sponsor_names = (
                [raw_sponsors] if isinstance(raw_sponsors, str) else raw_sponsors
            )
            if not isinstance(sponsor_names, list):
                continue
            for raw_name in sponsor_names:
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                name = raw_name.strip()
                member_id = generate_council_member_id(banana, name)
                desired_names.setdefault(member_id, name)
                appeared_at = row["meeting_date"]
                prior_seen = desired_last_seen.get(member_id)
                if appeared_at is not None and (
                    prior_seen is None or appeared_at > prior_seen
                ):
                    desired_last_seen[member_id] = appeared_at
                else:
                    desired_last_seen.setdefault(member_id, prior_seen)
                if member_id not in seen_by_matter[matter_id]:
                    seen_by_matter[matter_id].add(member_id)
                    desired_by_matter[matter_id].append(member_id)

        existing_member_ids = {
            row["council_member_id"] for row in existing_rows
        }
        touched_member_ids = existing_member_ids | set(desired_names)
        for member_id in sorted(desired_names):
            member = await self.find_or_create_member(
                banana,
                desired_names[member_id],
                desired_last_seen.get(member_id),
                conn=conn,
            )
            if member.id != member_id:  # pragma: no cover - ID helper invariant
                raise RuntimeError(
                    "council member identity changed during sponsorship reconcile"
                )

        desired_records: List[tuple[str, str, bool, int]] = []
        for matter_id in matter_ids:
            for order, member_id in enumerate(
                desired_by_matter[matter_id],
                start=1,
            ):
                desired_records.append(
                    (member_id, matter_id, order == 1, order)
                )

        desired_matters = [record[1] for record in desired_records]
        desired_members = [record[0] for record in desired_records]
        deleted = await conn.execute(
            """
            DELETE FROM sponsorships s
            WHERE s.matter_id = ANY($1::text[])
              AND NOT EXISTS (
                  SELECT 1
                  FROM unnest(
                      $2::text[], $3::text[]
                  ) AS desired(matter_id, council_member_id)
                  WHERE desired.matter_id = s.matter_id
                    AND desired.council_member_id = s.council_member_id
              )
            """,
            matter_ids,
            desired_matters,
            desired_members,
        )
        if desired_records:
            await conn.executemany(
                """
                INSERT INTO sponsorships (
                    council_member_id, matter_id, is_primary, sponsor_order
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (council_member_id, matter_id) DO UPDATE SET
                    is_primary = EXCLUDED.is_primary,
                    sponsor_order = EXCLUDED.sponsor_order
                WHERE sponsorships.is_primary IS DISTINCT FROM
                          EXCLUDED.is_primary
                   OR sponsorships.sponsor_order IS DISTINCT FROM
                          EXCLUDED.sponsor_order
                """,
                desired_records,
            )
        await self._recompute_attribution_counts(touched_member_ids, conn)
        deleted_count = self._parse_row_count(deleted)
        logger.debug(
            "reconciled matter sponsorships",
            banana=banana,
            matters=len(matter_ids),
            desired=len(desired_records),
            deleted=deleted_count,
            members_recounted=len(touched_member_ids),
        )
        return {
            "desired": len(desired_records),
            "deleted": deleted_count,
            "members_recounted": len(touched_member_ids),
        }

    # ==================
    # VOTING METHODS
    # ==================

    async def record_vote(
        self,
        council_member_id: str,
        matter_id: str,
        meeting_id: str,
        vote: str,
        vote_date: Optional[datetime] = None,
        sequence: Optional[int] = None,
        metadata: Optional[dict] = None,
        conn: Optional[Connection] = None,
    ) -> bool:
        """Record a single vote for a council member on a matter in a meeting.

        Existing rows are corrected in place when a later authoritative scrape
        changes the vote, sequence, date, or metadata. The denormalized count
        increments only for a genuinely new relationship. Returns True for an
        insert and False for an update/no-op.
        """
        async with self._ensure_conn(conn) as c:
            # Use RETURNING to detect if insert succeeded (no redundant SELECT)
            result = await c.fetchval(
                """
                INSERT INTO votes (council_member_id, matter_id, meeting_id, vote, vote_date, sequence, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (council_member_id, matter_id, meeting_id) DO NOTHING
                RETURNING id
                """,
                council_member_id,
                matter_id,
                meeting_id,
                vote,
                vote_date,
                sequence,
                metadata,
            )

            if not result:
                await c.execute(
                    """
                    UPDATE votes
                    SET vote = $4,
                        vote_date = $5,
                        sequence = $6,
                        metadata = $7
                    WHERE council_member_id = $1
                      AND matter_id = $2
                      AND meeting_id = $3
                      AND (
                            vote IS DISTINCT FROM $4
                            OR vote_date IS DISTINCT FROM $5
                            OR sequence IS DISTINCT FROM $6
                            OR metadata IS DISTINCT FROM $7
                          )
                    """,
                    council_member_id,
                    matter_id,
                    meeting_id,
                    vote,
                    vote_date,
                    sequence,
                    metadata,
                )
                return False

            # Only increment count when INSERT actually succeeded
            await c.execute(
                """
                UPDATE council_members
                SET vote_count = vote_count + 1,
                    last_seen = GREATEST(last_seen, $2),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = $1
                """,
                council_member_id,
                vote_date,
            )

            logger.debug(
                "recorded vote",
                council_member_id=council_member_id,
                matter_id=matter_id,
                meeting_id=meeting_id,
                vote=vote,
            )

            return True

    async def record_votes_for_matter(
        self,
        banana: str,
        matter_id: str,
        meeting_id: str,
        votes: List[Dict],
        vote_date: Optional[datetime] = None,
        conn: Optional[Connection] = None,
    ) -> int:
        """Record all votes for a matter in a meeting. Creates members if needed.

        Returns number of new votes recorded.
        """
        recorded_count = 0

        for vote_data in votes:
            name = vote_data.get("name")
            vote_value = vote_data.get("vote")

            if not name or not vote_value:
                continue

            vote_value = self._normalize_vote_value(vote_value)
            if vote_value is None:
                continue

            # Find or create council member
            member = await self.find_or_create_member(banana, name, vote_date, conn=conn)

            # Record vote
            if await self.record_vote(
                council_member_id=member.id,
                matter_id=matter_id,
                meeting_id=meeting_id,
                vote=vote_value,
                vote_date=vote_date,
                sequence=vote_data.get("sequence"),
                metadata=vote_data.get("metadata"),
                conn=conn,
            ):
                recorded_count += 1

        if recorded_count > 0:
            logger.info(
                "recorded votes for matter",
                matter_id=matter_id,
                meeting_id=meeting_id,
                vote_count=len(votes),
                new_votes=recorded_count,
            )

        return recorded_count

    async def reconcile_meeting_votes(
        self,
        *,
        banana: str,
        meeting_id: str,
        affected_matter_ids: List[str],
        observed_votes: Mapping[str, List[Dict[str, Any]]],
        vote_date: Optional[datetime],
        conn: Connection,
    ) -> Dict[str, int]:
        """Correct observed votes and remove only provably orphaned rows.

        The vote schema has no item identifier. An omitted member list is
        therefore not sufficient evidence to delete votes while the matter
        still has any retained item in this meeting. This boundary safely:

        * upserts every observed current member/value for retained matters;
        * deletes affected matter votes only when that matter now has zero
          retained meeting items (the guaranteed A -> B stale-link case); and
        * recomputes exact counters for the union of old and new members.
        """
        matter_ids = sorted(set(affected_matter_ids))
        if not matter_ids:
            return {"observed": 0, "deleted": 0, "members_recounted": 0}

        await self._lock_attribution_scope(banana, conn)
        retained_rows = await conn.fetch(
            """
            SELECT DISTINCT matter_id
            FROM items
            WHERE meeting_id = $1
              AND matter_id = ANY($2::text[])
            ORDER BY matter_id
            """,
            meeting_id,
            matter_ids,
        )
        retained_matter_ids = {row["matter_id"] for row in retained_rows}
        existing_rows = await conn.fetch(
            """
            SELECT council_member_id, matter_id
            FROM votes
            WHERE meeting_id = $1
              AND matter_id = ANY($2::text[])
            ORDER BY council_member_id, matter_id
            FOR UPDATE
            """,
            meeting_id,
            matter_ids,
        )
        touched_member_ids = {
            row["council_member_id"] for row in existing_rows
        }

        desired_names: Dict[str, str] = {}
        desired_votes: Dict[tuple[str, str], Dict[str, Any]] = {}
        for matter_id in matter_ids:
            if matter_id not in retained_matter_ids:
                continue
            for vote_data in observed_votes.get(matter_id, []):
                name = vote_data.get("name")
                vote = self._normalize_vote_value(vote_data.get("vote"))
                if not isinstance(name, str) or not name.strip() or vote is None:
                    continue
                display_name = name.strip()
                member_id = generate_council_member_id(banana, display_name)
                key = (matter_id, member_id)
                if key in desired_votes:
                    continue
                desired_names.setdefault(member_id, display_name)
                desired_votes[key] = {
                    "vote": vote,
                    "sequence": vote_data.get("sequence"),
                    "metadata": vote_data.get("metadata"),
                }

        touched_member_ids.update(desired_names)
        for member_id in sorted(desired_names):
            member = await self.find_or_create_member(
                banana,
                desired_names[member_id],
                vote_date,
                conn=conn,
            )
            if member.id != member_id:  # pragma: no cover - ID helper invariant
                raise RuntimeError(
                    "council member identity changed during vote reconcile"
                )

        for matter_id, member_id in sorted(desired_votes):
            desired = desired_votes[(matter_id, member_id)]
            await self.record_vote(
                council_member_id=member_id,
                matter_id=matter_id,
                meeting_id=meeting_id,
                vote=desired["vote"],
                vote_date=vote_date,
                sequence=desired["sequence"],
                metadata=desired["metadata"],
                conn=conn,
            )

        deleted_rows = await conn.fetch(
            """
            DELETE FROM votes v
            WHERE v.meeting_id = $1
              AND v.matter_id = ANY($2::text[])
              AND NOT EXISTS (
                  SELECT 1
                  FROM items i
                  WHERE i.meeting_id = v.meeting_id
                    AND i.matter_id = v.matter_id
              )
            RETURNING v.council_member_id
            """,
            meeting_id,
            matter_ids,
        )
        touched_member_ids.update(
            row["council_member_id"] for row in deleted_rows
        )
        await self._recompute_attribution_counts(touched_member_ids, conn)
        logger.debug(
            "reconciled meeting votes",
            banana=banana,
            meeting_id=meeting_id,
            matters=len(matter_ids),
            observed=len(desired_votes),
            deleted=len(deleted_rows),
            members_recounted=len(touched_member_ids),
        )
        return {
            "observed": len(desired_votes),
            "deleted": len(deleted_rows),
            "members_recounted": len(touched_member_ids),
        }

    async def get_votes_for_meeting(
        self,
        meeting_id: str,
    ) -> List[Vote]:
        """Get all votes cast in a meeting

        Args:
            meeting_id: Meeting ID

        Returns:
            List of Vote objects
        """
        rows = await self._fetch(
            """
            SELECT id, council_member_id, matter_id, meeting_id, vote,
                   vote_date, sequence, metadata, created_at
            FROM votes
            WHERE meeting_id = $1
            ORDER BY matter_id, sequence ASC NULLS LAST
            """,
            meeting_id,
        )

        return [
            Vote(
                id=row["id"],
                council_member_id=row["council_member_id"],
                matter_id=row["matter_id"],
                meeting_id=row["meeting_id"],
                vote=row["vote"],
                vote_date=row["vote_date"],
                sequence=row["sequence"],
                metadata=row["metadata"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def get_votes_for_matter(
        self,
        matter_id: str,
    ) -> List[Vote]:
        """Get all votes cast on a matter (across all meetings)

        Args:
            matter_id: Matter ID

        Returns:
            List of Vote objects ordered by vote_date
        """
        rows = await self._fetch(
            """
            SELECT id, council_member_id, matter_id, meeting_id, vote,
                   vote_date, sequence, metadata, created_at
            FROM votes
            WHERE matter_id = $1
            ORDER BY vote_date DESC NULLS LAST, sequence ASC NULLS LAST
            """,
            matter_id,
        )

        return [
            Vote(
                id=row["id"],
                council_member_id=row["council_member_id"],
                matter_id=row["matter_id"],
                meeting_id=row["meeting_id"],
                vote=row["vote"],
                vote_date=row["vote_date"],
                sequence=row["sequence"],
                metadata=row["metadata"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def get_member_voting_record(
        self,
        council_member_id: str,
        limit: int = 100,
    ) -> List[Dict]:
        """Get voting history for a council member

        Args:
            council_member_id: Council member ID
            limit: Maximum results

        Returns:
            List of vote dicts with matter info
        """
        rows = await self._fetch(
            """
            SELECT v.id, v.matter_id, v.meeting_id, v.vote, v.vote_date, v.sequence,
                   m.matter_file, m.title, m.matter_type
            FROM votes v
            JOIN city_matters m ON v.matter_id = m.id
            WHERE v.council_member_id = $1
            ORDER BY v.vote_date DESC NULLS LAST
            LIMIT $2
            """,
            council_member_id,
            limit,
        )

        return [
            {
                "id": row["id"],
                "matter_id": row["matter_id"],
                "meeting_id": row["meeting_id"],
                "vote": row["vote"],
                "vote_date": row["vote_date"].isoformat() if row["vote_date"] else None,
                "sequence": row["sequence"],
                "matter_file": row["matter_file"],
                "title": row["title"],
                "matter_type": row["matter_type"],
            }
            for row in rows
        ]

    async def get_member_topic_profile(self, council_member_id: str) -> List[Dict]:
        """Per-topic voting profile for a member.

        Joins votes to the canonical topic vocabulary through both topic
        homes — matter_topics (canonical) and item_topics via the matter's
        items — so chunker-vendor matters without canonical summaries still
        contribute. "Votes yes on housing 94% of the time" is this query.
        """
        rows = await self._fetch(
            """
            SELECT t.topic, v.vote, COUNT(*) AS cnt
            FROM votes v
            JOIN LATERAL (
                SELECT mt.topic FROM matter_topics mt
                WHERE mt.matter_id = v.matter_id
                UNION
                SELECT it.topic
                FROM items i
                JOIN item_topics it ON it.item_id = i.id
                WHERE i.matter_id = v.matter_id
            ) t ON TRUE
            WHERE v.council_member_id = $1
            GROUP BY t.topic, v.vote
            ORDER BY t.topic
            """,
            council_member_id,
        )

        profile: Dict[str, Dict] = {}
        for row in rows:
            entry = profile.setdefault(row["topic"], {
                "topic": row["topic"],
                "yes": 0, "no": 0, "abstain": 0, "absent": 0, "other": 0,
                "total": 0,
            })
            bucket = row["vote"] if row["vote"] in ("yes", "no", "abstain", "absent") else "other"
            entry[bucket] += row["cnt"]
            entry["total"] += row["cnt"]

        out = []
        for entry in profile.values():
            decided = entry["yes"] + entry["no"]
            entry["yes_rate"] = round(entry["yes"] / decided, 3) if decided else None
            out.append(entry)
        out.sort(key=lambda e: e["total"], reverse=True)
        return out

    async def get_vote_tally_for_matter(
        self,
        matter_id: str,
        meeting_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Get vote tally (counts) for a matter

        Args:
            matter_id: Matter ID
            meeting_id: Optional meeting ID to filter to specific vote

        Returns:
            Dict with vote counts: {yes: N, no: N, abstain: N, ...}
        """
        if meeting_id:
            rows = await self._fetch(
                """
                SELECT vote, COUNT(*) as count
                FROM votes
                WHERE matter_id = $1 AND meeting_id = $2
                GROUP BY vote
                """,
                matter_id,
                meeting_id,
            )
        else:
            # Get most recent meeting's votes
            rows = await self._fetch(
                """
                SELECT vote, COUNT(*) as count
                FROM votes
                WHERE matter_id = $1
                  AND meeting_id = (
                      SELECT meeting_id FROM votes WHERE matter_id = $1
                      ORDER BY vote_date DESC NULLS LAST LIMIT 1
                  )
                GROUP BY vote
                """,
                matter_id,
            )

        return {row["vote"]: row["count"] for row in rows}
