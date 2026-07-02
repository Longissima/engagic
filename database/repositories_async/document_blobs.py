"""Async DocumentBlobRepository: pointer + provenance index for the corpus.

The corpus itself (original bytes, extracted text) lives in R2, content-
addressed by sha256(source bytes). These rows record what exists, how the
text was produced (extract_method/extract_version), and where the bytes
have been seen (document_source). Blobs never sit inline in hot rows.

Everything here is upsert-shaped: two workers acquiring the same bytes
concurrently must both succeed, converging on one row.
"""

from typing import Any, Dict, Optional

from database.repositories_async.base import BaseRepository
from config import get_logger

logger = get_logger(__name__).bind(component="document_blob_repository")


class DocumentBlobRepository(BaseRepository):
    """Repository for document_blob / document_source corpus index rows."""

    async def get_blob(self, content_sha256: str) -> Optional[Dict[str, Any]]:
        row = await self._fetchrow(
            "SELECT * FROM document_blob WHERE content_sha256 = $1",
            content_sha256,
        )
        return dict(row) if row else None

    async def upsert_blob(
        self,
        content_sha256: str,
        byte_count: int,
        content_type: Optional[str] = None,
    ) -> None:
        """Ensure a blob row exists. Idempotent; never regresses provenance."""
        await self._execute(
            """
            INSERT INTO document_blob (content_sha256, bytes, content_type)
            VALUES ($1, $2, $3)
            ON CONFLICT (content_sha256) DO NOTHING
            """,
            content_sha256,
            byte_count,
            content_type,
        )

    async def mark_original_archived(self, content_sha256: str, original_key: str) -> None:
        await self._execute(
            """
            UPDATE document_blob
            SET original_key = $2
            WHERE content_sha256 = $1 AND original_key IS NULL
            """,
            content_sha256,
            original_key,
        )

    async def set_extraction(
        self,
        content_sha256: str,
        text_key: str,
        extract_method: Optional[str],
        extract_version: str,
        page_count: Optional[int],
        ocr_page_count: Optional[int],
        text_chars: int,
    ) -> None:
        """Record extracted text and its provenance for a blob."""
        await self._execute(
            """
            UPDATE document_blob
            SET text_key = $2,
                extract_method = $3,
                extract_version = $4,
                page_count = $5,
                ocr_page_count = $6,
                text_chars = $7,
                text_extracted_at = CURRENT_TIMESTAMP
            WHERE content_sha256 = $1
            """,
            content_sha256,
            text_key,
            extract_method,
            extract_version,
            page_count,
            ocr_page_count,
            text_chars,
        )

    async def record_source(
        self,
        content_sha256: str,
        source_identity: str,
        banana: Optional[str] = None,
    ) -> None:
        """Remember that these bytes were seen at this (stable) URL identity.

        Re-sightings backfill provenance: a row first written without a
        banana (early tee sites didn't know it) gains one the next time the
        same bytes surface from a caller that does. Never overwrites."""
        await self._execute(
            """
            INSERT INTO document_source (content_sha256, source_identity, banana)
            VALUES ($1, $2, $3)
            ON CONFLICT (content_sha256, source_identity) DO UPDATE
                SET banana = COALESCE(document_source.banana, EXCLUDED.banana)
            """,
            content_sha256,
            source_identity,
            banana,
        )

    async def get_blob_for_identity(self, source_identity: str) -> Optional[Dict[str, Any]]:
        """Newest blob seen at a URL identity -- the read path for consumers
        that hold an attachment URL rather than a hash. A URL that served
        revised bytes over time resolves to the latest revision."""
        row = await self._fetchrow(
            """
            SELECT b.*
            FROM document_source s
            JOIN document_blob b USING (content_sha256)
            WHERE s.source_identity = $1
            ORDER BY s.first_seen DESC
            LIMIT 1
            """,
            source_identity,
        )
        return dict(row) if row else None

    async def stats(self) -> Dict[str, Any]:
        """Corpus-wide counters for ops visibility."""
        row = await self._fetchrow(
            """
            SELECT
                count(*) AS blobs,
                count(original_key) AS originals_archived,
                count(text_key) AS texts_extracted,
                coalesce(sum(bytes), 0) AS total_bytes,
                coalesce(sum(text_chars), 0) AS total_text_chars
            FROM document_blob
            """
        )
        return dict(row) if row else {}
