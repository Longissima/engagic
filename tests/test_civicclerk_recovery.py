import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendors.adapters.civicclerk_adapter_async import AsyncCivicClerkAdapter
from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
from database.models import AgendaItem


@pytest.mark.asyncio
async def test_request_failure_does_not_chunk_or_report_partial_success(monkeypatch):
    adapter = AsyncCivicClerkAdapter('stormlakeia')
    monkeypatch.setattr(adapter, '_fetch_all_events', AsyncMock(return_value=[
        dict(id=1063, eventName='Airport Commission', agendaId=575, hasAgenda=True,
             publishedFiles=[dict(type='Agenda', fileId=1318)])]))
    monkeypatch.setattr(adapter, '_get', AsyncMock(side_effect=asyncio.TimeoutError()))
    chunk = AsyncMock()
    monkeypatch.setattr(adapter, '_parse_packet_pdf', chunk)
    result = await adapter.fetch_meetings()
    assert not result.success
    assert 'TimeoutError' in result.error and '575' in result.error
    assert not result.meetings
    chunk.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_api_shape_is_not_an_empty_agenda(monkeypatch):
    adapter = AsyncCivicClerkAdapter('stormlakeia')
    monkeypatch.setattr(adapter, '_get', AsyncMock(return_value=SimpleNamespace(json=AsyncMock(return_value={}))))
    with pytest.raises(RuntimeError, match='no valid items list'):
        await adapter._fetch_meeting_items(575)


@pytest.mark.asyncio
async def test_native_items_are_authoritative(monkeypatch):
    adapter = AsyncCivicClerkAdapter('stormlakeia')
    monkeypatch.setattr(adapter, '_fetch_meeting_items', AsyncMock(return_value=[dict(vendor_item_id='9508',title='Airport Minutes')]))
    result = await adapter._process_event(dict(id=1063, agendaId=575, hasAgenda=True))
    assert result['authoritative_item_source'] == 'civicclerk_api'


@pytest.mark.asyncio
async def test_recovery_does_not_reuse_chunk_id_or_its_summary():
    mid = 'stormlakeIA_16159cd0'
    old = AgendaItem(id=mid+'_seq003_abc', meeting_id=mid, title='Airport Minutes', sequence=3, summary='Old chunk summary')
    orchestrator = MeetingSyncOrchestrator(SimpleNamespace(items=SimpleNamespace(get_agenda_items=AsyncMock(return_value=[old]))))
    result = await orchestrator._process_agenda_items(
        [dict(vendor_item_id='9508',title=old.title,sequence=3)],
        SimpleNamespace(id=mid,banana='stormlakeIA'), {}, prefer_native_ids=True)
    assert result[0].id == mid+'_9508'
    assert result[0].summary is None
