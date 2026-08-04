-- Bound retries for source identities that repeatedly fail document download
-- or extraction. Failures are scoped to the extractor version so upgrading
-- the extractor automatically gives every identity a fresh chance.

CREATE TABLE IF NOT EXISTS document_ingest_failure (
    source_identity TEXT NOT NULL,
    extract_version TEXT NOT NULL,
    banana TEXT,
    failure_stage TEXT NOT NULL CHECK (failure_stage IN ('download', 'extract')),
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    permanent BOOLEAN NOT NULL DEFAULT FALSE,
    last_error TEXT,
    first_failed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_failed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retry_after TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_identity, extract_version)
);

CREATE INDEX IF NOT EXISTS idx_document_ingest_failure_retry
    ON document_ingest_failure (retry_after)
    WHERE permanent = FALSE;
