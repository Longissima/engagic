"""CorpusStore unit tests: archive/persist/lookup against in-memory fakes.

The store's contract under test:
- content addressing (same bytes converge, uploads skipped when archived)
- lookup misses on unknown hash, missing text, stale extract_version
- oversized originals are indexed but not uploaded
- every failure is swallowed: a broken R2 must never raise into the caller
"""

import asyncio
import io
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from corpus.r2 import R2Error
from corpus.store import CorpusOriginal, CorpusStore, sha256_hex


class FakeBlobRepo:
    def __init__(self):
        self.blobs = {}
        self.sources = []
        self.source_state = {}

    async def get_blob(self, sha):
        return dict(self.blobs[sha]) if sha in self.blobs else None

    async def get_blob_for_identity(self, source_identity):
        for sha, identity, _banana in reversed(self.sources):
            if identity == source_identity:
                blob = await self.get_blob(sha)
                if blob:
                    blob.update(self.source_state[(sha, identity)])
                return blob
        return None

    async def upsert_blob(self, sha, byte_count, content_type=None):
        self.blobs.setdefault(
            sha,
            {
                "content_sha256": sha,
                "bytes": byte_count,
                "content_type": content_type,
                "original_key": None,
                "text_key": None,
                "extract_method": None,
                "extract_version": None,
                "page_count": None,
                "ocr_page_count": None,
                "text_chars": None,
            },
        )

    async def mark_original_archived(self, sha, key):
        if self.blobs[sha]["original_key"] is None:
            self.blobs[sha]["original_key"] = key

    async def set_extraction(self, sha, text_key, extract_method, extract_version,
                             page_count, ocr_page_count, text_chars):
        self.blobs[sha].update(
            text_key=text_key,
            extract_method=extract_method,
            extract_version=extract_version,
            page_count=page_count,
            ocr_page_count=ocr_page_count,
            text_chars=text_chars,
        )

    async def record_source_observation(self, sha, source_identity, banana=None):
        self.sources.append((sha, source_identity, banana))
        state = self.source_state.setdefault(
            (sha, source_identity),
            {
                "etag": None,
                "last_modified": None,
                "last_observed_at": None,
                "last_validated_at": None,
                "last_validation_attempt_at": None,
            },
        )
        state["last_observed_at"] = datetime.now()

    async def record_source_validation(
        self,
        sha,
        source_identity,
        banana=None,
        *,
        etag=None,
        last_modified=None,
    ):
        self.sources.append((sha, source_identity, banana))
        now = datetime.now()
        self.source_state[(sha, source_identity)] = {
            "etag": etag,
            "last_modified": last_modified,
            "last_observed_at": now,
            "last_validated_at": now,
            "last_validation_attempt_at": now,
        }

    async def record_source_validation_failure(
        self, sha, source_identity, banana=None
    ):
        del banana
        state = self.source_state[(sha, source_identity)]
        state["last_observed_at"] = datetime.now()
        state["last_validation_attempt_at"] = datetime.now()


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.put_calls = 0

    async def put(self, key, data, content_type="application/octet-stream",
                  payload_sha256=None, content_length=None):
        self.put_calls += 1
        if not isinstance(data, bytes):
            data = data.read()
        self.objects[key] = data

    async def get(self, key):
        return self.objects.get(key)

    async def delete(self, key):
        self.objects.pop(key, None)

    async def close(self):
        pass


class BrokenR2(FakeR2):
    async def put(self, key, data, content_type="application/octet-stream",
                  payload_sha256=None, content_length=None):
        raise R2Error("simulated outage")

    async def get(self, key):
        raise R2Error("simulated outage")


PDF_BYTES = b"%PDF-1.4 fake document body"
SHA = sha256_hex(PDF_BYTES)


def make_store(r2=None):
    repo = FakeBlobRepo()
    store = CorpusStore(repo, r2 or FakeR2())
    return store, repo


def run(coro):
    return asyncio.run(coro)


def test_archive_then_lookup_roundtrip():
    store, repo = make_store()

    archived = run(store.archive_original(
        SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES,
        source_url="https://legistar.example/View.ashx?ID=1&sig=abc",
    ))
    assert archived
    assert store.r2.objects["originals/" + SHA] == PDF_BYTES
    assert repo.blobs[SHA]["original_key"] == "originals/" + SHA
    assert repo.blobs[SHA]["content_type"] == "application/pdf"
    # signature markers stripped by attachment_identity
    assert repo.sources[0][1] == "https://legistar.example/View.ashx"

    result = {"success": True, "text": "GROUND TRUTH", "method": "pymupdf",
              "page_count": 3, "ocr_pages": 1}
    assert run(store.persist_extraction(SHA, result))

    served = run(store.lookup_extraction(SHA))
    assert served is not None
    assert served["text"] == "GROUND TRUTH"
    assert served["from_corpus"] is True
    assert served["page_count"] == 3
    assert served["ocr_pages"] == 1


def test_second_archive_skips_upload_but_records_source():
    store, repo = make_store()
    run(store.archive_original(SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES,
                               source_url="https://a.example/x.pdf"))
    run(store.archive_original(SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES,
                               source_url="https://b.example/y.pdf"))
    assert store.r2.put_calls == 1
    assert len(repo.sources) == 2


def test_original_identity_read_preserves_hash_and_media_type():
    store, _ = make_store()
    source_url = "https://a.example/x.pdf?sig=volatile"
    run(store.archive_original(
        SHA,
        byte_count=len(PDF_BYTES),
        data=PDF_BYTES,
        source_url=source_url,
    ))

    original = run(store.get_original_artifact_by_identity(source_url))

    assert original is not None
    assert original.data == PDF_BYTES
    assert original.content_sha256 == SHA
    assert original.content_type == "application/pdf"
    assert original.last_validated_at is not None
    assert run(store.get_original_by_identity(source_url)) == PDF_BYTES


