"""Async DocumentBlobRepository: pointer + provenance index for the corpus.

The corpus itself (original bytes, extracted text) lives in R2, content-
addressed by sha256(source bytes). These rows record what exists, how the
text was produced (extract_method/extract_version), and where the bytes
have been seen (document_source). Blobs never sit inline in hot rows.

Everything here is upsert-shaped: two workers acquiring the same bytes
concurrently must both succeed, converging on one row.
"""

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Set

from asyncpg import Connection

from database.repositories_async.base import BaseRepository
from config import get_logger
from pipeline.utils import attachment_source_identities

logger = get_logger(__name__).bind(component="document_blob_repository")

# The bounded retry ledger every ingest path shares (sync archival, the corpus
# window, minutes and publications ingestion). Keyed by extractor version so
# an extractor upgrade reconsiders identities the old one could not handle.
RECORD_FAILURE_SQL = """
    INSERT INTO document_ingest_failure AS current_failure (
        source_identity, extract_version, banana, failure_stage,
        attempt_count, permanent, last_error, retry_after
    )
    VALUES (
        $1, $2, $3, $4, 1, ($6::boolean OR 1 >= $7), $5,
        CURRENT_TIMESTAMP + make_interval(days => $8)
    )
    ON CONFLICT (source_identity, extract_version) DO UPDATE SET
        banana = COALESCE(current_failure.banana, EXCLUDED.banana),
        failure_stage = EXCLUDED.failure_stage,
        attempt_count = current_failure.attempt_count + 1,
        permanent = (
            current_failure.permanent
            OR $6::boolean
            OR current_failure.attempt_count + 1 >= $7
        ),
        last_error = EXCLUDED.last_error,
        last_failed_at = CURRENT_TIMESTAMP,
        retry_after = CURRENT_TIMESTAMP + make_interval(days => $8)
    RETURNING attempt_count, permanent
"""


# item_documents: each item's attachments joined to the corpus (migration 068).
# An identity has about one document_source row; the newest validation wins
# when a URL has served more than one version.
_ITEM_ATTACHMENTS_SQL = "SELECT id, attachments FROM items WHERE id = ANY($1::text[])"
_IDENTITY_SHAS_SQL = """
    SELECT DISTINCT ON (source_identity) source_identity, content_sha256
    FROM document_source WHERE source_identity = ANY($1::text[])
    ORDER BY source_identity, last_validated_at DESC NULLS LAST, first_seen DESC, content_sha256
"""
# Replace without touching rows that did not change, so recorded_at stays the
# moment an appearance was first known (readers page new appearances by it).
_ITEM_DOCUMENTS_DELETE_SQL = """
    DELETE FROM item_documents d
    WHERE d.item_id = ANY($1::text[])
      AND NOT EXISTS (
          SELECT 1 FROM unnest($2::bytea[], $3::text[], $4::int2[], $5::int[], $6::int[])
              AS n(sha, item_id, ordinal, page_first, page_last)
          WHERE n.sha = d.content_sha256 AND n.item_id = d.item_id AND n.ordinal = d.ordinal
            AND n.page_first IS NOT DISTINCT FROM d.page_first AND n.page_last IS NOT DISTINCT FROM d.page_last)
"""
# recorded_at is when an appearance became known: now for live writers, the
# item's own creation for a backfill of history, so readers paging "new
# appearances" see only news.
_ITEM_DOCUMENTS_INSERT_SQL = """
    INSERT INTO item_documents (content_sha256, item_id, ordinal, page_first, page_last, recorded_at)
    SELECT n.sha, n.item_id, n.ordinal, n.page_first, n.page_last,
           CASE WHEN $6::boolean THEN COALESCE(i.created_at, now()) ELSE now() END
    FROM unnest($1::bytea[], $2::text[], $3::int2[], $4::int[], $5::int[])
        AS n(sha, item_id, ordinal, page_first, page_last)
    JOIN items i ON i.id = n.item_id
    ON CONFLICT (content_sha256, item_id, ordinal) DO UPDATE
        SET page_first = EXCLUDED.page_first, page_last = EXCLUDED.page_last
        WHERE (item_documents.page_first, item_documents.page_last)
              IS DISTINCT FROM (EXCLUDED.page_first, EXCLUDED.page_last)
"""


