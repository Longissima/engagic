"""Async DeliberationRepository for deliberation feature

Handles CRUD operations for:
- Deliberations (linked to matters)
- Comments with trust-based moderation
- Votes (agree/disagree/pass)
- Clustering results caching
- Pseudonymous participant tracking
"""

import re
import secrets
from typing import Any, Dict, List, Optional, Union

import asyncpg
from asyncpg import Connection
import numpy as np

from database.repositories_async.base import BaseRepository
from config import get_logger

logger = get_logger(__name__).bind(component="deliberation_repository")

# Deliberation ID format: delib_{matter_id}_{short_hash}
# Example: delib_nashvilleTN_abc123_x4f9a2
DELIBERATION_ID_PATTERN = re.compile(r"^delib_.+_.{4,8}$")

# Moderation status constants
MOD_STATUS_REJECTED = -1  # Hidden/rejected by moderator
MOD_STATUS_PENDING = 0    # Awaiting moderation
MOD_STATUS_APPROVED = 1   # Approved and visible


def generate_deliberation_id(matter_id: str) -> str:
    """Generate unique deliberation ID from matter ID.

    Format: delib_{matter_id}_{short_hash}

    Args:
        matter_id: Parent matter ID (e.g., "nashvilleTN_abc123")

    Returns:
        Deliberation ID (e.g., "delib_nashvilleTN_abc123_x4f9")
    """
    short_hash = secrets.token_urlsafe(4)[:6]
    return f"delib_{matter_id}_{short_hash}"


def is_valid_deliberation_id(deliberation_id: str) -> bool:
    """Validate deliberation ID format.

    Args:
        deliberation_id: ID to validate

    Returns:
        True if format is valid (delib_{matter_id}_{hash})
    """
    if not deliberation_id or not isinstance(deliberation_id, str):
        return False
    return bool(DELIBERATION_ID_PATTERN.match(deliberation_id))


