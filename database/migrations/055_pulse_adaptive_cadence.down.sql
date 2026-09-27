DROP INDEX IF EXISTS idx_jurisdiction_pulse_next_probe;
ALTER TABLE jurisdiction_pulse DROP COLUMN IF EXISTS next_probe_at, DROP COLUMN IF EXISTS quiet_streak;
