CREATE INDEX IF NOT EXISTS idx_document_source_identity
    ON document_source (
        source_identity,
        last_validated_at DESC NULLS LAST,
        first_seen DESC
    );
DROP INDEX IF EXISTS idx_document_source_identity_hash;
CREATE INDEX IF NOT EXISTS idx_document_blob_untexted
    ON document_blob (created_at)
    WHERE text_key IS NULL;
CREATE INDEX IF NOT EXISTS idx_document_blob_extraction_status
    ON document_blob (extraction_status, extraction_attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_ingest_audits_path
    ON meeting_ingest_audits (source_path, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_meeting_ingest_audits_vendor
    ON meeting_ingest_audits (vendor, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_city_matters_first_seen ON city_matters(first_seen);
