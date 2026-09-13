-- Minutes own the public projection whenever a confirmed motion exists.
-- API evidence remains stored, and becomes visible again on retraction.
CREATE OR REPLACE FUNCTION vote_is_preferred(
    evidence_source text, evidence_meeting text, evidence_matter text, evidence_item text
) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT evidence_source = 'minutes' OR NOT EXISTS (
        SELECT 1 FROM item_motions im
        WHERE im.source = 'minutes'
          AND im.meeting_id = evidence_meeting AND im.matter_id = evidence_matter
          AND (COALESCE(evidence_item, '') = '' OR im.item_id = evidence_item)
    );
$$;
CREATE INDEX IF NOT EXISTS idx_minutes_motion_preference
    ON item_motions(meeting_id, matter_id, item_id) WHERE source = 'minutes';

CREATE OR REPLACE FUNCTION recount_member_votes(member_ids text[])
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    PERFORM id FROM council_members WHERE id = ANY(member_ids) ORDER BY id FOR UPDATE;
    UPDATE council_members cm SET vote_count = (
        SELECT count(*) FROM votes v WHERE v.council_member_id = cm.id
        AND vote_is_preferred(v.source, v.meeting_id, v.matter_id, v.item_key)
    ), updated_at = CURRENT_TIMESTAMP WHERE cm.id = ANY(member_ids);
END;
$$;

-- Statement transition tables cover inserts, corrections, deletes, cascades,
-- and source changes. Motion changes also affect API-only members.
CREATE OR REPLACE FUNCTION maintain_preferred_vote_counts()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    affected text[] := ARRAY[]::text[];
    additional text[] := ARRAY[]::text[];
BEGIN
    IF TG_TABLE_NAME = 'votes' THEN
        IF TG_OP <> 'DELETE' THEN
            SELECT array_agg(DISTINCT council_member_id) INTO affected FROM new_count_rows;
        END IF;
        IF TG_OP <> 'INSERT' THEN
            SELECT array_agg(DISTINCT council_member_id) INTO additional FROM old_count_rows;
        END IF;
    ELSE
        IF TG_OP <> 'DELETE' THEN
            SELECT array_agg(DISTINCT v.council_member_id) INTO affected
            FROM votes v JOIN new_count_rows n
              ON n.meeting_id = v.meeting_id AND n.matter_id = v.matter_id;
        END IF;
        IF TG_OP <> 'INSERT' THEN
            SELECT array_agg(DISTINCT v.council_member_id) INTO additional
            FROM votes v JOIN old_count_rows o
              ON o.meeting_id = v.meeting_id AND o.matter_id = v.matter_id;
        END IF;
    END IF;
    PERFORM recount_member_votes(COALESCE(affected, ARRAY[]::text[]) || COALESCE(additional, ARRAY[]::text[]));
    IF TG_TABLE_NAME = 'votes' AND TG_OP <> 'DELETE' THEN
        UPDATE council_members cm SET last_seen = GREATEST(cm.last_seen, n.last_vote)
        FROM (SELECT council_member_id, max(vote_date) AS last_vote
              FROM new_count_rows GROUP BY council_member_id) n
        WHERE cm.id = n.council_member_id;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS votes_maintain_member_count ON votes;
DROP FUNCTION IF EXISTS maintain_vote_count();
CREATE TRIGGER votes_count_insert AFTER INSERT ON votes
    REFERENCING NEW TABLE AS new_count_rows FOR EACH STATEMENT
    EXECUTE FUNCTION maintain_preferred_vote_counts();
CREATE TRIGGER votes_count_update AFTER UPDATE ON votes
    REFERENCING OLD TABLE AS old_count_rows NEW TABLE AS new_count_rows FOR EACH STATEMENT
    EXECUTE FUNCTION maintain_preferred_vote_counts();
CREATE TRIGGER votes_count_delete AFTER DELETE ON votes
    REFERENCING OLD TABLE AS old_count_rows FOR EACH STATEMENT
    EXECUTE FUNCTION maintain_preferred_vote_counts();
CREATE TRIGGER motions_count_insert AFTER INSERT ON item_motions
    REFERENCING NEW TABLE AS new_count_rows FOR EACH STATEMENT
    EXECUTE FUNCTION maintain_preferred_vote_counts();
CREATE TRIGGER motions_count_update AFTER UPDATE ON item_motions
    REFERENCING OLD TABLE AS old_count_rows NEW TABLE AS new_count_rows FOR EACH STATEMENT
    EXECUTE FUNCTION maintain_preferred_vote_counts();
CREATE TRIGGER motions_count_delete AFTER DELETE ON item_motions
    REFERENCING OLD TABLE AS old_count_rows FOR EACH STATEMENT
    EXECUTE FUNCTION maintain_preferred_vote_counts();
SELECT recount_member_votes(array_agg(id)) FROM council_members;
