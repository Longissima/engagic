-- Recoverable, bounded transactional-outbox delivery.

CREATE SEQUENCE IF NOT EXISTS pipeline_work_generation_seq;

ALTER TABLE pipeline_outbox
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS claim_token UUID,
    ADD COLUMN IF NOT EXISTS work_generation BIGINT;

-- Preserve existing outbox order, then place all future queue/outbox desires on
-- one total-order sequence. This is independent of transaction-start clocks.
UPDATE pipeline_outbox
SET work_generation = id
WHERE work_generation IS NULL;

SELECT setval(
    'pipeline_work_generation_seq',
    GREATEST(
        COALESCE((SELECT MAX(work_generation) FROM pipeline_outbox), 0),
        (SELECT last_value FROM pipeline_work_generation_seq),
        1
    ),
    true
);

ALTER TABLE pipeline_outbox
    ALTER COLUMN work_generation SET DEFAULT
        nextval('pipeline_work_generation_seq'),
    ALTER COLUMN work_generation SET NOT NULL;

-- A pre-lease publisher cannot prove ownership after this deployment. Make
-- the idempotent event eligible for a fresh leased attempt.
UPDATE pipeline_outbox
SET status = 'failed',
    next_attempt_at = NOW(),
    claim_token = NULL,
    last_error = COALESCE(last_error, 'reclaimed during leased-outbox migration')
WHERE status = 'publishing';

ALTER TABLE pipeline_outbox
    DROP CONSTRAINT IF EXISTS pipeline_outbox_status_check;
ALTER TABLE pipeline_outbox
    ADD CONSTRAINT pipeline_outbox_status_check
    CHECK (status IN ('pending', 'publishing', 'published', 'failed', 'dead_letter'));

DROP INDEX IF EXISTS idx_pipeline_outbox_ready;
CREATE INDEX idx_pipeline_outbox_ready
    ON pipeline_outbox (next_attempt_at, work_generation)
    WHERE status IN ('pending', 'failed', 'publishing');

CREATE INDEX IF NOT EXISTS idx_pipeline_outbox_aggregate_order
    ON pipeline_outbox (
        event_type, aggregate_type, aggregate_id, work_generation
    )
    WHERE status NOT IN ('published', 'dead_letter');

CREATE INDEX IF NOT EXISTS idx_pipeline_outbox_queue_source_generation
    ON pipeline_outbox ((payload->>'source_url'), work_generation)
    WHERE event_type = 'queue.enqueue';

CREATE INDEX IF NOT EXISTS idx_pipeline_outbox_lease_expiry
    ON pipeline_outbox (lease_expires_at, work_generation)
    WHERE status = 'publishing';
