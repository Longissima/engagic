-- Versioned filter decisions, append-only ingest audits, and explicit corpus
-- extraction outcomes. All additions are forward-compatible and preserve data.

ALTER TABLE items
    ADD COLUMN IF NOT EXISTS filter_rule_id TEXT,
    ADD COLUMN IF NOT EXISTS filter_version TEXT,
    ADD COLUMN IF NOT EXISTS filter_evaluated_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS item_filter_audits (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,
    old_reason TEXT,
    new_reason TEXT,
    rule_id TEXT,
    filter_version TEXT NOT NULL,
    source TEXT NOT NULL,
    evaluated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_item_filter_audits_item
    ON item_filter_audits (item_id, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_item_filter_audits_version
    ON item_filter_audits (filter_version, evaluated_at DESC);

CREATE TABLE IF NOT EXISTS meeting_ingest_audits (
    id BIGSERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    banana TEXT NOT NULL,
    vendor TEXT,
    slug TEXT,
    source_path TEXT NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    audit JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_meeting_ingest_audits_meeting
    ON meeting_ingest_audits (meeting_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_ingest_audits_path
    ON meeting_ingest_audits (source_path, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_ingest_audits_vendor
    ON meeting_ingest_audits (vendor, observed_at DESC);

ALTER TABLE document_blob
    ADD COLUMN IF NOT EXISTS extraction_status TEXT,
    ADD COLUMN IF NOT EXISTS extraction_attempted_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS extraction_error_type TEXT,
    ADD COLUMN IF NOT EXISTS extraction_error_message TEXT;

ALTER TABLE document_blob
    DROP CONSTRAINT IF EXISTS document_blob_extraction_status_check;
ALTER TABLE document_blob
    ADD CONSTRAINT document_blob_extraction_status_check CHECK (
        extraction_status IS NULL OR extraction_status IN (
            'succeeded', 'partial', 'failed'
        )
    );

UPDATE document_blob
SET extraction_status = CASE
        WHEN extract_method LIKE '%-partial' THEN 'partial'
        ELSE 'succeeded'
    END,
    extraction_attempted_at = COALESCE(text_extracted_at, created_at)
WHERE text_key IS NOT NULL AND extraction_status IS NULL;

CREATE INDEX IF NOT EXISTS idx_document_blob_extraction_status
    ON document_blob (extraction_status, extraction_attempted_at DESC);
