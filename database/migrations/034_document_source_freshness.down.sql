DROP INDEX IF EXISTS idx_document_source_identity;

ALTER TABLE document_source
    DROP COLUMN IF EXISTS last_modified,
    DROP COLUMN IF EXISTS etag,
    DROP COLUMN IF EXISTS last_validation_attempt_at,
    DROP COLUMN IF EXISTS last_validated_at,
    DROP COLUMN IF EXISTS last_observed_at;

CREATE INDEX IF NOT EXISTS idx_document_source_identity
    ON document_source (source_identity, last_seen DESC);

