"""Validate corpus aggregates with connection-local temporary tables only."""
import os

import asyncpg
import pytest

from database.db_postgres import Database


@pytest.mark.asyncio
@pytest.mark.parametrize("populated", [False, True])
async def test_corpus_metrics_extraction_mix_and_storage(populated):
    dsn = os.getenv("ENGAGIC_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ENGAGIC_TEST_DATABASE_URL is required")
    conn = await asyncpg.connect(dsn)
    try:
        # Never read or write application tables, even on a populated database.
        await conn.execute("SET search_path TO pg_temp")
        for table in (
            "matter_appearances", "committees", "council_members",
            "committee_members", "sponsorships", "minutes_documents",
        ):
            await conn.execute(f"CREATE TEMP TABLE {table} (id integer)")
        await conn.execute("""
            CREATE TEMP TABLE document_blob (
                bytes bigint, original_key text, text_key text,
                extract_method text, page_count integer, ocr_page_count integer,
                ocr_pending_pages integer[]
            )
        """)
        if populated:
            await conn.execute("""
                INSERT INTO document_blob VALUES
                (100, 'a', 'a.txt', 'pymupdf', 3, 0, '{}'),
                (200, 'b', 'b.txt', 'pymupdf+ocr', 5, 2, '{}'),
                (300, 'c', 'c.txt', 'pymupdf+ocr', 4, 4, '{}'),
                (400, NULL, NULL, NULL, NULL, NULL, NULL),
                (500, 'd', 'd.txt', 'pymupdf-partial', 2, 0, '{1}'),
                (600, 'e', 'e.txt', 'python-docx', NULL, NULL, NULL),
                (700, 'f', 'f.txt', NULL, NULL, NULL, NULL)
            """)
        metrics = dict(await conn.fetchrow(Database._PLATFORM_METRICS_INFRASTRUCTURE))
        expected = {
            "corpus_documents": 7,
            "corpus_text_documents": 6,
            "corpus_native_documents": 2,
            "corpus_ocr_documents": 2,
            "corpus_pages": 14,
            "corpus_ocr_pages": 6,
            "corpus_documents_with_pages": 4,
            "corpus_archived_bytes": 2400,
        }
        for key, value in expected.items():
            assert metrics[key] == (value if populated else 0)
    finally:
        await conn.close()
