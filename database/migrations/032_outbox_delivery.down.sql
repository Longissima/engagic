-- Safe only when no delivery is active. Derived lease state is disposable.

UPDATE pipeline_outbox
SET status = 'failed',
    next_attempt_at = NOW()
WHERE status IN ('dead_letter', 'publishing');

ALTER TABLE pipeline_outbox
    DROP CONSTRAINT IF EXISTS pipeline_outbox_status_check;
ALTER TABLE pipeline_outbox
    ADD CONSTRAINT pipeline_outbox_status_check
    CHECK (status IN ('pending', 'publishing', 'published', 'failed'));

DROP INDEX IF EXISTS idx_pipeline_outbox_ready;
DROP INDEX IF EXISTS idx_pipeline_outbox_aggregate_order;
DROP INDEX IF EXISTS idx_pipeline_outbox_queue_source_generation;
DROP INDEX IF EXISTS idx_pipeline_outbox_lease_expiry;
CREATE INDEX idx_pipeline_outbox_ready
    ON pipeline_outbox (next_attempt_at, id)
    WHERE status IN ('pending', 'failed');

ALTER TABLE pipeline_outbox
    DROP COLUMN IF EXISTS claimed_at,
    DROP COLUMN IF EXISTS lease_owner,
    DROP COLUMN IF EXISTS lease_expires_at,
    DROP COLUMN IF EXISTS claim_token,
    DROP COLUMN IF EXISTS work_generation;

DROP SEQUENCE IF EXISTS pipeline_work_generation_seq;
