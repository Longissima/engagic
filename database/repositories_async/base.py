"""Base repository with async PostgreSQL connection pooling

All repositories inherit from BaseRepository and share:
- Connection pool (no connection per-instance)
- Transaction context managers
- Query execution helpers with proper error handling
- Logging infrastructure

Return Type Conventions
-----------------------
All repository methods follow these patterns for consistency:

    get_X(id) -> Optional[T]
        Single entity lookup by primary key.
        Returns None if entity not found.

    get_Xs(...) -> List[T]
        Multiple entity retrieval with filters.
        Returns empty list [] if none match.

    get_X_batch(ids) -> Dict[str, T]
        Batch lookup by multiple IDs.
        Returns dict mapping found IDs to entities.
        Missing IDs are absent from dict (not errors).
        Use .get(id) to handle missing keys gracefully.

Connection Patterns
-------------------
    self.pool.acquire()
        Use for read-only queries that don't need atomicity.

    self.transaction()
        Use for writes or multi-statement reads needing consistency.
"""

import asyncpg
from asyncpg import Connection
from typing import Any, AsyncIterator, List, Optional, cast
from contextlib import asynccontextmanager

from config import get_logger

logger = get_logger(__name__).bind(component="repository")


class BaseRepository:
    """Base class for async PostgreSQL repositories

    Provides connection pool management and query execution helpers.
    All subclasses share the same connection pool for efficiency.

    Design Principles:
    - Pool is passed in, not created (singleton pattern)
    - Transactions are explicit (async with self.transaction())
    - Queries use $1, $2 placeholders (PostgreSQL parameterization)
    - All methods are async (no sync fallbacks)
    """

    def __init__(self, pool: asyncpg.Pool):
        """Initialize repository with shared connection pool

        Args:
            pool: asyncpg connection pool (shared across all repositories)
        """
        self.pool = pool

    async def _fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        """Execute query and fetch single row"""
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def _fetch(self, query: str, *args: Any) -> List[asyncpg.Record]:
        """Execute query and fetch all rows"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def _execute(self, query: str, *args: Any) -> str:
        """Execute query without returning rows (INSERT, UPDATE, DELETE)"""
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def _executemany(self, query: str, args: List[tuple]) -> None:
        """Execute query multiple times with different parameters

        More efficient than looping _execute() for bulk operations.

        Args:
            query: SQL query with $1, $2, etc. placeholders
            args: List of parameter tuples
        """
        async with self.pool.acquire() as conn:
            await conn.executemany(query, args)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Connection]:
        """Context manager for explicit transactions

        Usage:
            async with self.transaction() as conn:
                await conn.execute("INSERT ...")
                await conn.execute("UPDATE ...")
                # Auto-commits on successful exit
                # Auto-rolls back on exception

        Yields:
            Connection with active transaction
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # asyncpg acquires a transparent PoolConnectionProxy. Its
                # public operation surface is Connection-compatible, and the
                # cast keeps repository UoW call sites precisely typed.
                yield cast(Connection, conn)

    @asynccontextmanager
    async def _ensure_conn(
        self, conn: Optional[Connection] = None
    ) -> AsyncIterator[Connection]:
        """Use provided connection or create new transaction.

        Allows methods to participate in caller's transaction when conn is passed.
        """
        if conn:
            yield conn
        else:
            async with self.transaction() as c:
                yield c

    @staticmethod
    def _parse_row_count(result: str) -> int:
        """Extract row count from PostgreSQL result like 'UPDATE 5' or 'DELETE 3'."""
        if not result:
            return 0
        return int(result.split()[-1])
