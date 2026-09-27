from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from database.models import Jurisdiction
from database.repositories_async.jurisdictions import JurisdictionRepository
from scripts import _jurisdiction_onboarding as onboarding


def answers(monkeypatch, values):
    values = iter(values)
    monkeypatch.setattr('builtins.input', lambda _: next(values))


def fake_db():
    conn = SimpleNamespace(execute=AsyncMock(), executemany=AsyncMock(), fetch=AsyncMock(return_value=[]))
    events = []

    @asynccontextmanager
    async def transaction():
        events.append('begin')
        try:
            yield conn
        except Exception:
            events.append('rollback')
            raise
        else:
            events.append('commit')

    @asynccontextmanager
    async def acquire():
        yield conn

    conn.transaction = transaction
    pool = SimpleNamespace(acquire=acquire)
    repo = JurisdictionRepository(pool)
    repo.get_city = AsyncMock(return_value=None)
    return SimpleNamespace(pool=pool, jurisdictions=repo), conn, events


@pytest.mark.asyncio
async def test_custom_type_uses_shared_insert_without_city_enrichment(monkeypatch):
    db, conn, events = fake_db()
    lookup = Mock(side_effect=AssertionError('Regional bodies must not use city ZIP lookup'))
    monkeypatch.setattr(onboarding, 'lookup_zipcodes', lookup)
    answers(monkeypatch, ['Regional Air Quality Board', 'Example Air Board', 'CA', 'EAB',
                         'example', 'legistar', '', '', ''])
    assert await onboarding.add_jurisdiction(db)
    args = conn.execute.call_args.args
    assert args[1:6] == ('eabCA', 'Example Air Board', 'CA', 'legistar', 'example')
    assert args[7] == 'regional_air_quality_board'
    assert events == ['begin', 'commit']
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_existing_database_types_are_selectable(monkeypatch):
    db, conn, _ = fake_db()
    conn.fetch.return_value = [{'type': 'mosquito_district'}]
    answers(monkeypatch, [str(len(onboarding.SUGGESTED_TYPES) + 1)])
    assert await onboarding.select_type(db) == 'mosquito_district'


@pytest.mark.asyncio
async def test_city_enrichment_and_manual_overrides(monkeypatch):
    db, conn, events = fake_db()
    monkeypatch.setattr(onboarding, 'lookup_zipcodes', Mock(return_value=['94000']))
    monkeypatch.setattr(onboarding, 'lookup_census_population', AsyncMock(return_value=100))
    monkeypatch.setattr(onboarding, 'lookup_census_geometry', AsyncMock(return_value=b'geometry'))
    answers(monkeypatch, ['city', 'Example', 'CA', '', 'example', 'legistar', '',
                         '94001, 94002', '1,234'])
    assert await onboarding.add_jurisdiction(db)
    assert conn.execute.call_args.args[-2:] == (1234, b'geometry')
    assert conn.executemany.call_args.args[1] == [('exampleCA', '94001', True), ('exampleCA', '94002', False)]
    assert events == ['begin', 'commit']


@pytest.mark.asyncio
async def test_existing_banana_is_not_overwritten(monkeypatch):
    db, conn, events = fake_db()
    db.jurisdictions.get_city.return_value = object()
    answers(monkeypatch, ['water_district', 'Example Water', 'CA', ''])
    assert not await onboarding.add_jurisdiction(db)
    conn.execute.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_zip_failure_rolls_back_jurisdiction_insert():
    db, conn, events = fake_db()
    conn.executemany.side_effect = RuntimeError('ZIP insert failed')
    jurisdiction = Jurisdiction(banana='exampleCA', name='Example', state='CA',
                                vendor='legistar', slug='example', type='city', zipcodes=['94001'])
    with pytest.raises(RuntimeError):
        await db.jurisdictions.add_city(jurisdiction)
    assert events == ['begin', 'rollback']


@pytest.mark.asyncio
async def test_county_name_is_kept_and_linking_remains_available(monkeypatch):
    db, conn, _ = fake_db()
    db.jurisdictions.get_cities = AsyncMock(return_value=[
        Jurisdiction(banana='exampleCA', name='Example', state='CA',
                     vendor='legistar', slug='example', type='city'),
    ])
    answers(monkeypatch, ['county', 'Example County', 'CA', '', 'examplecounty',
                         'legistar', '', '', 'y', '1'])
    assert await onboarding.add_jurisdiction(db)
    assert conn.execute.call_args.args[2] == 'Example County'
    assert conn.executemany.call_args.args[1] == [('examplecountyCA', 'exampleCA')]