def _pages(attachment: Dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    pages = attachment.get("pages")
    if isinstance(pages, list) and len(pages) == 2 and all(isinstance(p, int) for p in pages):
        return pages[0], pages[1]
    return None, None


async def refresh_item_documents(conn: Connection, item_ids: List[str], *, backdate: bool = False) -> int:
    """Rewrite these items' item_documents rows from their stored attachments.

    Reads the stored rows, not the caller's objects, so a frozen (summarized)
    item keeps the documents it was summarized from. `backdate` dates new rows
    by their item's creation (history backfills). Returns the resolved count.
    """
    if not item_ids:
        return 0
    items = await conn.fetch(_ITEM_ATTACHMENTS_SQL, item_ids)
    wanted: List[tuple[str, int, Dict[str, Any], List[str]]] = []
    for row in items:
        attachments = row["attachments"] if isinstance(row["attachments"], list) else []
        for ordinal, attachment in enumerate(attachments):
            if isinstance(attachment, dict):
                wanted.append((row["id"], ordinal, attachment, attachment_source_identities(attachment)))
    keys = sorted({k for *_, ids in wanted for k in ids})
    sha = {r["source_identity"]: r["content_sha256"] for r in await conn.fetch(_IDENTITY_SHAS_SQL, keys)} if keys else {}
    resolved = []
    for item_id, ordinal, attachment, identities in wanted:
        found = next((sha[k] for k in identities if k in sha), None)
        if found:
            resolved.append((bytes.fromhex(found), item_id, ordinal, *_pages(attachment)))
    cols = [list(c) for c in zip(*resolved)] if resolved else [[], [], [], [], []]
    await conn.execute(_ITEM_DOCUMENTS_DELETE_SQL, [r["id"] for r in items], *cols)
    if resolved:
        await conn.execute(_ITEM_DOCUMENTS_INSERT_SQL, *cols, backdate)
    return len(resolved)


class DocumentBlobRepository(BaseRepository):
    """Repository for document_blob / document_source corpus index rows."""

    async def get_minutes_documents(self, meeting_id: str):
        """Newest observed revision first, with corpus readiness and evidence keys."""
        from corpus.store import COMPATIBLE_EXTRACT_VERSIONS
        rows = await self._fetch("""
            SELECT md.meeting_id, md.content_sha256, md.source_identity, md.ingested_at,
                   b.original_key, b.text_key, b.extract_version, b.extract_method, b.ocr_pending_pages,
                   (COALESCE(b.extract_method LIKE '%-partial', false)
                    OR COALESCE(cardinality(b.ocr_pending_pages), 0) > 0) AS text_incomplete,
                   COALESCE(b.text_key IS NOT NULL AND b.extract_version = ANY($2::text[]), false) AS text_ready
            FROM minutes_documents md JOIN document_blob b USING (content_sha256)
            WHERE md.meeting_id = $1
            ORDER BY md.ingested_at DESC, md.content_sha256 DESC
        """, meeting_id, list(COMPATIBLE_EXTRACT_VERSIONS))
        return [dict(row) for row in rows]

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
        extraction_status: str = "succeeded",
        ocr_pending_pages: Optional[list[int]] = None,
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
                text_extracted_at = CURRENT_TIMESTAMP,
                extraction_status = $8,
                ocr_pending_pages = $9,
                extraction_attempted_at = CURRENT_TIMESTAMP,
                extraction_error_type = NULL,
                extraction_error_message = NULL
            WHERE content_sha256 = $1
            """,
            content_sha256,
            text_key,
            extract_method,
            extract_version,
            page_count,
            ocr_page_count,
            text_chars,
            extraction_status,
            ocr_pending_pages,
        )

    async def record_extraction_failure(
        self,
        content_sha256: str,
        *,
        error_type: str,
        error_message: str,
    ) -> None:
        await self._execute(
            """
            UPDATE document_blob
            SET extraction_status = 'failed',
                extraction_attempted_at = CURRENT_TIMESTAMP,
                extraction_error_type = $2,
                extraction_error_message = LEFT($3, 2000)
            WHERE content_sha256 = $1
            """,
            content_sha256,
            error_type,
            error_message,
        )

    async def record_listing(
        self, source_identity: str, title: Optional[str], published_on: Optional[date],
    ) -> int:
        """What a publications listing currently says about the document at
        this URL, applied to every revision seen there (the listing names the
        URL, not a revision). The listing is the authority on its own words,
        so a retitle replaces the old title; a listing that drops its date
        does not erase one."""
        result = await self._execute(
            """
            UPDATE document_source
            SET title = COALESCE($2, title), published_on = COALESCE($3, published_on)
            WHERE source_identity = $1
              AND (title IS DISTINCT FROM COALESCE($2, title)
                   OR published_on IS DISTINCT FROM COALESCE($3, published_on))
            """,
            source_identity,
            title,
            published_on,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def record_source_observation(
        self,
        content_sha256: str,
        source_identity: str,
        banana: Optional[str] = None,
    ) -> None:
        """Record use of archived bytes without claiming an origin fetch."""
        await self._execute(
            """
            INSERT INTO document_source (
                content_sha256, source_identity, banana, last_observed_at
            )
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (content_sha256, source_identity) DO UPDATE
                SET banana = COALESCE(document_source.banana, EXCLUDED.banana),
                    last_observed_at = CURRENT_TIMESTAMP
            """,
            content_sha256,
            source_identity,
            banana,
        )

    async def record_source_alias(
        self, content_sha256: str, source_identity: str, alias_identity: str,
        banana: Optional[str] = None,
    ) -> None:
        """Alias a known revision using its actual origin-validation clocks."""
        await self._execute("""
            INSERT INTO document_source (
                content_sha256, source_identity, banana, last_seen, last_observed_at,
                last_validated_at, last_validation_attempt_at
            )
            SELECT content_sha256, $3, COALESCE($4, banana), last_seen, CURRENT_TIMESTAMP,
                   last_validated_at, last_validation_attempt_at
            FROM document_source WHERE content_sha256=$1 AND source_identity=$2
            ON CONFLICT (content_sha256, source_identity) DO UPDATE SET
                banana = COALESCE(document_source.banana, EXCLUDED.banana),
                last_seen = GREATEST(document_source.last_seen, EXCLUDED.last_seen),
                last_observed_at = CURRENT_TIMESTAMP,
                last_validated_at = GREATEST(document_source.last_validated_at, EXCLUDED.last_validated_at),
                last_validation_attempt_at = GREATEST(document_source.last_validation_attempt_at,
                                                     EXCLUDED.last_validation_attempt_at)
        """, content_sha256, source_identity, alias_identity, banana)

    async def record_source_validation(
        self,
        content_sha256: str,
        source_identity: str,
        banana: Optional[str] = None,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """Record a successful HTTP 200/304 validation of one revision."""
        await self._execute(
            """
            INSERT INTO document_source (
                content_sha256, source_identity, banana, last_seen,
                last_observed_at, last_validated_at,
                last_validation_attempt_at, etag, last_modified
            )
            VALUES (
                $1, $2, $3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, $4, $5
            )
            ON CONFLICT (content_sha256, source_identity) DO UPDATE
                SET banana = COALESCE(document_source.banana, EXCLUDED.banana),
                    last_seen = CURRENT_TIMESTAMP,
                    last_observed_at = CURRENT_TIMESTAMP,
                    last_validated_at = CURRENT_TIMESTAMP,
                    last_validation_attempt_at = CURRENT_TIMESTAMP,
                    etag = COALESCE(EXCLUDED.etag, document_source.etag),
                    last_modified = COALESCE(
                        EXCLUDED.last_modified, document_source.last_modified
                    )
            """,
            content_sha256,
            source_identity,
            banana,
            etag,
            last_modified,
        )

    async def record_source_validation_failure(
        self,
        content_sha256: str,
        source_identity: str,
        banana: Optional[str] = None,
    ) -> None:
        """Throttle retries after a failed origin check while serving cache."""
        await self._execute(
            """
            UPDATE document_source
            SET banana = COALESCE(document_source.banana, $3),
                last_observed_at = CURRENT_TIMESTAMP,
                last_validation_attempt_at = CURRENT_TIMESTAMP
            WHERE content_sha256 = $1 AND source_identity = $2
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
            SELECT b.*, s.source_identity, s.last_observed_at,
                   s.last_validated_at, s.last_validation_attempt_at,
                   s.etag, s.last_modified
            FROM document_source s
            JOIN document_blob b USING (content_sha256)
            WHERE s.source_identity = $1
            ORDER BY s.last_validated_at DESC NULLS LAST, s.first_seen DESC
            LIMIT 1
            """,
            source_identity,
        )
        return dict(row) if row else None

    async def refresh_meeting_documents(self, meeting_ids: List[str]) -> int:
        """Re-resolve every item of these meetings, after their documents were archived."""
        if not meeting_ids:
            return 0
        async with self._ensure_conn(None) as conn:
            item_ids = [r["id"] for r in await conn.fetch(
                "SELECT id FROM items WHERE meeting_id = ANY($1::text[])", meeting_ids)]
            return await refresh_item_documents(conn, item_ids)

    async def archived_identities(self, identities: Iterable[str]) -> Set[str]:
        """The subset of URL identities whose original bytes are in R2."""
        wanted = sorted(set(identities))
        if not wanted:
            return set()
        rows = await self._fetch(
            """
            SELECT DISTINCT s.source_identity
            FROM document_source s
            JOIN document_blob b USING (content_sha256)
            WHERE s.source_identity = ANY($1::text[])
              AND b.original_key IS NOT NULL
              AND COALESCE(b.bytes, 0) > 0
            """,
            wanted,
        )
        return {row["source_identity"] for row in rows}

    async def deferred_download_identities(
        self, identities: Iterable[str], extract_version: str
    ) -> Set[str]:
        """Identities whose download failed and must not be retried yet."""
        wanted = sorted(set(identities))
        if not wanted:
            return set()
        rows = await self._fetch(
            """
            SELECT source_identity FROM document_ingest_failure
            WHERE source_identity = ANY($1::text[])
              AND extract_version = $2
              AND failure_stage = 'download'
              AND (permanent OR retry_after > CURRENT_TIMESTAMP)
            """,
            wanted,
            extract_version,
        )
        return {row["source_identity"] for row in rows}

    async def record_ingest_failure(
        self,
        *,
        source_identity: str,
        extract_version: str,
        banana: Optional[str],
        stage: str,
        error_text: str,
        permanent: bool,
        max_failures: int,
        retry_days: int,
    ) -> Optional[Dict[str, Any]]:
        row = await self._fetchrow(
            RECORD_FAILURE_SQL,
            source_identity,
            extract_version,
            banana,
            stage,
            error_text[:1000],
            permanent,
            max_failures,
            retry_days,
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
