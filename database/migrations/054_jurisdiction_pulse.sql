-- Change-signal watcher ("pulse"): one cheap probe per jurisdiction replaces
-- re-syncing on a clock. A probe compares the vendor's cheapest change signal
-- (Legistar delta query, agenda RSS, ETag, or a digest of a small endpoint)
-- against the stored state and marks the jurisdiction dirty when it moves.
-- dirty_since is durable intent: a crash between detecting a change and
-- finishing the targeted sync re-drives the sync on restart instead of
-- losing the signal (the new digest is already stored).
--
-- Signals are per jurisdiction, never assumed per vendor: a city whose feed
-- is disabled or empty is marked usable = FALSE with the reason and falls
-- back to the scheduled sweep.

CREATE TABLE IF NOT EXISTS jurisdiction_pulse (
    banana TEXT PRIMARY KEY REFERENCES jurisdictions(banana) ON DELETE CASCADE ON UPDATE CASCADE,
    signal TEXT NOT NULL,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    usable BOOLEAN NOT NULL DEFAULT TRUE,
    checked_at TIMESTAMP,
    changed_at TIMESTAMP,
    dirty_since TIMESTAMP,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_jurisdiction_pulse_dirty
    ON jurisdiction_pulse(dirty_since) WHERE dirty_since IS NOT NULL;

COMMENT ON COLUMN jurisdiction_pulse.state IS
    'Signal-specific memory: {"watermark": iso} for legistar_delta, {url: {digest, etag, last_modified}} for feed/digest signals.';
COMMENT ON COLUMN jurisdiction_pulse.dirty_since IS
    'Set when a probe observes a change; cleared only after the targeted sync completes.';
