-- Separate cache observation from origin validation for stable document URLs.
--
-- One source identity may serve several content hashes over time. Reading an
-- archived object must not make an old revision look freshly fetched, so
-- consumer observations and successful HTTP validations have distinct clocks.

ALTER TABLE document_source
    ADD COLUMN IF NOT EXISTS last_observed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_validation_attempt_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS etag TEXT,
    ADD COLUMN IF NOT EXISTS last_modified TEXT;

-- Legacy ``last_seen`` advanced on corpus sightings as well as origin fetches,
-- so it is evidence of observation but not proof of validation. Leave the two
-- validation clocks NULL: each source will establish them lazily on its next
-- acquisition instead of receiving a false fresh-cache window.
UPDATE document_source
SET last_observed_at = COALESCE(last_observed_at, last_seen, first_seen);

ALTER TABLE document_source
    ALTER COLUMN last_observed_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN last_observed_at SET NOT NULL;

COMMENT ON COLUMN document_source.last_seen IS
    'Deprecated compatibility clock; advances only after successful origin validation';
COMMENT ON COLUMN document_source.last_observed_at IS
    'Most recent pipeline use of this source/content association, including corpus reads';
COMMENT ON COLUMN document_source.last_validated_at IS
    'Most recent successful origin validation (HTTP 200 or 304)';
COMMENT ON COLUMN document_source.last_validation_attempt_at IS
    'Most recent origin validation attempt, successful or failed, for retry throttling';

DROP INDEX IF EXISTS idx_document_source_identity;
CREATE INDEX idx_document_source_identity
    ON document_source (
        source_identity,
        last_validated_at DESC NULLS LAST,
        first_seen DESC
    );
