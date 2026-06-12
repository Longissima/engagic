DROP VIEW IF EXISTS late_additions;
DROP TRIGGER IF EXISTS trg_item_revisions ON items;
DROP FUNCTION IF EXISTS record_item_revision();
DROP TABLE IF EXISTS item_revisions;
DROP TRIGGER IF EXISTS trg_meeting_revisions ON meetings;
DROP FUNCTION IF EXISTS record_meeting_revision();
DROP TABLE IF EXISTS meeting_revisions;
