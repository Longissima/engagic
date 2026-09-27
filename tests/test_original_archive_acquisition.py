import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pipeline.document_acquisition import DocumentSourceAcquirer


def acquirer(stream):
    corpus = SimpleNamespace(
        blobs=SimpleNamespace(get_blob_for_identity=AsyncMock(return_value=None)),
        record_sighting=AsyncMock(), record_alias=AsyncMock(),
    )
    source = DocumentSourceAcquirer(
        AsyncMock(), fetch_errors=(RuntimeError,), corpus_getter=lambda: corpus,
        metric_component="test", stream_loader=stream,
    )
    return source, corpus


@pytest.mark.asyncio
@pytest.mark.parametrize("status,calls", [(404, 1), (410, 1), (403, 1), (429, 2), (500, 2)])
@pytest.mark.parametrize("raised", [False, True])
async def test_only_missing_urls_are_cached(status, calls, raised):
    response = SimpleNamespace(status=status, release=Mock())
    stream = AsyncMock(return_value=response)
    if raised:
        error = RuntimeError(f"HTTP {status}")
        error.status_code = status
        stream.side_effect = error
    source, _ = acquirer(stream)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await source.archive("https://example.org/file.pdf?token=one")
    assert stream.await_count == calls
    if not raised:
        assert response.release.call_count == calls
    with pytest.raises(RuntimeError):
        await source.archive("https://example.org/file.pdf?token=two")
    assert stream.await_count == calls + 1


@pytest.mark.asyncio
async def test_acquisition_makes_one_attempt_per_visit():
    source, _ = acquirer(AsyncMock(side_effect=TimeoutError))
    for _ in range(2):
        with pytest.raises(TimeoutError):
            await source.archive("https://example.org/file.pdf")
    assert source._stream_loader.await_count == 2


@pytest.mark.asyncio
async def test_reuse_does_not_wait_for_transfer_slot():
    source, corpus = acquirer(AsyncMock())
    corpus.blobs.get_blob_for_identity.return_value = {
        "original_key": "originals/hash", "bytes": 42, "content_sha256": "hash",
    }
    receipt = await asyncio.wait_for(source.archive(
        "https://example.org/file.pdf", banana="exampleCA",
        document_semaphore=asyncio.Semaphore(0),
    ), timeout=1)
    assert receipt["reused"] is True
    corpus.record_sighting.assert_awaited_once_with(
        "hash", "https://example.org/file.pdf", "exampleCA")
    source._stream_loader.assert_not_awaited()


@pytest.mark.asyncio
async def test_transfer_is_bounded_and_slot_released_on_failure():
    source, _ = acquirer(AsyncMock())
    semaphore = asyncio.Semaphore(1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def download(*args):
        assert semaphore.locked()
        entered.set()
        await release.wait()
        raise TimeoutError

    source._archive_download = download
    task = asyncio.create_task(source.archive(
        "https://example.org/file.pdf", document_semaphore=semaphore))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert semaphore.locked()
    release.set()
    with pytest.raises(TimeoutError):
        await task
    assert not semaphore.locked()


def test_missing_cache_is_bounded():
    source, _ = acquirer(AsyncMock())
    for index in range(4097):
        source._remember_missing(str(index), 404)
    assert len(source._archive_missing) == 4096
    assert "0" not in source._archive_missing


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [403, 404, 410, 503, None])
async def test_only_unavailable_documents_allow_meeting_completion(monkeypatch, status):
    import json
    from pipeline.document_acquisition import ArchiveDocumentUnavailable
    from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator

    terminal = status in (403, 404, 410)
    error = ArchiveDocumentUnavailable(status) if terminal else TimeoutError()
    if status == 503:
        error = RuntimeError('HTTP 503')
    archive = AsyncMock(side_effect=error)
    corpus = SimpleNamespace(archive_original=AsyncMock(return_value=True))
    monkeypatch.setattr('corpus.store.get_corpus', lambda: corpus)
    adapter = SimpleNamespace(vendor='legistar', slug='example',
                              _document_acquirer=SimpleNamespace(archive=archive))
    sync = MeetingSyncOrchestrator(SimpleNamespace())
    receipt = await sync.archive_meeting(
        {'vendor_id': '1', 'title': 'Council', 'start': '2026-01-01',
         'agenda_url': 'https://example.org/file.pdf'},
        SimpleNamespace(banana='exampleCA'), adapter,
    )
    assert receipt['success'] is terminal
    assert receipt['unavailable'] == int(terminal)
    assert receipt['failed'] == int(not terminal)
    assert receipt['archived'] == 0
    manifest = json.loads(corpus.archive_original.call_args.kwargs['data'])
    document = next(iter(manifest['documents'].values()))
    assert document['status'] == ('unavailable' if terminal else 'failed')
    if terminal:
        assert document['http_status'] == status


