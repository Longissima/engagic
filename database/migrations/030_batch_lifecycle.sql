-- Durable, concurrent Batch API lifecycle state.
--
-- submission_key gives each logical chunk a pre-provider intent.  The partial
-- unique index prevents two workers from creating the same open chunk while
-- allowing a terminal partial/failed chunk to be retried later.
-- Poll due-times, error counters, and leases make collector backoff observable
-- and keep daemon/foreground collectors from ingesting the same row at once.

BEGIN;

ALTER TABLE batch_jobs
    ADD COLUMN IF NOT EXISTS submission_key TEXT,
    ADD COLUMN IF NOT EXISTS submit_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS poll_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS poll_error_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS consecutive_poll_errors INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_poll_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS lease_owner TEXT,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMP;

CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_jobs_open_submission_key
    ON batch_jobs (submission_key)
    WHERE status = 'submitted';

CREATE INDEX IF NOT EXISTS idx_batch_jobs_pollable
    ON batch_jobs (next_poll_at, created_at)
    WHERE status = 'submitted';

COMMIT;
