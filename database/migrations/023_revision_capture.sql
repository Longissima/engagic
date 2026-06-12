-- Amendment capture: the daily re-sync sees every agenda change and, until
-- now, silently overwrote it. The store_meeting upsert blind-overwrites
-- title/date/agenda_url/packet_url/status; the items upsert applies changes
-- to unfrozen rows. These triggers record what actually changed, turning
-- "the agenda was amended" from invisible into queryable — months before
-- any amendment-diffing feature consumes it. Zero pipeline code: the
-- signal is computed where the overwrite happens.
--
-- Frozen items (summary IS NOT NULL) are untouched by design: the upsert's
-- freeze CASEs keep old values, so NEW = OLD and no revision row is
-- written. Post-summary drift is invisible at row level — that invariant
-- is the temporal-snapshot contract, not a gap here.

CREATE TABLE IF NOT EXISTS meeting_revisions (
    id BIGSERIAL PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changes JSONB NOT NULL  -- {field: {old, new}}, watched fields only
);
CREATE INDEX IF NOT EXISTS idx_meeting_revisions_meeting
    ON meeting_revisions(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_revisions_changed
    ON meeting_revisions(changed_at);

CREATE OR REPLACE FUNCTION record_meeting_revision() RETURNS trigger AS $$
DECLARE
    diff JSONB := '{}'::jsonb;
BEGIN
    IF NEW.title IS DISTINCT FROM OLD.title THEN
        diff := diff || jsonb_build_object('title',
            jsonb_build_object('old', OLD.title, 'new', NEW.title));
    END IF;
    IF NEW.date IS DISTINCT FROM OLD.date THEN
        diff := diff || jsonb_build_object('date',
            jsonb_build_object('old', OLD.date, 'new', NEW.date));
    END IF;
    IF NEW.agenda_url IS DISTINCT FROM OLD.agenda_url THEN
        diff := diff || jsonb_build_object('agenda_url',
            jsonb_build_object('old', OLD.agenda_url, 'new', NEW.agenda_url));
    END IF;
    IF NEW.packet_url IS DISTINCT FROM OLD.packet_url THEN
        diff := diff || jsonb_build_object('packet_url',
            jsonb_build_object('old', OLD.packet_url, 'new', NEW.packet_url));
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        diff := diff || jsonb_build_object('status',
            jsonb_build_object('old', OLD.status, 'new', NEW.status));
    END IF;
    IF diff <> '{}'::jsonb THEN
        INSERT INTO meeting_revisions (meeting_id, changes) VALUES (NEW.id, diff);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_meeting_revisions ON meetings;
CREATE TRIGGER trg_meeting_revisions
    AFTER UPDATE OF title, date, agenda_url, packet_url, status ON meetings
    FOR EACH ROW EXECUTE FUNCTION record_meeting_revision();

CREATE TABLE IF NOT EXISTS item_revisions (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL,  -- denormalized for meeting-history queries
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changes JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_revisions_meeting
    ON item_revisions(meeting_id);

CREATE OR REPLACE FUNCTION record_item_revision() RETURNS trigger AS $$
DECLARE
    diff JSONB := '{}'::jsonb;
BEGIN
    IF NEW.title IS DISTINCT FROM OLD.title THEN
        diff := diff || jsonb_build_object('title',
            jsonb_build_object('old', OLD.title, 'new', NEW.title));
    END IF;
    IF NEW.agenda_number IS DISTINCT FROM OLD.agenda_number THEN
        diff := diff || jsonb_build_object('agenda_number',
            jsonb_build_object('old', OLD.agenda_number, 'new', NEW.agenda_number));
    END IF;
    IF NEW.attachment_hash IS DISTINCT FROM OLD.attachment_hash THEN
        diff := diff || jsonb_build_object('attachment_hash',
            jsonb_build_object('old', OLD.attachment_hash, 'new', NEW.attachment_hash));
    END IF;
    -- body_text can be 15KB+; lengths are the signal, not the payload
    IF NEW.body_text IS DISTINCT FROM OLD.body_text THEN
        diff := diff || jsonb_build_object('body_text',
            jsonb_build_object('old_len', COALESCE(length(OLD.body_text), 0),
                               'new_len', COALESCE(length(NEW.body_text), 0)));
    END IF;
    IF diff <> '{}'::jsonb THEN
        INSERT INTO item_revisions (item_id, meeting_id, changes)
        VALUES (NEW.id, NEW.meeting_id, diff);
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_item_revisions ON items;
CREATE TRIGGER trg_item_revisions
    AFTER UPDATE OF title, agenda_number, attachment_hash, body_text ON items
    FOR EACH ROW EXECUTE FUNCTION record_item_revision();

-- Late additions: items that appeared on an agenda inside the notice window
-- (Brown Act: 72h regular, 24h special) after the meeting was already known.
-- The 6h slack excludes multi-pass initial syncs; "the whole agenda posted
-- late" is a different signal than "an item slipped onto a known agenda."
-- hours_before_meeting lets consumers pick their own window.
CREATE OR REPLACE VIEW late_additions AS
SELECT
    i.id AS item_id,
    i.meeting_id,
    m.banana,
    m.title AS meeting_title,
    m.date AS meeting_date,
    i.created_at AS item_created_at,
    i.title,
    ROUND((EXTRACT(EPOCH FROM (m.date - i.created_at)) / 3600.0)::numeric, 1)
        AS hours_before_meeting
FROM items i
JOIN meetings m ON m.id = i.meeting_id
WHERE m.date IS NOT NULL
  AND i.created_at > m.created_at + INTERVAL '6 hours'
  AND i.created_at > m.date - INTERVAL '72 hours'
  AND i.created_at < m.date;
