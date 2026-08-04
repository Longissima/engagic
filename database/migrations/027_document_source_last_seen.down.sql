DROP INDEX IF EXISTS idx_document_source_identity;

ALTER TABLE document_source
    DROP COLUMN IF EXISTS last_seen;

CREATE INDEX IF NOT EXISTS idx_document_source_identity
    ON document_source (source_identity, first_seen DESC);
