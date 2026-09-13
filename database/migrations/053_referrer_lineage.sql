ALTER TABLE matter_appearances ADD COLUMN IF NOT EXISTS referrer_receipt JSONB;
ALTER TABLE matter_appearances ADD COLUMN IF NOT EXISTS referrer_parse_run_id TEXT
    REFERENCES minutes_parse_runs(id);
COMMENT ON COLUMN matter_appearances.referrer_receipt IS
    'Minutes document/text hashes and Unicode source span supporting reported_referrer.';
