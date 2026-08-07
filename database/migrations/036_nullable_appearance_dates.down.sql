-- Restoring NOT NULL is safe only while no authoritative undated appearances
-- exist. Fail closed instead of fabricating dates or silently deleting rows.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM matter_appearances
        WHERE appeared_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'cannot restore matter_appearances.appeared_at NOT NULL while undated appearances exist';
    END IF;

    ALTER TABLE matter_appearances
        ALTER COLUMN appeared_at SET NOT NULL;
END
$$;

COMMENT ON COLUMN matter_appearances.appeared_at IS NULL;
