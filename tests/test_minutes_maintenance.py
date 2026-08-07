"""Regression tests for the minutes discovery and corpus-ingest jobs."""

import asyncio
from datetime import datetime
import hashlib

import pytest

import analysis.analyzer_async as analyzer_module
from analysis.analyzer_async import AsyncAnalyzer
from database.id_generation import generate_meeting_id
from exceptions import DocumentDownloadError, ExtractionError
import scripts.ingest_minutes as ingest_minutes
from scripts.ingest_minutes import select_candidates
import scripts.sweep_minutes as sweep_minutes
from vendors.adapters.base_adapter_async import AsyncBaseAdapter, FetchResult
from vendors.adapters.parsers.router import ChunkResult, DEFERRED


@pytest.fixture(autouse=True)
def _inline_thread_offloads_in_restricted_test_sandbox(monkeypatch):
    async def inline(call, *args, **kwargs):
        await asyncio.sleep(0)
        return call(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


class _DiscoveryAdapter(AsyncBaseAdapter):
    MINUTES_DISCOVERY_SUPPORTED = True

    def __init__(self):
        super().__init__("test", "test")
        self.called = False
        self.chunk_result: ChunkResult | None = None

    async def _fetch_meetings_impl(self, days_back, days_forward):
        self.called = True
        assert self._minutes_discovery_only is True
        self.chunk_result = await self._chunk_pdf_bytes(
            b"%PDF-1.4 must not be parsed", source_url="https://example.test/agenda.pdf"
        )
        return [
            {
                "vendor_id": "1",
                "title": "Council",
                "start": "2026-08-01T18:00:00",
                "minutes_url": "https://example.test/minutes.pdf",
            },
            {
                "vendor_id": "2",
                "title": "Council",
                "start": "2026-08-02T18:00:00",
            },
        ]


class _UnsupportedAdapter(AsyncBaseAdapter):
    def __init__(self):
        super().__init__("test", "test")
        self.called = False

    async def _fetch_meetings_impl(self, days_back, days_forward):
        self.called = True
        raise AssertionError("unsupported discovery must not run a full fetch")


def test_fetch_minutes_uses_discovery_mode_and_filters_results():
    adapter = _DiscoveryAdapter()

    result = asyncio.run(adapter.fetch_minutes())

    assert result.success is True
    assert adapter.called is True
    assert adapter.chunk_result is not None
    assert adapter.chunk_result.failure_reason == DEFERRED
    assert [m["vendor_id"] for m in result.meetings] == ["1"]
    assert adapter._minutes_discovery_only is False


def test_fetch_minutes_does_not_fall_back_for_unsupported_adapter():
    adapter = _UnsupportedAdapter()

    result = asyncio.run(adapter.fetch_minutes())

    assert result.success is True
    assert result.meetings == []
    assert adapter.called is False


def test_select_candidates_retries_incomplete_and_rechecks_old_sources():
    rows = [
        {"id": "new", "minutes_url": "https://example.test/new.pdf?X-Amz-Signature=one"},
        {"id": "duplicate", "minutes_url": "https://example.test/new.pdf?X-Amz-Signature=two"},
        {"id": "broken", "minutes_url": "https://example.test/broken.pdf"},
        {"id": "old", "minutes_url": "https://example.test/old.pdf"},
        {"id": "current", "minutes_url": "https://example.test/current.pdf"},
        {"id": "backoff", "minutes_url": "https://example.test/backoff.pdf"},
        {"id": "permanent", "minutes_url": "https://example.test/permanent.pdf"},
        {
            "id": "viewer",
            "minutes_url": "https://meetings.boardbook.org/Public/Minutes/123?org=1",
        },
    ]
    states = {
        "https://example.test/broken.pdf": {
            "corpus_ready": False,
            "recheck_due": False,
        },
        "https://example.test/old.pdf": {
            "corpus_ready": True,
            "recheck_due": True,
        },
        "https://example.test/current.pdf": {
            "corpus_ready": True,
            "recheck_due": False,
        },
    }
    failure_states = {
        "https://example.test/backoff.pdf": {
            "permanent": False,
            "retry_due": False,
        },
        "https://example.test/permanent.pdf": {
            "permanent": True,
            "retry_due": True,
        },
    }

    selected, counts = select_candidates(
        rows, states, limit=10, failure_states=failure_states
    )

    assert [(row["id"], reason) for row, _, reason in selected] == [
        ("new", "new"),
        ("broken", "incomplete"),
        ("old", "revision_recheck"),
    ]
    assert counts == {
        "new": 1,
        "incomplete": 1,
        "revision_recheck": 1,
        "current": 1,
        "failure_backoff": 1,
        "permanent_failure": 1,
        "unsupported_url": 1,
    }


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(404, False), (410, False), (429, True), (503, True)],
)
def test_download_http_status_classification_and_url_redaction(
    monkeypatch, status, retryable
):
    signed_url = (
        "https://example.test/minutes.pdf?X-Amz-Signature=super-secret"
        "&X-Amz-Credential=temporary"
    )

    class Response:
        headers = {}

        def __init__(self):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Session:
        calls = 0

        def get(self, url, ssl):
            assert url == signed_url
            assert ssl is True
            self.calls += 1
            return Response()

    class Limiter:
        async def wait_if_needed(self, vendor):
            return None

    analyzer = AsyncAnalyzer(enable_llm=False)

    async def get_session():
        return Session()

    analyzer._get_session = get_session
    analyzer._sleep_download_retry = lambda *args: asyncio.sleep(0)
    monkeypatch.setattr(analyzer_module, "get_rate_limiter", lambda: Limiter())

    with pytest.raises(DocumentDownloadError) as raised:
        asyncio.run(analyzer.download_pdf_async(signed_url))

    error = raised.value
    assert error.status_code == status
    assert error.is_retryable is retryable
    assert error.document_url == "https://example.test/minutes.pdf"
    assert "super-secret" not in str(error)
    expected_limit = (
        ingest_minutes.UNBOUNDED_FAILURE_ATTEMPTS if retryable else 3
    )
    assert ingest_minutes.failure_attempt_limit(error, 3) == expected_limit


