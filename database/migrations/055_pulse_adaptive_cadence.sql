-- Adaptive probe cadence. Each jurisdiction carries its own schedule:
-- quiet_streak counts consecutive unchanged probes and stretches the interval
-- (10 -> 60 min); a change resets it to 0. next_probe_at is the single
-- scheduling truth, also used to rest missing or failing signals.

ALTER TABLE jurisdiction_pulse
    ADD COLUMN IF NOT EXISTS quiet_streak INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS next_probe_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_jurisdiction_pulse_next_probe
    ON jurisdiction_pulse(next_probe_at);
