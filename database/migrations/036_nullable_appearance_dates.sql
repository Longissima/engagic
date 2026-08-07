-- Preserve authoritative undated meetings in their matter timelines.
-- meetings.date is intentionally nullable, and relationship persistence must
-- not reject or invent a date when the source publishes no schedule date.

ALTER TABLE matter_appearances
    ALTER COLUMN appeared_at DROP NOT NULL;

COMMENT ON COLUMN matter_appearances.appeared_at IS
    'Authoritative meeting date; NULL when the source meeting is undated';
