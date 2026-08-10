"""The body-identifier backfill only touches true aggregate orphans."""

from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace

import pytest

from scripts import backfill_body_identifiers


class _Acquire(AbstractAsyncContextManager):
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Transaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, candidates=None):
        self.candidates = candidates or []
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        if "SELECT i.id" in query:
            return self.candidates
        return [{"id": item_id} for item_id in args[0]]

    def transaction(self):
        return _Transaction()


def _db(connection):
    return SimpleNamespace(pool=SimpleNamespace(acquire=lambda: _Acquire(connection)))


@pytest.mark.asyncio
async def test_candidate_query_excludes_items_already_linked_to_a_matter():
    connection = _Connection()

    assert await backfill_body_identifiers.find_candidates(_db(connection), None) == []

    query, _ = connection.calls[0]
    assert "i.matter_file IS NULL" in query
    assert "i.matter_id IS NULL" in query


@pytest.mark.asyncio
async def test_apply_rechecks_both_identity_fields_and_counts_written_rows(monkeypatch):
    connection = _Connection()
    candidates = [
        {
            "item_id": "item-1",
            "banana": "alphaCA",
            "meeting_id": "meeting-1",
            "matter_file": "Contract 6007968",
            "matter_type": "Contract",
        }
    ]

    async def find_candidates(_db, _banana):
        return candidates

    monkeypatch.setattr(backfill_body_identifiers, "find_candidates", find_candidates)
    await backfill_body_identifiers.backfill(_db(connection), apply=True, banana=None)

    query, args = connection.calls[0]
    assert "AND item.matter_file IS NULL" in query
    assert "AND item.matter_id IS NULL" in query
    assert args == (["item-1"], ["Contract 6007968"], ["Contract"])
