"""Concurrent minutes discovery must await a complete listing."""
import asyncio
from types import SimpleNamespace

import pytest

from vendors.adapters.municode_adapter_async import AsyncMunicodeAdapter


@pytest.mark.asyncio
async def test_concurrent_minutes_discovery_shares_completed_listing():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def get(url):
        calls.append(url)
        entered.set()
        await release.wait()
        async def text():
            return '<td class="views-field-field-minutes"><a href="/meeting/2984/minutes.pdf">Minutes</a></td>'
        return SimpleNamespace(text=text)

    adapter = object.__new__(AsyncMunicodeAdapter)
    adapter._drupal_minutes_cache = None
    adapter._drupal_minutes_lock = asyncio.Lock()
    adapter._is_publish_page = adapter._is_drupal = False
    adapter.base_url = 'https://example.invalid'
    adapter.slug = 'test'
    adapter._get = get

    async def lookup():
        return (await adapter._drupal_minutes_by_meeting_id()).get('2984')

    first = asyncio.create_task(lookup())
    await entered.wait()
    second = asyncio.create_task(lookup())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert results == ['https://example.invalid/meeting/2984/minutes.pdf'] * 2
    assert await lookup() == results[0]
    assert calls == ['https://example.invalid/']
