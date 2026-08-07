"""
Database Migration Runner

Simple versioned SQL migrations for PostgreSQL.
No ORM, no dependencies beyond asyncpg.

Usage:
    python -m database.migrate              # Apply pending migrations
    python -m database.migrate --status     # Show migration status
    python -m database.migrate --rollback 1 # Rollback last migration (if down file exists)

Pattern:
    - Migrations are numbered SQL files: 001_name.sql, 002_name.sql
    - Each migration runs in a transaction (all-or-nothing)
    - Applied migrations tracked in schema_migrations table
    - Optional rollback via 001_name.down.sql files

Confidence: 8/10 - Standard pattern, asyncpg-native
"""

import argparse
import asyncio
from pathlib import Path
from typing import Any, Protocol

import asyncpg

from config import config, get_logger

logger = get_logger(__name__).bind(component="migrations")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationReadConnection(Protocol):
    async def fetch(self, query: str, *args: Any) -> Any: ...


class MigrationFailedError(RuntimeError):
    """A pending migration failed and the deployment must stop."""


class PendingMigrationsError(RuntimeError):
    """Runtime startup was attempted against an out-of-date schema."""


async def get_connection() -> asyncpg.Connection:
    """Get a database connection."""
    return await asyncpg.connect(config.get_postgres_dsn())


async def ensure_migrations_table(conn: asyncpg.Connection) -> None:
    """Create schema_migrations table if not exists."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


async def get_applied_migrations(conn: MigrationReadConnection) -> set[str]:
    """Get set of applied migration versions."""
    rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    return {row["version"] for row in rows}


def get_pending_migrations(applied: set[str]) -> list[tuple[str, str, Path]]:
    """
    Get list of pending migrations as (version, name, path) tuples.

    Returns migrations sorted by version number.
    """
    pending = []

    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        # Skip rollback files
        if sql_file.name.endswith(".down.sql"):
            continue

        # Parse version from filename: 001_name.sql -> 001
        parts = sql_file.stem.split("_", 1)
        if len(parts) != 2:
            logger.warning("skipping malformed migration", file=sql_file.name)
            continue

        version, name = parts

        if version not in applied:
            pending.append((version, name, sql_file))

    return pending


async def assert_schema_current(conn: Any) -> None:
    """Fail fast when runtime code is newer than the database schema.

    This check is intentionally read-only. Deployments must run the migration
    command explicitly before starting application processes; a worker should
    never discover a missing relation after it has already claimed work.
    """
    try:
        applied = await get_applied_migrations(conn)
    except asyncpg.UndefinedTableError as exc:
        raise PendingMigrationsError(
            "schema_migrations is missing; initialize and migrate the database "
            "before starting the application"
        ) from exc

    pending = get_pending_migrations(applied)
    if not pending:
        return

    names = ", ".join(f"{version}_{name}" for version, name, _ in pending)
    raise PendingMigrationsError(
        f"database schema is not current; apply pending migrations: {names}"
    )


async def apply_migration(conn: asyncpg.Connection, version: str, name: str, sql_file: Path) -> bool:
    """
    Apply a single migration in a transaction.

    Returns True if successful, False if failed.
    """
    sql = sql_file.read_text()

    logger.info("applying migration", version=version, name=name)

    try:
        async with conn.transaction():
            # Execute migration SQL
            await conn.execute(sql)

            # Record migration
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                version, name
            )

        logger.info("migration applied", version=version, name=name)
        return True

    except Exception as e:
        logger.error("migration failed", version=version, name=name, error=str(e))
        return False


async def rollback_migration(conn: asyncpg.Connection, version: str, name: str) -> bool:
    """
    Rollback a migration if down file exists.

    Returns True if successful, False if failed or no down file.
    """
    down_file = MIGRATIONS_DIR / f"{version}_{name}.down.sql"

    if not down_file.exists():
        logger.error("no rollback file", version=version, file=down_file.name)
        return False

    sql = down_file.read_text()

    logger.info("rolling back migration", version=version, name=name)

    try:
        async with conn.transaction():
            # Execute rollback SQL
            await conn.execute(sql)

            # Remove migration record
            await conn.execute(
                "DELETE FROM schema_migrations WHERE version = $1",
                version
            )

        logger.info("migration rolled back", version=version, name=name)
        return True

    except Exception as e:
        logger.error("rollback failed", version=version, name=name, error=str(e))
        return False


async def migrate() -> int:
    """
    Apply all pending migrations.

    Returns number of migrations applied.
    """
    conn = await get_connection()

    try:
        await ensure_migrations_table(conn)
        applied = await get_applied_migrations(conn)
        pending = get_pending_migrations(applied)

        if not pending:
            logger.info("no pending migrations")
            return 0

        logger.info("pending migrations", count=len(pending))

        applied_count = 0
        for version, name, sql_file in pending:
            if await apply_migration(conn, version, name, sql_file):
                applied_count += 1
            else:
                logger.error("stopping due to failed migration")
                raise MigrationFailedError(
                    f"migration {version}_{name} failed; schema is not current"
                )

        return applied_count

    finally:
        await conn.close()


async def status() -> None:
    """Print migration status."""
    conn = await get_connection()

    try:
        await ensure_migrations_table(conn)
        applied = await get_applied_migrations(conn)
        pending = get_pending_migrations(applied)

        # Get applied migration details
        rows = await conn.fetch(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        )

        print("\n=== Applied Migrations ===")
        if rows:
            for row in rows:
                print(f"  [{row['version']}] {row['name']} - {row['applied_at']}")
        else:
            print("  (none)")

        print("\n=== Pending Migrations ===")
        if pending:
            for version, name, _ in pending:
                print(f"  [{version}] {name}")
        else:
            print("  (none)")

        print()

    finally:
        await conn.close()


async def rollback(count: int = 1) -> int:
    """
    Rollback the last N migrations.

    Returns number of migrations rolled back.
    """
    conn = await get_connection()

    try:
        await ensure_migrations_table(conn)

        # Get applied migrations in reverse order
        rows = await conn.fetch(
            "SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT $1",
            count
        )

        if not rows:
            logger.info("no migrations to rollback")
            return 0

        rolled_back = 0
        for row in rows:
            if await rollback_migration(conn, row["version"], row["name"]):
                rolled_back += 1
            else:
                logger.error("stopping due to failed rollback")
                break

        return rolled_back

    finally:
        await conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect, apply, or roll back versioned database migrations."
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status", action="store_true", help="show applied and pending migrations"
    )
    action.add_argument(
        "--rollback",
        nargs="?",
        const=1,
        type=int,
        metavar="COUNT",
        help="roll back COUNT migrations (default: 1)",
    )
    return parser


def main():
    """CLI entry point. Parsing completes before any database connection."""
    args = _build_parser().parse_args()

    if args.status:
        asyncio.run(status())
    elif args.rollback is not None:
        rolled_back = asyncio.run(rollback(args.rollback))
        print(f"Rolled back {rolled_back} migration(s)")
    else:
        applied = asyncio.run(migrate())
        print(f"Applied {applied} migration(s)")


if __name__ == "__main__":
    main()
