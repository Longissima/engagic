-- Separate durable provider wait from local pre-provider intent/create time.

ALTER TABLE batch_jobs
    ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP;

-- Exact provider activation time was not recorded for legacy jobs. Creation
-- is the closest lower-bound estimate; all new activations write NOW().
UPDATE batch_jobs
SET submitted_at = created_at
WHERE submitted_at IS NULL
  AND gemini_job_name NOT LIKE 'intent:%';

COMMENT ON COLUMN batch_jobs.submitted_at IS
    'Provider accepted/submitted time; provider wait starts here, not at intent reservation';
