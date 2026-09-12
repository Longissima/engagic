-- Refuse rollback while it would discard motion evidence or collapse items.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM minutes_parse_runs) THEN
        RAISE EXCEPTION 'minutes audit history exists; export it before rollback';
    END IF;
    IF EXISTS (SELECT 1 FROM item_motions) THEN
        RAISE EXCEPTION 'item_motions contains evidence; export it before rollback';
    END IF;
    IF EXISTS (SELECT 1 FROM votes WHERE item_id IS NULL AND item_key <> '') THEN
        RAISE EXCEPTION 'detached votes retain item identity; export it before rollback';
    END IF;
END $$;
CREATE UNIQUE INDEX uq_votes_member_matter_meeting_motion
    ON votes(council_member_id, matter_id, meeting_id, motion_index);
DROP INDEX uq_votes_member_item_motion;
ALTER TABLE matter_appearances DROP COLUMN vote_source;
DROP TABLE item_motions;

DROP TRIGGER votes_maintain_member_count ON votes;
DROP FUNCTION maintain_vote_count();
DROP TRIGGER votes_preserve_item_key ON votes;
DROP FUNCTION preserve_vote_item_key();
ALTER TABLE votes DROP COLUMN item_key;

ALTER TABLE votes DROP COLUMN observation_ordinal;
ALTER TABLE votes DROP COLUMN parse_run_id;
DROP TABLE minutes_publications;
DROP TABLE minutes_observations;
DROP TABLE minutes_parse_runs;
DROP TABLE minutes_text_snapshots;