def test_cache_sighting_does_not_advance_origin_validation():
    store, repo = make_store()
    source_url = "https://a.example/x.pdf"
    run(store.archive_original(
        SHA,
        byte_count=len(PDF_BYTES),
        data=PDF_BYTES,
        source_url=source_url,
        etag='"v1"',
        last_modified="Wed, 01 Jul 2026 12:00:00 GMT",
    ))
    state = repo.source_state[(SHA, source_url)]
    validated_at = state["last_validated_at"]

    run(store.record_sighting(SHA, source_url, "exampleCA"))

    assert state["last_validated_at"] == validated_at
    assert state["last_observed_at"] >= validated_at


def test_corpus_original_freshness_and_failure_backoff():
    now = datetime(2026, 8, 7, 12, 0, 0)
    fresh = CorpusOriginal(
        PDF_BYTES,
        SHA,
        "application/pdf",
        last_validated_at=now - timedelta(hours=2),
        last_validation_attempt_at=now - timedelta(hours=2),
    )
    assert not fresh.needs_revalidation(
        max_age_seconds=24 * 60 * 60,
        failure_retry_seconds=60 * 60,
        now=now,
    )

    stale_with_recent_failure = CorpusOriginal(
        PDF_BYTES,
        SHA,
        "application/pdf",
        last_validated_at=now - timedelta(days=2),
        last_validation_attempt_at=now - timedelta(minutes=10),
    )
    assert not stale_with_recent_failure.needs_revalidation(
        max_age_seconds=24 * 60 * 60,
        failure_retry_seconds=60 * 60,
        now=now,
    )
    assert stale_with_recent_failure.needs_revalidation(
        max_age_seconds=24 * 60 * 60,
        failure_retry_seconds=5 * 60,
        now=now,
    )


def test_schema_and_migration_define_distinct_source_clocks():
    root = Path(__file__).resolve().parents[1]
    schema = (root / "database/schema_postgres.sql").read_text()
    migration = (
        root / "database/migrations/034_document_source_freshness.sql"
    ).read_text()
    for field in (
        "last_observed_at",
        "last_validated_at",
        "last_validation_attempt_at",
        "etag",
        "last_modified",
    ):
        assert field in schema
        assert field in migration
    assert "last_validated_at DESC NULLS LAST" in schema
    assert "last_validated_at DESC NULLS LAST" in migration
    assert "last_observed_at = COALESCE" in migration
    assert "last_validated_at = COALESCE" not in migration


def test_file_obj_archive_streams_and_sniffs():
    store, repo = make_store()
    archived = run(store.archive_original(
        SHA, byte_count=len(PDF_BYTES), file_obj=io.BytesIO(PDF_BYTES),
        source_url="https://a.example/x.pdf",
    ))
    assert archived
    assert store.r2.objects["originals/" + SHA] == PDF_BYTES
    assert repo.blobs[SHA]["content_type"] == "application/pdf"


def test_lookup_misses():
    store, repo = make_store()
    # unknown hash
    assert run(store.lookup_extraction(SHA)) is None
    # archived but no text yet
    run(store.archive_original(SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES))
    assert run(store.lookup_extraction(SHA)) is None
    # stale extractor version is a miss (forces lazy re-extraction)
    run(store.persist_extraction(SHA, {"success": True, "text": "T", "method": "pymupdf",
                                       "page_count": 1, "ocr_pages": 0}))
    repo.blobs[SHA]["extract_version"] = "0"
    assert run(store.lookup_extraction(SHA)) is None


def test_failed_extraction_not_persisted():
    store, _ = make_store()
    run(store.archive_original(SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES))
    assert not run(store.persist_extraction(SHA, {"success": False, "error": "boom"}))
    assert not run(store.persist_extraction(SHA, {"success": True, "text": ""}))
    assert run(store.lookup_extraction(SHA)) is None


def test_oversize_indexed_not_uploaded(monkeypatch):
    from config import config
    monkeypatch.setattr(config, "CORPUS_MAX_ORIGINAL_BYTES", 10)
    store, repo = make_store()
    archived = run(store.archive_original(
        SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES,
        source_url="https://a.example/huge.pdf",
    ))
    assert archived is False
    assert store.r2.objects == {}
    assert SHA in repo.blobs  # still indexed for dedup
    assert len(repo.sources) == 1


def test_r2_outage_never_raises():
    store, _ = make_store(r2=BrokenR2())
    assert run(store.archive_original(SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES,
                                      source_url="https://a.example/x.pdf")) is False
    assert run(store.persist_extraction(
        SHA, {"success": True, "text": "T", "method": "pymupdf", "page_count": 1})) is False
    assert run(store.lookup_extraction(SHA)) is None


def test_missing_text_object_is_a_miss():
    store, repo = make_store()
    run(store.archive_original(SHA, byte_count=len(PDF_BYTES), data=PDF_BYTES))
    run(store.persist_extraction(SHA, {"success": True, "text": "T", "method": "pymupdf",
                                       "page_count": 1, "ocr_pages": 0}))
    del store.r2.objects["text/" + SHA + ".txt"]
    assert run(store.lookup_extraction(SHA)) is None


def test_sha256_hex_is_content_identity():
    assert sha256_hex(b"a") != sha256_hex(b"b")
    assert sha256_hex(PDF_BYTES) == SHA
    assert len(SHA) == 64


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