@pytest.mark.asyncio
async def test_interrupted_body_is_released_and_left_for_scheduled_retry():
    import aiohttp

    class Body:
        def __init__(self, broken):
            self.broken = broken

        async def iter_chunked(self, size):
            yield b'%PDF-1.7\n'
            if self.broken:
                raise aiohttp.ClientPayloadError('truncated response')
            yield b'complete'

    responses = [SimpleNamespace(
        status=200, headers={'Content-Type': 'application/pdf'},
        content=Body(broken), url='https://example.org/file.pdf', release=Mock(),
    ) for broken in (True, False)]
    source, corpus = acquirer(AsyncMock(side_effect=responses))
    corpus.archive_original = AsyncMock(return_value=True)
    corpus.blobs.get_blob = AsyncMock(return_value={
        'original_key': 'originals/hash', 'content_type': 'application/pdf'})
    with pytest.raises(aiohttp.ClientPayloadError):
        await source.archive('https://example.org/file.pdf')
    assert source._stream_loader.await_count == 1
    corpus.archive_original.assert_not_awaited()
    receipt = await source.archive('https://example.org/file.pdf')
    assert receipt['bytes'] == len(b'%PDF-1.7\ncomplete')
    assert source._stream_loader.await_count == 2
    assert corpus.archive_original.await_count == 1
    for response in responses:
        response.release.assert_called_once()


@pytest.mark.asyncio
async def test_partial_discovery_archives_good_documents_and_rediscovery_is_required():
    import json
    import sqlite3
    from scripts.archive_meetings import archive_source_window

    state = sqlite3.connect(':memory:')
    state.execute("""CREATE TABLE windows (
        source TEXT, start TEXT, end TEXT, status TEXT, meetings TEXT,
        receipts TEXT NOT NULL DEFAULT '{}', error TEXT, discovery_error TEXT,
        PRIMARY KEY(source,start,end))""")
    identity = ('source', '2024-01-01', '2024-02-01')
    first = {'vendor_id': 'first', 'agenda_url': 'https://example.org/good.pdf'}
    second = {'vendor_id': 'second', 'agenda_url': 'https://example.org/recovered.pdf'}
    adapter = SimpleNamespace(slug='example', fetch_meetings=AsyncMock(side_effect=[
        SimpleNamespace(success=False, error='Attachment-list HTTP 503', meetings=[first]),
        # Reverse indexes to ensure old successful receipts cannot skip new work.
        SimpleNamespace(success=True, error=None, meetings=[second, first]),
    ]))
    sync = SimpleNamespace(archive_meeting=AsyncMock(return_value={
        'success': True, 'documents': 1, 'unavailable': 0, 'no_documents': False}))
    args = SimpleNamespace(refresh_discovery=False, meeting_concurrency=1,
                           max_document_mib=10, no_store_meetings=True)
    params = (state, identity, adapter, SimpleNamespace(banana='exampleCA'), sync, args, asyncio.Semaphore(2))
    try:
        with pytest.raises(RuntimeError, match='Attachment-list HTTP 503'):
            await archive_source_window(*params)
        sync.archive_meeting.assert_awaited_once()
        assert sync.archive_meeting.call_args.args[0] == first
        row = state.execute('SELECT status,receipts,discovery_error FROM windows').fetchone()
        assert row[0] != 'complete'
        assert json.loads(row[1])['0']['success'] is True
        assert row[2] == 'Attachment-list HTTP 503'
        await archive_source_window(*params)
        assert adapter.fetch_meetings.await_count == 2
        assert [call.args[0] for call in sync.archive_meeting.await_args_list] == [first, second, first]
        assert state.execute('SELECT status,discovery_error FROM windows').fetchone() == ('complete', None)
        await archive_source_window(*params)
        assert adapter.fetch_meetings.await_count == 2
        assert sync.archive_meeting.await_count == 3
    finally:
        state.close()


