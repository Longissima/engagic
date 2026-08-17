-- Support Motioncount's incremental summary-discovery cursor.
-- ``id`` provides a stable tie-breaker when several summaries share a timestamp.

CREATE INDEX IF NOT EXISTS idx_items_summary_updated_id
    ON items(summary_updated_at, id);