def test_record_failure_redacts_signed_url_before_database_write():
    signed_url = (
        "https://example.test/minutes.pdf?sig=super-secret&se=tomorrow"
    )
    captured = {}

    class Connection:
        async def fetchrow(self, query, *args):
            captured["query"] = query
            captured["args"] = args
            return {"attempt_count": 1, "permanent": False}

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Database:
        pool = Pool()

    error = ExtractionError(
        f"could not extract {signed_url}",
        document_url=signed_url,
    )
    result = asyncio.run(
        ingest_minutes.record_failure(
            Database(),
            identity="https://example.test/minutes.pdf",
            source_url=signed_url,
            banana="exampleCA",
            stage="extract",
            error=error,
            max_failures=3,
            retry_days=7,
        )
    )

    assert result == {"attempt_count": 1, "permanent": False}
    assert captured["query"] == ingest_minutes.RECORD_FAILURE_SQL
    persisted_error = captured["args"][4]
    assert "super-secret" not in persisted_error
    assert "https://example.test/minutes.pdf" in persisted_error


def test_extraction_only_analyzer_does_not_construct_llm(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    analyzer = AsyncAnalyzer(enable_llm=False)

    assert analyzer.summarizer is None


def test_ingest_dry_run_never_constructs_analyzer(monkeypatch):
    class Connection:
        async def fetch(self, query, *args):
            if query == ingest_minutes.CANDIDATES_SQL:
                return [{
                    "id": "meeting-1",
                    "banana": "exampleCA",
                    "minutes_url": "https://example.test/minutes.pdf",
                }]
            return []

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Database:
        def __init__(self):
            self.pool = Pool()

        @classmethod
        async def create(cls):
            return cls()

        async def close(self):
            return None

    def analyzer_must_not_be_constructed(*args, **kwargs):
        raise AssertionError("dry-run constructed the analyzer")

    monkeypatch.setattr(ingest_minutes, "Database", Database)
    monkeypatch.setattr(ingest_minutes, "AsyncAnalyzer", analyzer_must_not_be_constructed)
    monkeypatch.setattr("sys.argv", ["ingest_minutes.py", "--dry-run"])

    assert asyncio.run(ingest_minutes.main()) == 0


def test_extract_pdf_surfaces_corpus_persistence_failure(monkeypatch):
    class Corpus:
        async def lookup_extraction(self, content_sha256):
            return None

        async def archive_original(self, *args, **kwargs):
            return True

        async def persist_extraction(self, content_sha256, result):
            return False

    analyzer = AsyncAnalyzer(enable_llm=False)

    async def acquire(url: str, banana=None):
        from pipeline.document_artifacts import make_artifact

        data = b"%PDF-1.4 test"
        return make_artifact(
            requested_url=url,
            source_url=url,
            data=data,
            content_sha256=hashlib.sha256(data).hexdigest(),
        )

    analyzer.acquire_document_async = acquire
    monkeypatch.setattr(analyzer_module, "get_corpus", lambda: Corpus())
    monkeypatch.setattr(
        analyzer_module,
        "_extract_pdf_in_subprocess",
        lambda *args: {
            "success": True,
            "text": "minutes text",
            "method": "pymupdf",
            "page_count": 1,
            "ocr_pages": 0,
        },
    )

    result = asyncio.run(
        analyzer.extract_pdf_async("https://example.test/minutes.pdf")
    )

    assert result["content_sha256"]
    assert result["corpus_persisted"] is False


def test_vendor_streams_includes_valid_extras_once():
    city = {
        "vendor": "onbase",
        "slug": "primary",
        "extra_vendors": [
            {"vendor": "civicplus", "slug": "commissions"},
            {"vendor": "onbase", "slug": "primary"},
            {"vendor": "broken"},
        ],
    }

    assert sweep_minutes.vendor_streams(city) == [
        ("onbase", "primary"),
        ("civicplus", "commissions"),
    ]


def test_sweep_city_uses_minutes_discovery_for_primary_and_extra(monkeypatch):
    calls = []
    id_lookups = []

    class Adapter:
        MINUTES_DISCOVERY_SUPPORTED = True

        def __init__(self, vendor, slug):
            self.vendor = vendor
            self.slug = slug
            self.banana = None

        async def fetch_minutes(self, days_back, days_forward):
            calls.append((self.vendor, self.slug, self.banana, days_back, days_forward))
            return FetchResult(meetings=[{
                "vendor_id": f"{self.vendor}-1",
                "title": "Council",
                "start": "2026-08-01T18:00:00",
                "minutes_url": f"https://example.test/{self.vendor}.pdf",
            }])

    monkeypatch.setattr(
        sweep_minutes,
        "get_async_adapter",
        lambda vendor, slug, **kwargs: Adapter(vendor, slug),
    )

    class Connection:
        async def fetchrow(self, query, meeting_id):
            assert query == sweep_minutes.MEETING_STATE_SQL
            id_lookups.append(meeting_id)
            # First stream proves a fillable ID; second deliberately proves
            # that dry-run reports parity drift instead of claiming a fill.
            return {"minutes_url": None} if len(id_lookups) == 1 else None

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class Database:
        pool = Pool()

    city = {
        "banana": "exampleCA",
        "vendor": "onbase",
        "slug": "primary",
        "extra_vendors": [{"vendor": "civicplus", "slug": "commissions"}],
    }

    counts = asyncio.run(
        sweep_minutes.sweep_city(
            Database(),
            lambda meeting: datetime.fromisoformat(meeting["start"]),
            city,
            days_back=60,
            dry_run=True,
        )
    )

    assert calls == [
        ("onbase", "primary", "exampleCA", 60, 0),
        ("civicplus", "commissions", "exampleCA", 60, 0),
    ]
    assert counts["fetched"] == 2
    assert len(id_lookups) == 2
    assert counts["would_fill"] == 1
    assert counts["id_miss"] == 1
    assert counts["filled"] == 0


def test_sweep_city_reuses_stable_undated_meeting_identity(monkeypatch):
    class Adapter:
        MINUTES_DISCOVERY_SUPPORTED = True
        banana = None

        async def fetch_minutes(self, days_back, days_forward):
            return FetchResult(
                meetings=[
                    {
                        "vendor_id": "vendor-undated-1",
                        "title": "Council",
                        "start": None,
                        "minutes_url": "https://example.test/undated.pdf",
                    }
                ]
            )

    monkeypatch.setattr(
        sweep_minutes,
        "get_async_adapter",
        lambda *_args, **_kwargs: Adapter(),
    )
    expected_id = generate_meeting_id(
        "exampleCA",
        "vendor-undated-1",
        None,
        "Council",
    )
    looked_up = []

    class Connection:
        async def fetchrow(self, query, meeting_id):
            assert query == sweep_minutes.MEETING_STATE_SQL
            looked_up.append(meeting_id)
            return {"minutes_url": None}

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    counts = asyncio.run(
        sweep_minutes.sweep_city(
            type("Database", (), {"pool": Pool()})(),
            lambda _meeting: None,
            {
                "banana": "exampleCA",
                "vendor": "onbase",
                "slug": "primary",
            },
            days_back=60,
            dry_run=True,
        )
    )

    assert looked_up == [expected_id]
    assert counts["would_fill"] == 1
    assert counts["id_miss"] == 0