def checkpoint_state():
    import sqlite3
    from scripts.archive_meetings import initialize_checkpoint
    state = sqlite3.connect(':memory:')
    initialize_checkpoint(state)
    return state


def test_new_work_excludes_all_attempted_windows_and_prioritizes_unseen_sources():
    import json
    from datetime import date
    from scripts.archive_meetings import source_jobs
    state = checkpoint_state()
    old = ('aaaCA', 'legistar', 'aaa')
    new = ('zzzCA', 'legistar', 'zzz')
    sources = {old: object(), new: object()}
    for left, right, status in [('2024-01-01', '2024-02-01', 'failed'),
                                ('2024-02-01', '2024-03-01', 'complete'),
                                ('2024-03-01', '2024-04-01', 'discovered')]:
        state.execute('INSERT INTO windows(source,start,end,status) VALUES(?,?,?,?)',
                      (json.dumps(old), left, right, status))
    start, end = date(2024, 1, 1), date(2024, 5, 1)
    jobs = source_jobs(state, sources, start, end, 'new', 100)
    assert [job[0] for job in jobs] == [new, old]
    assert list(jobs[1][2]) == [(date(2024, 4, 1), end)]
    resumed = source_jobs(state, sources, start, end, 'resume', 100)
    assert list(resumed[0][2]) == [(date(2024, 3, 1), date(2024, 4, 1))]
    state.execute("UPDATE windows SET attempts=3 WHERE status='failed'")
    assert source_jobs(state, sources, start, end, 'retry', 100) == []
    state.execute("UPDATE windows SET attempts=1,next_retry_at=200 WHERE status='failed'")
    assert source_jobs(state, sources, start, end, 'retry', 100) == []
    assert len(source_jobs(state, sources, start, end, 'retry', 201)) == 1
    state.close()


@pytest.mark.asyncio
async def test_sources_rotate_and_failures_do_not_block_other_sources(monkeypatch):
    from datetime import date
    import scripts.archive_meetings as runner
    state = checkpoint_state()
    sources = {(name, 'legistar', name): SimpleNamespace(banana=name) for name in ('a', 'b', 'c')}
    seen = []

    async def archive(state, identity, adapter, city, sync, args, semaphore):
        seen.append((city.banana, identity[1]))
        if city.banana == 'a':
            raise TimeoutError('unreachable source')
        state.execute("UPDATE windows SET status='complete' WHERE source=? AND start=? AND end=?", identity)
        state.commit()

    monkeypatch.setattr(runner, 'archive_source_window', archive)
    monkeypatch.setattr(runner, 'get_async_adapter', lambda *a, **k: SimpleNamespace())
    args = SimpleNamespace(start=date(2024, 1, 1), end=date(2024, 3, 1),
                           max_windows=0, processed_windows=0, source_concurrency=1)
    await runner.execute_phase(state, sources, None, args, asyncio.Semaphore(1), 'new')
    assert seen == [(name, month) for month in ('2024-01-01', '2024-02-01') for name in ('a', 'b', 'c')]
    assert state.execute("SELECT count(*) FROM windows WHERE status='complete'").fetchone()[0] == 4
    before = list(seen)
    await runner.execute_phase(state, sources, None, args, asyncio.Semaphore(1), 'new')
    await runner.execute_phase(state, sources, None, args, asyncio.Semaphore(1), 'retry')
    assert seen == before  # Restart does not immediately redo deferred work.
    state.close()


