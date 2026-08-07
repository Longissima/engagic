-- Fence queue workers with a per-claim token and separate lease timestamps.

ALTER TABLE queue
    ADD COLUMN IF NOT EXISTS claim_token UUID,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS ready_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS desired_generation BIGINT;

-- A legacy active outbox version and a different legacy queue version have no
-- trustworthy cross-table ordering. Refuse to invent one: the operator must
-- drain or explicitly reconcile these rows before retrying the migration.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM queue q
        JOIN pipeline_outbox po
          ON po.event_type = 'queue.enqueue'
         AND po.payload->>'source_url' = q.source_url
        WHERE po.status IN ('pending', 'publishing', 'failed', 'dead_letter')
          AND q.work_version IS DISTINCT FROM po.payload->>'work_version'
    ) THEN
        RAISE EXCEPTION USING
            MESSAGE = 'ambiguous legacy queue/outbox versions block migration 033',
            HINT = 'drain or reconcile mismatched active queue.enqueue intents before retrying';
    END IF;
END
$$;

-- ``last_enqueued_at`` is the age of the currently desired work. ``ready_at``
-- is a separate per-attempt clock: retry backoff must not be reported as time
-- spent waiting for an available worker.
--
-- With ambiguous active publications excluded, the installed queue row is the
-- current desired state. Allocate it after every legacy outbox generation so
-- historical failed/dead-letter intents cannot overwrite it after replay. Do
-- both backfills in one table pass to reduce rollout WAL and lock duration.
UPDATE queue
SET desired_generation = COALESCE(
        desired_generation,
        nextval('pipeline_work_generation_seq')
    ),
    ready_at = COALESCE(
        ready_at,
        retry_at,
        last_enqueued_at,
        created_at,
        CURRENT_TIMESTAMP
    )
WHERE desired_generation IS NULL OR ready_at IS NULL;

ALTER TABLE queue
    ALTER COLUMN desired_generation SET DEFAULT
        nextval('pipeline_work_generation_seq'),
    ALTER COLUMN desired_generation SET NOT NULL,
    ALTER COLUMN ready_at SET DEFAULT CURRENT_TIMESTAMP,
    ALTER COLUMN ready_at SET NOT NULL;

-- Existing workers may still finish while the additive DDL is installed.
-- Their tokenless claims remain processing and are reclaimed through the
-- ordinary stale-claim timeout after the new code starts; migration itself
-- never creates an overlapping worker.

DROP INDEX IF EXISTS idx_queue_processing;
CREATE INDEX idx_queue_processing
    ON queue (status, priority DESC, last_enqueued_at ASC);

DROP INDEX IF EXISTS idx_queue_ready;
CREATE INDEX idx_queue_ready
    ON queue (status, retry_at, priority DESC, last_enqueued_at ASC)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_queue_claim_heartbeat
    ON queue (heartbeat_at)
    WHERE status = 'processing';