class DeliberationRepository(BaseRepository):
    """Repository for deliberation operations."""

    # -------------------------------------------------------------------------
    # Deliberation CRUD
    # -------------------------------------------------------------------------

    async def create_deliberation(
        self,
        matter_id: str,
        banana: str,
        topic: Optional[str] = None,
        conn: Optional[Connection] = None,
    ) -> Dict[str, Any]:
        """Create a new deliberation for a matter.

        Args:
            matter_id: Parent matter ID (must exist - FK constraint)
            banana: City identifier
            topic: Optional custom topic (defaults to matter title)
            conn: Optional connection for transaction participation

        Returns:
            Created deliberation record

        Note:
            Pass conn from caller's transaction when checking matter exists
            in the same transaction as creating the deliberation.
        """
        deliberation_id = generate_deliberation_id(matter_id)

        async with self._ensure_conn(conn) as c:
            await c.execute(
                """
                INSERT INTO deliberations (id, matter_id, banana, topic, is_active, created_at)
                VALUES ($1, $2, $3, $4, true, NOW())
                """,
                deliberation_id,
                matter_id,
                banana,
                topic,
            )

        logger.info(
            "created deliberation",
            deliberation_id=deliberation_id,
            matter_id=matter_id,
            banana=banana,
        )

        return {
            "id": deliberation_id,
            "matter_id": matter_id,
            "banana": banana,
            "topic": topic,
            "is_active": True,
        }

    async def get_deliberation(self, deliberation_id: str) -> Optional[Dict[str, Any]]:
        """Get a deliberation by ID.

        Args:
            deliberation_id: Deliberation ID

        Returns:
            Deliberation dict or None
        """
        # Early return for malformed IDs
        if not is_valid_deliberation_id(deliberation_id):
            logger.debug("invalid deliberation_id format", deliberation_id=deliberation_id)
            return None

        row = await self._fetchrow(
            """
            SELECT id, matter_id, banana, topic, is_active, created_at, closed_at
            FROM deliberations
            WHERE id = $1
            """,
            deliberation_id,
        )

        if not row:
            return None

        return dict(row)

    async def get_deliberation_for_matter(self, matter_id: str) -> Optional[Dict[str, Any]]:
        """Get active deliberation for a matter.

        Args:
            matter_id: Matter ID

        Returns:
            Deliberation dict or None
        """
        row = await self._fetchrow(
            """
            SELECT id, matter_id, banana, topic, is_active, created_at, closed_at
            FROM deliberations
            WHERE matter_id = $1 AND is_active = true
            ORDER BY created_at DESC
            LIMIT 1
            """,
            matter_id,
        )

        if not row:
            return None

        return dict(row)

    async def close_deliberation(self, deliberation_id: str) -> bool:
        """Close a deliberation (no more comments/votes).

        Args:
            deliberation_id: Deliberation ID

        Returns:
            True if updated, False if not found
        """
        result = await self._execute(
            """
            UPDATE deliberations
            SET is_active = false, closed_at = NOW()
            WHERE id = $1 AND is_active = true
            """,
            deliberation_id,
        )
        return self._parse_row_count(result) > 0

    # -------------------------------------------------------------------------
    # Participant Management (Pseudonyms)
    # -------------------------------------------------------------------------

    async def get_or_assign_participant_number(
        self, deliberation_id: str, user_id: str
    ) -> int:
        """Get or assign participant number for pseudonym display.

        First-time participants get the next available number.
        Returns existing number for returning participants.

        Args:
            deliberation_id: Deliberation ID
            user_id: User ID

        Returns:
            Participant number (1-indexed)
        """
        async with self.transaction() as conn:
            # Check for existing assignment
            existing = await conn.fetchrow(
                """
                SELECT participant_number
                FROM deliberation_participants
                WHERE deliberation_id = $1 AND user_id = $2
                """,
                deliberation_id,
                user_id,
            )

            if existing:
                return existing["participant_number"]

            # Get next available number with row lock to prevent race condition
            max_row = await conn.fetchrow(
                """
                SELECT COALESCE(MAX(participant_number), 0) as max_num
                FROM deliberation_participants
                WHERE deliberation_id = $1
                FOR UPDATE
                """,
                deliberation_id,
            )
            if max_row is None:  # pragma: no cover - aggregate always returns a row
                raise RuntimeError("participant number aggregate returned no row")
            next_number = (max_row["max_num"] or 0) + 1

            # Insert new participant
            await conn.execute(
                """
                INSERT INTO deliberation_participants (deliberation_id, user_id, participant_number)
                VALUES ($1, $2, $3)
                ON CONFLICT (deliberation_id, user_id) DO NOTHING
                """,
                deliberation_id,
                user_id,
                next_number,
            )

            return next_number

    # -------------------------------------------------------------------------
    # Trust Management
    # -------------------------------------------------------------------------

    async def is_user_trusted(self, user_id: str) -> bool:
        """Check if user is trusted (has had comments approved before).

        Args:
            user_id: User ID

        Returns:
            True if trusted
        """
        row = await self._fetchrow(
            """
            SELECT 1 FROM userland.deliberation_trusted_users
            WHERE user_id = $1
            """,
            user_id,
        )
        return row is not None

    async def mark_user_trusted(self, user_id: str) -> None:
        """Mark user as trusted (after first comment approval).

        Args:
            user_id: User ID
        """
        await self._execute(
            """
            INSERT INTO userland.deliberation_trusted_users (user_id, first_approved_at)
            VALUES ($1, NOW())
            ON CONFLICT (user_id) DO NOTHING
            """,
            user_id,
        )
        logger.info("marked user trusted", user_id=user_id)

    # -------------------------------------------------------------------------
    # Comment Operations
    # -------------------------------------------------------------------------

    async def create_comment(
        self,
        deliberation_id: str,
        user_id: str,
        txt: str,
    ) -> Union[Dict[str, Any], Dict[str, str]]:
        """Create a new comment.

        Auto-approves if user is trusted, otherwise queues for moderation.
        All queries use single connection for atomicity.

        Args:
            deliberation_id: Deliberation ID
            user_id: User ID
            txt: Comment text

        Returns:
            Created comment dict with mod_status, or {"error": "duplicate"}
        """
        # Note: participant assignment logic is duplicated from get_or_assign_participant_number()
        # because all operations must use the same connection for transactional atomicity.
        async with self.transaction() as conn:
            existing = await conn.fetchrow(
                """
                SELECT participant_number FROM deliberation_participants
                WHERE deliberation_id = $1 AND user_id = $2
                """,
                deliberation_id,
                user_id,
            )
            if existing:
                participant_number = existing["participant_number"]
            else:
                # Lock rows to prevent race condition on participant number
                max_row = await conn.fetchrow(
                    """
                    SELECT COALESCE(MAX(participant_number), 0) as max_num
                    FROM deliberation_participants WHERE deliberation_id = $1
                    FOR UPDATE
                    """,
                    deliberation_id,
                )
                if max_row is None:  # pragma: no cover - aggregate always yields
                    raise RuntimeError("participant number aggregate returned no row")
                participant_number = (max_row["max_num"] or 0) + 1
                await conn.execute(
                    """
                    INSERT INTO deliberation_participants (deliberation_id, user_id, participant_number)
                    VALUES ($1, $2, $3) ON CONFLICT (deliberation_id, user_id) DO NOTHING
                    """,
                    deliberation_id,
                    user_id,
                    participant_number,
                )

            # Check trust status (inlined for atomicity)
            trust_row = await conn.fetchrow(
                "SELECT 1 FROM userland.deliberation_trusted_users WHERE user_id = $1",
                user_id,
            )
            mod_status = MOD_STATUS_APPROVED if trust_row else MOD_STATUS_PENDING

            # Insert comment
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO deliberation_comments
                        (deliberation_id, user_id, participant_number, txt, mod_status, created_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    RETURNING id, deliberation_id, user_id, participant_number, txt, mod_status, created_at
                    """,
                    deliberation_id,
                    user_id,
                    participant_number,
                    txt,
                    mod_status,
                )
            except asyncpg.UniqueViolationError:
                logger.warning(
                    "duplicate comment rejected",
                    deliberation_id=deliberation_id,
                    user_id=user_id,
                    participant_number=participant_number,
                )
                return {"error": "duplicate"}

            if row is None:  # pragma: no cover - INSERT RETURNING always yields
                raise RuntimeError("comment insert returned no row")

            logger.info(
                "created comment",
                comment_id=row["id"],
                deliberation_id=deliberation_id,
                participant_number=participant_number,
                mod_status=mod_status,
            )

            return dict(row)

    async def get_comments(
        self,
        deliberation_id: str,
        include_pending: bool = False,
        include_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        """Get comments for a deliberation.

        Args:
            deliberation_id: Deliberation ID
            include_pending: Include pending comments (mod_status=0)
            include_hidden: Include hidden comments (mod_status=-1)

        Returns:
            List of comment dicts
        """
        # Build mod_status filter
        statuses = [1]  # Always include approved
        if include_pending:
            statuses.append(0)
        if include_hidden:
            statuses.append(-1)

        rows = await self._fetch(
            """
            SELECT id, deliberation_id, user_id, participant_number, txt, mod_status, created_at
            FROM deliberation_comments
            WHERE deliberation_id = $1 AND mod_status = ANY($2)
            ORDER BY created_at ASC
            """,
            deliberation_id,
            statuses,
        )

        return [dict(row) for row in rows]

    async def get_pending_comments(self, deliberation_id: str) -> List[Dict[str, Any]]:
        """Get pending comments for moderation.

        Args:
            deliberation_id: Deliberation ID

        Returns:
            List of pending comment dicts
        """
        rows = await self._fetch(
            """
            SELECT id, deliberation_id, user_id, participant_number, txt, mod_status, created_at
            FROM deliberation_comments
            WHERE deliberation_id = $1 AND mod_status = 0
            ORDER BY created_at ASC
            """,
            deliberation_id,
        )

        return [dict(row) for row in rows]

    async def moderate_comment(self, comment_id: int, approve: bool) -> bool:
        """Approve or hide a comment.

        If approving, also marks the user as trusted for future auto-approval.

        Args:
            comment_id: Comment ID
            approve: True to approve (mod_status=1), False to hide (mod_status=-1)

        Returns:
            True if updated, False if not found
        """
        async with self.transaction() as conn:
            # Get comment details for trust marking
            row = await conn.fetchrow(
                """
                SELECT user_id, mod_status
                FROM deliberation_comments
                WHERE id = $1
                """,
                comment_id,
            )

            if not row:
                return False

            new_status = MOD_STATUS_APPROVED if approve else MOD_STATUS_REJECTED

            # Update comment status
            await conn.execute(
                """
                UPDATE deliberation_comments
                SET mod_status = $1
                WHERE id = $2
                """,
                new_status,
                comment_id,
            )

            # If approving, mark user as trusted
            if approve:
                await conn.execute(
                    """
                    INSERT INTO userland.deliberation_trusted_users (user_id, first_approved_at)
                    VALUES ($1, NOW())
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    row["user_id"],
                )

            logger.info(
                "moderated comment",
                comment_id=comment_id,
                approve=approve,
                user_id=row["user_id"],
            )

            return True

    # -------------------------------------------------------------------------
    # Vote Operations
    # -------------------------------------------------------------------------

    async def record_vote(
        self,
        comment_id: int,
        user_id: str,
        vote: int,
    ) -> Optional[Dict[str, str]]:
        """Record a vote on a comment.

        Upserts - replaces existing vote if user changes their mind.

        Args:
            comment_id: Comment ID
            user_id: User ID
            vote: -1 (disagree), 0 (pass), 1 (agree)

        Returns:
            None on success, {"error": "not_found"} if comment doesn't exist
        """
        if vote not in (-1, 0, 1):
            raise ValueError(f"Invalid vote value: {vote}. Must be -1, 0, or 1.")

        try:
            await self._execute(
                """
                INSERT INTO deliberation_votes (comment_id, user_id, vote, created_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (comment_id, user_id) DO UPDATE SET
                    vote = EXCLUDED.vote,
                    created_at = NOW()
                """,
                comment_id,
                user_id,
                vote,
            )
            return None
        except asyncpg.ForeignKeyViolationError:
            logger.warning(
                "vote on non-existent comment",
                comment_id=comment_id,
                user_id=user_id,
                vote=vote,
            )
            return {"error": "not_found"}

    async def get_user_votes(
        self, deliberation_id: str, user_id: str
    ) -> Dict[int, int]:
        """Get user's votes for all comments in a deliberation.

        Args:
            deliberation_id: Deliberation ID
            user_id: User ID

        Returns:
            Dict mapping comment_id -> vote value
        """
        rows = await self._fetch(
            """
            SELECT dv.comment_id, dv.vote
            FROM deliberation_votes dv
            JOIN deliberation_comments dc ON dv.comment_id = dc.id
            WHERE dc.deliberation_id = $1 AND dv.user_id = $2
            """,
            deliberation_id,
            user_id,
        )

        return {row["comment_id"]: row["vote"] for row in rows}

    async def get_vote_matrix(
        self, deliberation_id: str
    ) -> tuple[np.ndarray, List[str], List[int]]:
        """Build vote matrix for clustering algorithm.

        Returns matrix where:
        - Rows = participants
        - Columns = comments
        - Values = votes (-1, 0, 1) or NaN for unvoted

        Only includes approved comments (mod_status=1).

        Args:
            deliberation_id: Deliberation ID

        Returns:
            Tuple of (vote_matrix, user_ids, comment_ids)
        """
        async with self.pool.acquire() as conn:
            # Get approved comments
            comments = await conn.fetch(
                """
                SELECT id FROM deliberation_comments
                WHERE deliberation_id = $1 AND mod_status = 1
                ORDER BY id
                """,
                deliberation_id,
            )
            comment_ids = [row["id"] for row in comments]

            if not comment_ids:
                return np.array([]).reshape(0, 0), [], []

            # Get all participants who voted
            participants = await conn.fetch(
                """
                SELECT DISTINCT dv.user_id
                FROM deliberation_votes dv
                JOIN deliberation_comments dc ON dv.comment_id = dc.id
                WHERE dc.deliberation_id = $1 AND dc.mod_status = 1
                ORDER BY dv.user_id
                """,
                deliberation_id,
            )
            user_ids = [row["user_id"] for row in participants]

            if not user_ids:
                return np.array([]).reshape(0, 0), [], []

            # Get all votes
            votes = await conn.fetch(
                """
                SELECT dv.user_id, dv.comment_id, dv.vote
                FROM deliberation_votes dv
                JOIN deliberation_comments dc ON dv.comment_id = dc.id
                WHERE dc.deliberation_id = $1 AND dc.mod_status = 1
                """,
                deliberation_id,
            )

            # Build matrix: indices guaranteed by SQL join constraints
            user_idx = {uid: i for i, uid in enumerate(user_ids)}
            comment_idx = {cid: i for i, cid in enumerate(comment_ids)}

            matrix = np.full((len(user_ids), len(comment_ids)), np.nan)
            for row in votes:
                matrix[user_idx[row["user_id"]], comment_idx[row["comment_id"]]] = row["vote"]

            return matrix, user_ids, comment_ids

    # -------------------------------------------------------------------------
    # Results Caching
    # -------------------------------------------------------------------------

    async def save_results(
        self,
        deliberation_id: str,
        results: Dict[str, Any],
    ) -> None:
        """Save clustering results.

        Args:
            deliberation_id: Deliberation ID
            results: Clustering results from compute_deliberation_clusters
        """
        await self._execute(
            """
            INSERT INTO deliberation_results
                (deliberation_id, n_participants, n_comments, k, positions, clusters,
                 cluster_centers, consensus, group_votes, computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
            ON CONFLICT (deliberation_id) DO UPDATE SET
                n_participants = EXCLUDED.n_participants,
                n_comments = EXCLUDED.n_comments,
                k = EXCLUDED.k,
                positions = EXCLUDED.positions,
                clusters = EXCLUDED.clusters,
                cluster_centers = EXCLUDED.cluster_centers,
                consensus = EXCLUDED.consensus,
                group_votes = EXCLUDED.group_votes,
                computed_at = NOW()
            """,
            deliberation_id,
            results["n_participants"],
            results["n_comments"],
            results["k"],
            results.get("positions"),
            results.get("clusters"),
            results.get("cluster_centers"),
            results.get("consensus"),
            results.get("group_votes"),
        )

        logger.info(
            "saved clustering results",
            deliberation_id=deliberation_id,
            n_participants=results["n_participants"],
            n_comments=results["n_comments"],
            k=results["k"],
        )

    async def get_results(self, deliberation_id: str) -> Optional[Dict[str, Any]]:
        """Get cached clustering results.

        Args:
            deliberation_id: Deliberation ID

        Returns:
            Results dict or None
        """
        row = await self._fetchrow(
            """
            SELECT deliberation_id, n_participants, n_comments, k, positions,
                   clusters, cluster_centers, consensus, group_votes, computed_at
            FROM deliberation_results
            WHERE deliberation_id = $1
            """,
            deliberation_id,
        )

        if not row:
            return None

        return dict(row)

    # -------------------------------------------------------------------------
    # Statistics
    # -------------------------------------------------------------------------

    async def get_deliberation_stats(self, deliberation_id: str) -> Dict[str, int]:
        """Get participation stats for a deliberation.

        Args:
            deliberation_id: Deliberation ID

        Returns:
            Stats dict with comment_count, vote_count, participant_count
        """
        row = await self._fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM deliberation_comments
                 WHERE deliberation_id = $1 AND mod_status = 1) as comment_count,
                (SELECT COUNT(*) FROM deliberation_votes dv
                 JOIN deliberation_comments dc ON dv.comment_id = dc.id
                 WHERE dc.deliberation_id = $1 AND dc.mod_status = 1) as vote_count,
                (SELECT COUNT(*) FROM deliberation_participants
                 WHERE deliberation_id = $1) as participant_count
            """,
            deliberation_id,
        )

        if row is None:
            return {"comment_count": 0, "vote_count": 0, "participant_count": 0}

        return {
            "comment_count": row["comment_count"] or 0,
            "vote_count": row["vote_count"] or 0,
            "participant_count": row["participant_count"] or 0,
        }