def test_document_outcomes_survive_restart_and_retries_are_bounded(tmp_path, monkeypatch):
    import sqlite3
    import scripts.archive_meetings as runner
    path = tmp_path / 'checkpoint.sqlite3'
    now = [1000.0]
    monkeypatch.setattr(runner.time, 'time', lambda: now[0])
    state = sqlite3.connect(path)
    runner.initialize_checkpoint(state)
    checkpoint = runner.ArchiveDocumentCheckpoint(state)
    for url, status in [('good', 'archived'), ('gone', 'unavailable'), ('slow', 'failed')]:
        checkpoint.record('cityCA', f'https://example.org/{url}', {'status': status, 'url': url})
    state.close()
    state = sqlite3.connect(path)
    checkpoint = runner.ArchiveDocumentCheckpoint(state)
    assert checkpoint.get('cityCA', 'https://example.org/good')['reused'] is True
    assert checkpoint.get('cityCA', 'https://example.org/gone')['status'] == 'unavailable'
    checkpoint.retry_failed = True
    assert checkpoint.get('cityCA', 'https://example.org/slow')['status'] == 'failed'
    now[0] += runner.RETRY_DELAY_SECONDS + 1
    checkpoint.retry_failed = False
    assert checkpoint.get('cityCA', 'https://example.org/slow')['status'] == 'failed'
    checkpoint.retry_failed = True
    assert checkpoint.get('cityCA', 'https://example.org/slow') is None
    for _ in range(2):
        checkpoint.record('cityCA', 'https://example.org/slow', {'status': 'failed'})
        now[0] += runner.RETRY_DELAY_SECONDS + 1
        checkpoint = runner.ArchiveDocumentCheckpoint(state)
        checkpoint.retry_failed = True
    assert checkpoint.get('cityCA', 'https://example.org/slow')['status'] == 'failed'
    assert checkpoint.get('anotherCA', 'https://example.org/good') is None  # Preserve provenance.
    assert checkpoint.get('cityCA', 'https://example.org/gone?new-signature=1') is None
    state.close()


