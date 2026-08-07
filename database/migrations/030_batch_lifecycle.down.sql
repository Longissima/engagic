BEGIN;

DROP INDEX IF EXISTS idx_batch_jobs_pollable;
DROP INDEX IF EXISTS idx_batch_jobs_open_submission_key;

ALTER TABLE batch_jobs
    DROP COLUMN IF EXISTS lease_expires_at,
    DROP COLUMN IF EXISTS lease_owner,
    DROP COLUMN IF EXISTS last_error_at,
    DROP COLUMN IF EXISTS next_poll_at,
    DROP COLUMN IF EXISTS consecutive_poll_errors,
    DROP COLUMN IF EXISTS poll_error_count,
    DROP COLUMN IF EXISTS poll_attempts,
    DROP COLUMN IF EXISTS submit_attempts,
    DROP COLUMN IF EXISTS submission_key;

COMMIT;
