-- Track the most recent time a stable source identity was actually fetched.
-- A source URL can serve revised bytes over time, so first_seen alone cannot
-- drive a bounded revalidation schedule.

ALTER TABLE document_source
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMP;

UPDATE document_source
SET last_seen = first_seen
WHERE last_seen IS NULL;

ALTER TABLE document_source
    ALTER COLUMN last_seen SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE document_source
    ALTER COLUMN last_seen SET NOT NULL;

DROP INDEX IF EXISTS idx_document_source_identity;
CREATE INDEX IF NOT EXISTS idx_document_source_identity
    ON document_source (source_identity, last_seen DESC);