@pytest.mark.asyncio
async def test_document_saved_before_manifest_failure_is_not_reacquired(monkeypatch):
    from scripts.archive_meetings import ArchiveDocumentCheckpoint
    from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
    state = checkpoint_state()
    checkpoint = ArchiveDocumentCheckpoint(state)
    corpus = SimpleNamespace(archive_original=AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr('corpus.store.get_corpus', lambda: corpus)
    acquire = AsyncMock(return_value={'content_sha256': 'hash', 'original_key': 'key',
                                    'bytes': 10, 'reused': False})
    adapter = SimpleNamespace(vendor='legistar', slug='example',
                              _document_acquirer=SimpleNamespace(archive=acquire))
    sync = MeetingSyncOrchestrator(SimpleNamespace())
    meeting = {'vendor_id': '1', 'start': '2024-01-01', 'agenda_url': 'https://example.org/good'}
    city = SimpleNamespace(banana='cityCA')
    with pytest.raises(RuntimeError, match='manifest'):
        await sync.archive_meeting(meeting, city, adapter, document_checkpoint=checkpoint)
    checkpoint = ArchiveDocumentCheckpoint(state)
    result = await sync.archive_meeting(meeting, city, adapter, document_checkpoint=checkpoint)
    assert result['success'] is True
    assert result['reused'] == 1
    assert acquire.await_count == 1
    state.close()


@pytest.mark.asyncio
async def test_archive_transport_disables_nested_retries(monkeypatch):
    from vendors.adapters.legistar_adapter_async import AsyncLegistarAdapter
    adapter = AsyncLegistarAdapter('example')
    get = AsyncMock()
    monkeypatch.setattr(adapter, '_get', get)
    await adapter._get_archive_document('https://example.org/file.pdf')
    get.assert_awaited_once_with('https://example.org/file.pdf', _max_attempts=1)


@pytest.mark.asyncio
async def test_recovery_only_attempts_failed_documents_after_cooldown(monkeypatch):
    import collections
    from pipeline.document_acquisition import ArchiveDocumentUnavailable
    from pipeline.orchestrators.meeting_sync import MeetingSyncOrchestrator
    import scripts.archive_meetings as runner
    state = checkpoint_state()
    now = [1000.0]
    monkeypatch.setattr(runner.time, 'time', lambda: now[0])
    calls = collections.Counter()

    async def archive(url, **kwargs):
        calls[url] += 1
        if url.endswith('/gone'):
            raise ArchiveDocumentUnavailable(404)
        if url.endswith('/slow') and calls[url] == 1:
            raise TimeoutError
        return {'content_sha256': url, 'original_key': url, 'bytes': 10, 'reused': False}

    corpus = SimpleNamespace(archive_original=AsyncMock(return_value=True))
    monkeypatch.setattr('corpus.store.get_corpus', lambda: corpus)
    adapter = SimpleNamespace(vendor='legistar', slug='example',
                              _document_acquirer=SimpleNamespace(archive=archive))
    meeting = {'vendor_id': '1', 'start': '2024-01-01', 'archive_documents': [
        {'url': f'https://example.org/{name}'} for name in ('good', 'gone', 'slow')]}
    city = SimpleNamespace(banana='cityCA')
    checkpoint = runner.ArchiveDocumentCheckpoint(state)
    result = await MeetingSyncOrchestrator(None).archive_meeting(
        meeting, city, adapter, document_checkpoint=checkpoint)
    assert result['failed'] == 1 and result['unavailable'] == 1 and result['archived'] == 1
    # The same failed URL in another meeting, then a process restart, does not retry.
    for checkpoint in (checkpoint, runner.ArchiveDocumentCheckpoint(state)):
        await MeetingSyncOrchestrator(None).archive_meeting(
            {**meeting, 'vendor_id': '2'}, city, adapter, document_checkpoint=checkpoint)
    assert list(calls.values()) == [1, 1, 1]
    checkpoint.retry_failed = True
    await MeetingSyncOrchestrator(None).archive_meeting(
        meeting, city, adapter, document_checkpoint=checkpoint)
    assert calls['https://example.org/slow'] == 1  # Cooldown still applies.
    now[0] += runner.RETRY_DELAY_SECONDS + 1
    result = await MeetingSyncOrchestrator(None).archive_meeting(
        meeting, city, adapter, document_checkpoint=checkpoint)
    assert result['success'] is True
    assert result['archived'] == 2 and result['unavailable'] == 1
    assert calls == {'https://example.org/good': 1, 'https://example.org/gone': 1,
                     'https://example.org/slow': 2}
    state.close()


def test_legacy_checkpoint_migration_preserves_existing_work():
    import sqlite3
    from scripts.archive_meetings import initialize_checkpoint
    state = sqlite3.connect(':memory:')
    state.execute("""CREATE TABLE windows (
        source TEXT, start TEXT, end TEXT, status TEXT, meetings TEXT,
        receipts TEXT NOT NULL DEFAULT '{}', error TEXT,
        PRIMARY KEY(source,start,end))""")
    state.execute("INSERT INTO windows VALUES('source','2024-01-01','2024-02-01','failed','[]','{}','timeout')")
    initialize_checkpoint(state)
    initialize_checkpoint(state)
    assert state.execute('SELECT status,meetings,receipts,error,attempts FROM windows').fetchone() == (
        'failed', '[]', '{}', 'timeout', 1)
    state.close()
