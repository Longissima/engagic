-- last_synced_at is derived lifecycle state, not municipal domain data.
ALTER TABLE jurisdictions
    DROP COLUMN IF EXISTS last_synced_at;
