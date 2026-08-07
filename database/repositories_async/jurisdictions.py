"""Async JurisdictionRepository for jurisdiction operations

Handles CRUD operations for jurisdictions (cities, counties, utility boards, etc.):
- Store/retrieve jurisdictions with zipcodes
- Filter by state, vendor, name, type
- Get meeting frequency statistics
- Get last sync timestamp
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from datetime import datetime

from database.repositories_async.base import BaseRepository
from database.repositories_async.helpers import deserialize_city_participation, deserialize_extra_vendors
from database.models import Jurisdiction
from config import get_logger

logger = get_logger(__name__).bind(component="jurisdiction_repository")


@dataclass(frozen=True)
class JurisdictionSyncStats:
    """Activity and durable lifecycle inputs for adaptive scheduling."""

    recent_meetings: int = 0
    last_synced_at: Optional[datetime] = None


def _build_jurisdiction(row, zipcodes: List[str]) -> Jurisdiction:
    """Factory to construct Jurisdiction from database row + zipcodes"""
    return Jurisdiction(
        banana=row["banana"],
        name=row["name"],
        state=row["state"],
        vendor=row["vendor"],
        slug=row["slug"],
        extra_vendors=deserialize_extra_vendors(row["extra_vendors"]),
        type=row["type"],
        county_banana=row["county_banana"],
        status=row["status"],
        participation=deserialize_city_participation(row["participation"]),
        zipcodes=zipcodes,
    )


class JurisdictionRepository(BaseRepository):
    """Repository for jurisdiction operations

    Provides:
    - Add jurisdictions with zipcode handling
    - Retrieve jurisdictions with filtering
    - Meeting frequency statistics
    - Last sync timestamp queries
    """

    async def add_city(self, city: Jurisdiction) -> None:
        """Add a jurisdiction to the database

        Args:
            city: Jurisdiction object with banana, name, state, vendor, slug

        Raises:
            asyncpg.UniqueViolationError: If jurisdiction already exists
        """
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO jurisdictions (banana, name, state, vendor, slug, extra_vendors, type, county_banana, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                city.banana,
                city.name,
                city.state,
                city.vendor,
                city.slug,
                json.dumps(city.extra_vendors) if city.extra_vendors else None,
                city.type,
                city.county_banana,
                city.status or "active",
            )

            # Insert zipcodes (batch for efficiency)
            if city.zipcodes:
                zipcode_records = [(city.banana, z, False) for z in city.zipcodes]
                await conn.executemany(
                    """
                    INSERT INTO zipcodes (banana, zipcode, is_primary)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (banana, zipcode) DO NOTHING
                    """,
                    zipcode_records,
                )

        logger.info("jurisdiction added", banana=city.banana, name=city.name, type=city.type)

    async def upsert_city(self, city: Jurisdiction) -> None:
        """Insert or update a jurisdiction in the database

        Args:
            city: Jurisdiction object with banana, name, state, vendor, slug
        """
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO jurisdictions (banana, name, state, vendor, slug, extra_vendors, type, county_banana, status, population)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (banana) DO UPDATE SET
                    name = EXCLUDED.name,
                    state = EXCLUDED.state,
                    vendor = EXCLUDED.vendor,
                    slug = EXCLUDED.slug,
                    extra_vendors = COALESCE(EXCLUDED.extra_vendors, jurisdictions.extra_vendors),
                    type = COALESCE(EXCLUDED.type, jurisdictions.type),
                    county_banana = COALESCE(EXCLUDED.county_banana, jurisdictions.county_banana),
                    status = COALESCE(EXCLUDED.status, jurisdictions.status),
                    population = COALESCE(EXCLUDED.population, jurisdictions.population)
                """,
                city.banana,
                city.name,
                city.state,
                city.vendor,
                city.slug,
                json.dumps(city.extra_vendors) if city.extra_vendors else None,
                city.type,
                city.county_banana,
                city.status or "active",
                city.population,
            )

            # Insert zipcodes (batch for efficiency)
            if city.zipcodes:
                zipcode_records = [(city.banana, z, False) for z in city.zipcodes]
                await conn.executemany(
                    """
                    INSERT INTO zipcodes (banana, zipcode, is_primary)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (banana, zipcode) DO NOTHING
                    """,
                    zipcode_records,
                )

        logger.info("jurisdiction upserted", banana=city.banana, name=city.name)

    async def get_city(self, banana: str) -> Optional[Jurisdiction]:
        """Get a jurisdiction by banana

        Args:
            banana: Jurisdiction banana (e.g., "paloaltoCA")

        Returns:
            Jurisdiction object with zipcodes, or None if not found
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT banana, name, state, vendor, slug, extra_vendors, type, county_banana, status, participation
                FROM jurisdictions
                WHERE banana = $1
                """,
                banana,
            )

            if not row:
                return None

            # Fetch zipcodes
            zipcodes_rows = await conn.fetch(
                """
                SELECT zipcode
                FROM zipcodes
                WHERE banana = $1
                """,
                banana,
            )
            zipcodes = [str(r["zipcode"]) for r in zipcodes_rows]

            return _build_jurisdiction(row, zipcodes)

    async def get_cities_by_bananas(
        self,
        bananas: Sequence[str],
        *,
        include_zipcodes: bool = False,
    ) -> Dict[str, Jurisdiction]:
        """Resolve jurisdiction identifiers in one database acquisition.

        Results are keyed by banana. Missing identifiers are absent, allowing
        callers to preserve their input order (and duplicate semantics) while
        handling missing jurisdictions explicitly.
        """
        unique_bananas = list(dict.fromkeys(bananas))
        if not unique_bananas:
            return {}

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT banana, name, state, vendor, slug, extra_vendors, type,
                       county_banana, status, participation
                FROM jurisdictions
                WHERE banana = ANY($1::text[])
                """,
                unique_bananas,
            )

            zipcodes_map: Dict[str, List[str]] = {}
            if include_zipcodes and rows:
                zipcode_rows = await conn.fetch(
                    """
                    SELECT banana, zipcode
                    FROM zipcodes
                    WHERE banana = ANY($1::text[])
                    """,
                    [row["banana"] for row in rows],
                )
                for row in zipcode_rows:
                    zipcodes_map.setdefault(row["banana"], []).append(
                        str(row["zipcode"])
                    )

        return {
            row["banana"]: _build_jurisdiction(
                row, zipcodes_map.get(row["banana"], [])
            )
            for row in rows
        }

    async def get_cities_by_states(
        self,
        states: Sequence[str],
        *,
        status: str = "active",
    ) -> Dict[str, List[Jurisdiction]]:
        """Resolve multiple state expansions in one query.

        Jurisdictions retain the same per-state name ordering as
        :meth:`get_cities`. Requested states with no matches map to an empty
        list so callers can distinguish an empty expansion without more I/O.
        """
        unique_states = list(dict.fromkeys(states))
        result: Dict[str, List[Jurisdiction]] = {
            state: [] for state in unique_states
        }
        if not unique_states:
            return result

        rows = await self._fetch(
            """
            SELECT banana, name, state, vendor, slug, extra_vendors, type,
                   county_banana, status, participation
            FROM jurisdictions
            WHERE state = ANY($1::text[]) AND status = $2
            ORDER BY state, name
            """,
            unique_states,
            status,
        )
        for row in rows:
            result[row["state"]].append(_build_jurisdiction(row, []))
        return result

    async def get_city_by_zipcode(self, zipcode: str) -> Optional[Jurisdiction]:
        """Get jurisdiction by zipcode lookup

        Args:
            zipcode: ZIP code to search for

        Returns:
            Jurisdiction object or None
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT c.banana, c.name, c.state, c.vendor, c.slug, c.extra_vendors, c.type, c.county_banana, c.status, c.participation
                FROM jurisdictions c
                INNER JOIN zipcodes z ON c.banana = z.banana
                WHERE z.zipcode = $1
                LIMIT 1
                """,
                zipcode,
            )

            if not row:
                return None

            # Fetch all zipcodes for this jurisdiction
            zip_rows = await conn.fetch(
                "SELECT zipcode FROM zipcodes WHERE banana = $1",
                row["banana"]
            )
            zipcodes = [str(r["zipcode"]) for r in zip_rows]

            return _build_jurisdiction(row, zipcodes)

    async def get_all_cities(
        self, status: str = "active", include_zipcodes: bool = False
    ) -> List[Jurisdiction]:
        """Get all jurisdictions with given status

        Args:
            status: Jurisdiction status filter (default: "active")
            include_zipcodes: If True, batch fetch zipcodes for all jurisdictions.
                             Default False for performance in search contexts.

        Returns:
            List of Jurisdiction objects (zipcodes empty unless include_zipcodes=True)
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT banana, name, state, vendor, slug, extra_vendors, type, county_banana, status, participation
                FROM jurisdictions
                WHERE status = $1
                ORDER BY name
                """,
                status,
            )

            zipcodes_map: Dict[str, List[str]] = {}
            if include_zipcodes and rows:
                bananas = [row["banana"] for row in rows]
                zipcodes_rows = await conn.fetch(
                    "SELECT banana, zipcode FROM zipcodes WHERE banana = ANY($1)",
                    bananas,
                )
                for zrow in zipcodes_rows:
                    zipcodes_map.setdefault(zrow["banana"], []).append(str(zrow["zipcode"]))

            return [_build_jurisdiction(row, zipcodes_map.get(row["banana"], [])) for row in rows]

    async def get_cities(
        self,
        state: Optional[str] = None,
        vendor: Optional[str] = None,
        name: Optional[str] = None,
        status: str = "active",
        limit: Optional[int] = None,
        include_zipcodes: bool = False,
    ) -> List[Jurisdiction]:
        """Batch jurisdiction lookup with filters

        Args:
            state: Filter by state (e.g., "CA")
            vendor: Filter by vendor (e.g., "primegov")
            name: Filter by exact name match
            status: Filter by status (default: "active")
            limit: Maximum number of results
            include_zipcodes: If True, batch fetch zipcodes for all jurisdictions.
                             Default False for performance in search contexts.

        Returns:
            List of Jurisdiction objects matching filters (zipcodes empty unless include_zipcodes=True)
        """
        conditions = ["status = $1"]
        params: List[Any] = [status]
        param_counter = 2

        if state:
            conditions.append(f"state = ${param_counter}")
            params.append(state)
            param_counter += 1

        if vendor:
            conditions.append(f"vendor = ${param_counter}")
            params.append(vendor)
            param_counter += 1

        if name:
            conditions.append(f"LOWER(name) = ${param_counter}")
            params.append(name.lower())
            param_counter += 1

        where_clause = " AND ".join(conditions)
        limit_clause = f"LIMIT ${param_counter}" if limit else ""
        if limit:
            params.append(limit)

        query = f"""
            SELECT banana, name, state, vendor, slug, extra_vendors, type, county_banana, status, participation
            FROM jurisdictions
            WHERE {where_clause}
            ORDER BY state, name
            {limit_clause}
        """

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

            zipcodes_map: Dict[str, List[str]] = {}
            if include_zipcodes and rows:
                bananas = [row["banana"] for row in rows]
                zipcodes_rows = await conn.fetch(
                    "SELECT banana, zipcode FROM zipcodes WHERE banana = ANY($1)",
                    bananas,
                )
                for zrow in zipcodes_rows:
                    zipcodes_map.setdefault(zrow["banana"], []).append(str(zrow["zipcode"]))

            return [_build_jurisdiction(row, zipcodes_map.get(row["banana"], [])) for row in rows]

    async def get_city_names(self, status: str = "active") -> List[str]:
        """Get just jurisdiction names for fuzzy matching (no N+1)

        Lightweight query that returns only names, avoiding the full Jurisdiction
        object construction and zipcode queries. Used for fuzzy search.

        Args:
            status: Jurisdiction status filter (default: "active")

        Returns:
            List of jurisdiction names
        """
        rows = await self._fetch(
            "SELECT DISTINCT name FROM jurisdictions WHERE status = $1 ORDER BY name",
            status,
        )
        return [row["name"] for row in rows]

    async def get_county_jurisdictions(self, county_banana: str) -> List[str]:
        """Get all bananas linked to a county (the county itself + all cities with county_banana FK).

        Args:
            county_banana: The county's banana identifier

        Returns:
            List of bananas: [county_banana, city1, city2, ...]
        """
        return (
            await self.get_county_jurisdictions_batch([county_banana])
        ).get(county_banana, [])

    async def get_county_jurisdictions_batch(
        self, county_bananas: Sequence[str]
    ) -> Dict[str, List[str]]:
        """Expand multiple counties in one query, preserving member order."""
        unique_counties = list(dict.fromkeys(county_bananas))
        result: Dict[str, List[str]] = {
            county_banana: [] for county_banana in unique_counties
        }
        if not unique_counties:
            return result

        rows = await self._fetch(
            """
            SELECT requested.county_banana, jurisdiction.banana
            FROM unnest($1::text[]) WITH ORDINALITY
                AS requested(county_banana, input_order)
            JOIN jurisdictions AS jurisdiction
              ON jurisdiction.banana = requested.county_banana
              OR jurisdiction.county_banana = requested.county_banana
            ORDER BY requested.input_order, jurisdiction.type DESC,
                     jurisdiction.name
            """,
            unique_counties,
        )
        for row in rows:
            result[row["county_banana"]].append(row["banana"])
        return result

    async def get_city_meeting_frequency(self, banana: str, days: int = 30) -> int:
        """Get count of meetings for a jurisdiction in the last N days

        Args:
            banana: Jurisdiction banana identifier
            days: Number of days to look back (default: 30)

        Returns:
            Count of meetings in the last N days
        """
        row = await self._fetchrow(
            """
            SELECT COUNT(*) as count
            FROM meetings
            WHERE banana = $1
            AND date >= NOW() - INTERVAL '1 day' * $2
            """,
            banana,
            days
        )
        return row["count"] if row else 0

    async def get_city_last_sync(self, banana: str) -> Optional[datetime]:
        """Get the most recent successful vendor-sync checkpoint.

        Used by fetcher to determine if jurisdiction needs syncing.

        Args:
            banana: Jurisdiction banana

        Returns:
            Datetime of the last completed sync, or None if never successful.
        """
        row = await self._fetchrow(
            """
            SELECT last_synced_at
            FROM jurisdictions
            WHERE banana = $1
            """,
            banana,
        )

        return row["last_synced_at"] if row else None

    async def mark_city_synced(self, banana: str) -> bool:
        """Durably checkpoint an aggregate-completed jurisdiction sync.

        Uses the database clock and never participates in meeting persistence;
        callers invoke it only after the fetcher's aggregate policy succeeds.
        """
        result = await self._execute(
            """
            UPDATE jurisdictions
            SET last_synced_at = CURRENT_TIMESTAMP
            WHERE banana = $1
            """,
            banana,
        )
        return self._parse_row_count(result) == 1

    async def get_city_sync_stats(
        self,
        bananas: Sequence[str],
        *,
        days: int = 30,
    ) -> Dict[str, JurisdictionSyncStats]:
        """Load adaptive-scheduling inputs for many jurisdictions at once.

        Meeting frequency remains event-date based; scheduling freshness comes
        exclusively from the jurisdiction's successful-sync checkpoint.
        """
        unique_bananas = list(dict.fromkeys(bananas))
        if not unique_bananas:
            return {}

        rows = await self._fetch(
            """
            SELECT
                requested.banana,
                COUNT(m.id) FILTER (
                    WHERE m.date >= NOW() - INTERVAL '1 day' * $2
                ) AS recent_meetings,
                jurisdiction.last_synced_at
            FROM unnest($1::text[]) AS requested(banana)
            LEFT JOIN jurisdictions AS jurisdiction
              ON jurisdiction.banana = requested.banana
            LEFT JOIN meetings m ON m.banana = requested.banana
            GROUP BY requested.banana, jurisdiction.last_synced_at
            """,
            unique_bananas,
            days,
        )
        return {
            row["banana"]: JurisdictionSyncStats(
                recent_meetings=int(row["recent_meetings"] or 0),
                last_synced_at=row["last_synced_at"],
            )
            for row in rows
        }
