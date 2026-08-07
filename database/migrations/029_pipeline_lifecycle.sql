-- Durable lifecycle and observability for sync/processing work.
--
-- Queue rows represent the latest desired work. Attempts and stage events are
-- append-only history, so deduplication/re-enqueue can no longer erase the
-- evidence needed to explain throughput, retries, or partial failures.

ALTER TABLE queue
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS last_enqueued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS retry_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS work_version TEXT;

UPDATE queue
SET updated_at = COALESCE(updated_at, created_at),
    last_enqueued_at = COALESCE(last_enqueued_at, updated_at, created_at)
WHERE updated_at IS NULL OR last_enqueued_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON queue (status, retry_at, priority DESC, created_at)
    WHERE status = 'pending';

ALTER TABLE items
    ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMP;

ALTER TABLE meetings
    ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMP;

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id BIGSERIAL PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    command TEXT NOT NULL,
    targets JSONB,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    host TEXT,
    process_id INTEGER,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
    ON pipeline_runs (status, heartbeat_at);

CREATE TABLE IF NOT EXISTS job_attempts (
    id BIGSERIAL PRIMARY KEY,
    queue_id BIGINT REFERENCES queue(id) ON DELETE SET NULL,
    run_id BIGINT REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    job_type TEXT NOT NULL,
    lane TEXT,
    banana TEXT,
    meeting_id TEXT,
    matter_id TEXT,
    work_version TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN (
            'running', 'succeeded', 'partial', 'retryable_failure',
            'terminal_failure', 'abandoned'
        )),
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_type TEXT,
    error_message TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (queue_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_job_attempts_run
    ON job_attempts (run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_job_attempts_status
    ON job_attempts (status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_job_attempts_entity
    ON job_attempts (job_type, banana, meeting_id, matter_id);

CREATE TABLE IF NOT EXISTS pipeline_stage_events (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT REFERENCES job_attempts(id) ON DELETE CASCADE,
    run_id BIGINT REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_type TEXT,
    error_message TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stage_attempt
    ON pipeline_stage_events (attempt_id, started_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage_run
    ON pipeline_stage_events (run_id, stage, started_at);

-- Transactional handoff for work that must be published only if the meeting
-- persistence transaction commits. Consumers claim with SKIP LOCKED and record
-- publication independently of the domain write.
CREATE TABLE IF NOT EXISTS pipeline_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'publishing', 'published', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pipeline_outbox_ready
    ON pipeline_outbox (next_attempt_at, id)
    WHERE status IN ('pending', 'failed');
