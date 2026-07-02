-- Decouple batch submit from collect.
--
-- Before this, a meeting's Gemini Batch job was polled inline on a batch-lane
-- slot until it finished -- pinning the slot (and its document cache) for the
-- whole job lifetime, and losing the job reference entirely on restart. This
-- table makes a submitted job a durable, restartable record: the submit path
-- writes a row and releases the slot; a collector polls open rows and ingests
-- results when Gemini reports SUCCEEDED. We never cancel a running job, so the
-- only authority on "done" is Gemini's terminal state.
--
-- One row per submitted chunk (a meeting may submit several). item_ids is the
-- set of keys submitted in that chunk, used to re-associate the response JSONL
-- back to items at collect time without re-deriving the request payload.
-- meeting_meta carries the meeting-level finalization context (participation)
-- computed at submit time but only applied once the meeting's last chunk lands.

CREATE TABLE IF NOT EXISTS batch_jobs (
    id BIGSERIAL PRIMARY KEY,
    gemini_job_name TEXT NOT NULL UNIQUE,
    meeting_id TEXT NOT NULL,
    banana TEXT,
    chunk_num INTEGER NOT NULL DEFAULT 1,
    item_ids JSONB NOT NULL,
    cache_name TEXT,
    prompts_version TEXT,
    meeting_meta JSONB,
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'collected', 'failed')),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_polled_at TIMESTAMP,
    collected_at TIMESTAMP,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
    FOREIGN KEY (banana) REFERENCES jurisdictions(banana) ON DELETE CASCADE
);

-- The collector scans open jobs oldest-first; a partial index keeps that scan
-- proportional to in-flight work, not the full (mostly collected) history.
CREATE INDEX IF NOT EXISTS idx_batch_jobs_open
    ON batch_jobs (created_at)
    WHERE status = 'submitted';

-- "Does this meeting already have a batch in flight?" guards double-submit.
CREATE INDEX IF NOT EXISTS idx_batch_jobs_meeting
    ON batch_jobs (meeting_id);
