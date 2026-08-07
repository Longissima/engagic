-- Safe only when no queue worker is active. Claim state is derived.

UPDATE queue
SET status = 'pending',
    started_at = NULL,
    retry_at = NOW(),
    updated_at = NOW()
WHERE status = 'processing';

DROP INDEX IF EXISTS idx_queue_claim_heartbeat;

DROP INDEX IF EXISTS idx_queue_processing;
CREATE INDEX idx_queue_processing
    ON queue (status, priority DESC, created_at ASC);

DROP INDEX IF EXISTS idx_queue_ready;
CREATE INDEX idx_queue_ready
    ON queue (status, retry_at, priority DESC, created_at ASC)
    WHERE status = 'pending';

ALTER TABLE queue
    DROP COLUMN IF EXISTS claim_token,
    DROP COLUMN IF EXISTS claimed_at,
    DROP COLUMN IF EXISTS heartbeat_at,
    DROP COLUMN IF EXISTS ready_at,
    DROP COLUMN IF EXISTS desired_generation;
