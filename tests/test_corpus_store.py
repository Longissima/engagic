"""CorpusStore unit tests: archive/persist/lookup against in-memory fakes.

The store's contract under test:
- content addressing (same bytes converge, uploads skipped when archived)
- lookup misses on unknown hash, missing text, stale extract_version
- oversized originals are indexed but not uploaded
- every failure is swallowed: a broken R2 must never raise into the caller
"""

import asyncio
import io

import pytest

from corpus.r2 import R2Error
from corpus.store import CorpusStore, sha256_hex


class FakeBlobRepo:
    def __init__(self):
        self.blobs = {}
        self.sources = []

    async def get_blob(self, sha):
        return dict(self.blobs[sha]) if sha in self.blobs else None

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

    async def record_source(self, sha, source_identity, banana=None):
        self.sources.append((sha, source_identity, banana))


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
