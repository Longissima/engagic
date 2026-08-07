"""Regression tests for safe Census county boundary refreshes."""

import asyncio
from types import SimpleNamespace

import scripts.import_census_boundaries as census


def test_county_pipeline_stops_before_match_when_import_fails(monkeypatch):
    calls = []

    async def download():
        calls.append("download")
        return True

    async def import_counties():
        calls.append("import")
        return False

    async def match():
        calls.append("match")

    monkeypatch.setattr(census, "download_county_shapefile", download)
    monkeypatch.setattr(census, "import_counties_to_staging", import_counties)
    monkeypatch.setattr(census, "match_counties", match)

    assert asyncio.run(census.run_county_pipeline()) is False
    assert calls == ["download", "import"]


def test_failed_ogr_import_never_drops_current_county_table(monkeypatch, tmp_path):
    shapefile = tmp_path / census.CARTO_COUNTY_FILENAME
    shapefile.write_bytes(b"not-a-real-zip")
    statements = []

    class Connection:
        async def execute(self, query, *args):
            statements.append(" ".join(query.split()))

        async def close(self):
            return None

    async def connect(dsn):
        return Connection()

    monkeypatch.setattr(census, "DATA_DIR", tmp_path)
    monkeypatch.setattr(census.asyncpg, "connect", connect)
    monkeypatch.setattr(
        census.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="bad zip"),
    )

    assert asyncio.run(census.import_counties_to_staging()) is False
    assert statements == [
        "DROP TABLE IF EXISTS census_counties_import CASCADE"
    ]
    assert "DROP TABLE IF EXISTS census_counties" not in statements


def test_invalid_county_import_never_promotes_over_current_table(
    monkeypatch, tmp_path
):
    shapefile = tmp_path / census.CARTO_COUNTY_FILENAME
    shapefile.write_bytes(b"placeholder")
    events = []

    class Transaction:
        async def __aenter__(self):
            events.append("transaction_begin")

        async def __aexit__(self, exc_type, exc, traceback):
            events.append("transaction_rollback" if exc else "transaction_commit")
            return False

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            events.append(" ".join(query.split()))

        async def fetch(self, query, *args):
            events.append("validate_columns")
            return [
                {"column_name": name}
                for name in ("name", "namelsad", "stusps", "wkb_geometry")
            ]

        async def fetchrow(self, query, *args):
            events.append("validate_geometry")
            return {
                "total": 12,
                "null_geometry": 0,
                "invalid_geometry": 0,
                "wrong_srid": 0,
            }

        async def close(self):
            return None

    async def connect(dsn):
        return Connection()

    monkeypatch.setattr(census, "DATA_DIR", tmp_path)
    monkeypatch.setattr(census.asyncpg, "connect", connect)
    monkeypatch.setattr(
        census.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    assert asyncio.run(census.import_counties_to_staging()) is False
    assert "transaction_rollback" in events
    assert "DROP TABLE IF EXISTS census_counties" not in events


def test_valid_county_import_promotes_only_after_validation(monkeypatch, tmp_path):
    shapefile = tmp_path / census.CARTO_COUNTY_FILENAME
    shapefile.write_bytes(b"placeholder")
    events = []

    class Transaction:
        async def __aenter__(self):
            events.append("transaction_begin")

        async def __aexit__(self, exc_type, exc, traceback):
            events.append("transaction_rollback" if exc else "transaction_commit")
            return False

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            events.append(" ".join(query.split()))

        async def fetch(self, query, *args):
            events.append("validate_columns")
            return [
                {"column_name": name}
                for name in ("name", "namelsad", "stusps", "wkb_geometry")
            ]

        async def fetchrow(self, query, *args):
            events.append("validate_geometry")
            return {
                "total": 3_235,
                "null_geometry": 0,
                "invalid_geometry": 0,
                "wrong_srid": 0,
            }

        async def close(self):
            return None

    async def connect(dsn):
        return Connection()

    monkeypatch.setattr(census, "DATA_DIR", tmp_path)
    monkeypatch.setattr(census.asyncpg, "connect", connect)
    monkeypatch.setattr(
        census.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    assert asyncio.run(census.import_counties_to_staging()) is True
    drop_current = events.index("DROP TABLE IF EXISTS census_counties")
    rename_import = events.index(
        "ALTER TABLE census_counties_import RENAME TO census_counties"
    )
    assert events.index("validate_columns") < drop_current
    assert events.index("validate_geometry") < drop_current
    assert drop_current < rename_import
    assert events[-1] == "transaction_commit"
