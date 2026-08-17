DROP INDEX IF EXISTS idx_document_blob_extraction_status;
ALTER TABLE document_blob
    DROP CONSTRAINT IF EXISTS document_blob_extraction_status_check,
    DROP COLUMN IF EXISTS extraction_error_message,
    DROP COLUMN IF EXISTS extraction_error_type,
    DROP COLUMN IF EXISTS extraction_attempted_at,
    DROP COLUMN IF EXISTS extraction_status;

DROP TABLE IF EXISTS meeting_ingest_audits;
DROP TABLE IF EXISTS item_filter_audits;

ALTER TABLE items
    DROP COLUMN IF EXISTS filter_evaluated_at,
    DROP COLUMN IF EXISTS filter_version,
    DROP COLUMN IF EXISTS filter_rule_id;
