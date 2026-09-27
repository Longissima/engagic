-- Targeted sync hints. A probe that can tell WHICH meeting dates changed
-- (Legistar EventDate, CivicClerk startDateTime, Granicus feed titles)
-- accumulates them in dirty_dates, and the sync covers only those dates.
-- dirty_full marks a change with no usable hint (whole-window sync).
-- dirty_touched_at is the latest change observation; the sync clears a row
-- only if nothing new arrived after it started.

ALTER TABLE jurisdiction_pulse
    ADD COLUMN IF NOT EXISTS dirty_dates DATE[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS dirty_full BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS dirty_touched_at TIMESTAMP;

-- Rows already dirty predate hints: sync them whole.
UPDATE jurisdiction_pulse SET dirty_full = TRUE, dirty_touched_at = dirty_since
WHERE dirty_since IS NOT NULL;
