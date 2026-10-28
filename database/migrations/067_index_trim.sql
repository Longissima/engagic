-- Index trim: the VPS disk was full enough that queries failed spilling to temp
-- (2026-10-25/26). About 2.4G of index served nothing a btree is needed for.
--
-- Applied to the live DB on 2026-10-28 via CREATE/DROP INDEX CONCURRENTLY;
-- IF [NOT] EXISTS makes this migration a no-op there. Plain statements here
-- because migrations run inside a transaction.

-- Every lookup by URL identity is equality (= $1 or = ANY), and an identity
-- has about one row (118,181 identities in a 118,190-row sample), so the
-- ORDER BY last_validated_at sorts one row. The btree carried the ~95-byte
-- URL plus two timestamps for 11.8M rows (2.1G); a hash index stores a 4-byte
-- hash. Prefix LIKE on source_identity (one archived backfill) could not use
-- the btree either: the column is not C-collated.
CREATE INDEX IF NOT EXISTS idx_document_source_identity_hash
    ON document_source USING hash (source_identity);
DROP INDEX IF EXISTS idx_document_source_identity;

-- No query reads these. idx_scan was 0 or 1 since at least 2026-10-08 and no
-- code in engagic, motioncount, spygov, or engagic-ops filters or orders on
-- them; the only readers are scripts/audit_chunker.py full-table GROUP BYs.
DROP INDEX IF EXISTS idx_document_blob_untexted;           -- 219M
DROP INDEX IF EXISTS idx_document_blob_extraction_status;  -- 127M
DROP INDEX IF EXISTS idx_meeting_ingest_audits_path;       -- 119M
DROP INDEX IF EXISTS idx_meeting_ingest_audits_vendor;     -- 111M
DROP INDEX IF EXISTS idx_city_matters_first_seen;          -- 25M
