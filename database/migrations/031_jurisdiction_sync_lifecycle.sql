-- Durable adaptive-sync checkpoint.
--
-- Meeting event dates are domain data, not evidence that a vendor sync ran.
-- NULL deliberately means "never successfully synced" so every jurisdiction
-- establishes a truthful checkpoint on its next successful run.

ALTER TABLE jurisdictions
    ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMP;

COMMENT ON COLUMN jurisdictions.last_synced_at IS
    'Database timestamp of the most recent jurisdiction sync with aggregate status completed; NULL means never successfully synced.';
