-- Internal evidence ledger. A run is immutable once completed; a separate
-- pointer selects the current public projection. No identity resolution is
-- required to save an observation, and no ledger row is exposed by vote APIs.
CREATE TABLE minutes_text_snapshots (
    text_sha256 TEXT PRIMARY KEY,
    text_content TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE minutes_parse_runs (
    id TEXT PRIMARY KEY,
    cache_key TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    text_sha256 TEXT REFERENCES minutes_text_snapshots(text_sha256),
    extract_version TEXT,
    parser_version TEXT NOT NULL,
    parser_build TEXT NOT NULL,
    inputs JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'missing_text')),
    error JSONB,
    summary JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_minutes_completed_input ON minutes_parse_runs(cache_key) WHERE status = 'completed';
CREATE INDEX idx_minutes_runs_meeting ON minutes_parse_runs(meeting_id, created_at DESC);
CREATE TABLE minutes_observations (
    run_id TEXT NOT NULL REFERENCES minutes_parse_runs(id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    raw_text TEXT NOT NULL,
    item_id TEXT,
    evidence JSONB NOT NULL,
    interpretation JSONB NOT NULL,
    checks JSONB NOT NULL,
    publication JSONB,
    PRIMARY KEY (run_id, ordinal)
);
CREATE TABLE minutes_publications (
    meeting_id TEXT PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES minutes_parse_runs(id),
    published_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Motion records retain outcomes even when no individual voters are known.
-- Stop vote writers before migration and restart on this code: the conflict
-- target now includes item identity. NULL item_id remains the legacy API grain.
CREATE TABLE item_motions (
    item_id TEXT NOT NULL, -- historical identity; retained if the agenda item is removed
    motion_index SMALLINT NOT NULL CHECK (motion_index >= 0),
    matter_id TEXT NOT NULL REFERENCES city_matters(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    motion_text TEXT NOT NULL,
    outcome TEXT,
    tally JSONB,
    method TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'minutes' CHECK (source IN ('api', 'minutes')),
    content_sha256 TEXT,
    receipt JSONB,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tally_basis TEXT,
    parse_run_id TEXT,
    observation_ordinal INTEGER,
    FOREIGN KEY (parse_run_id, observation_ordinal) REFERENCES minutes_observations(run_id, ordinal),
    PRIMARY KEY (item_id, motion_index, source)
);
CREATE INDEX idx_item_motions_matter ON item_motions(matter_id, meeting_id);
CREATE INDEX idx_item_motions_meeting ON item_motions(meeting_id);
-- Keep the original item identity even if the nullable item FK is cleared.
ALTER TABLE votes ADD COLUMN item_key TEXT NOT NULL DEFAULT '';
UPDATE votes SET item_key = COALESCE(item_id, '');
CREATE FUNCTION preserve_vote_item_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.item_id IS NOT NULL THEN
        NEW.item_key := NEW.item_id;
    ELSIF TG_OP = 'UPDATE' THEN
        NEW.item_key := OLD.item_key;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER votes_preserve_item_key
    BEFORE INSERT OR UPDATE OF item_id ON votes
    FOR EACH ROW EXECUTE FUNCTION preserve_vote_item_key();

CREATE UNIQUE INDEX uq_votes_member_item_motion
    ON votes(council_member_id, matter_id, meeting_id, item_key, motion_index, source);
DROP INDEX uq_votes_member_matter_meeting_motion;
ALTER TABLE matter_appearances ADD COLUMN vote_source TEXT
    CHECK (vote_source IN ('api', 'minutes'));
-- The old minutes writer alone adds this method marker.
UPDATE matter_appearances SET vote_source = 'minutes'
WHERE vote_tally ->> 'method' IN ('driver', 'named', 'unanimous', 'tally', 'outcome');
-- Repair historical direct inserts; subsequent minutes reconciliation recounts
-- all affected members, including those whose last vote has been removed.
UPDATE council_members cm SET vote_count = (
    SELECT count(*) FROM votes v WHERE v.council_member_id = cm.id
) WHERE vote_count IS DISTINCT FROM (
    SELECT count(*)::int FROM votes v WHERE v.council_member_id = cm.id
);

-- Counts cover direct writes and relationship moves as well as repository calls.
CREATE FUNCTION maintain_vote_count() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.council_member_id = NEW.council_member_id THEN
        RETURN NULL;
    END IF;
    IF TG_OP IN ('DELETE', 'UPDATE') THEN
        UPDATE council_members SET vote_count = vote_count - 1,
            updated_at = CURRENT_TIMESTAMP WHERE id = OLD.council_member_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        UPDATE council_members SET vote_count = vote_count + 1,
            last_seen = GREATEST(last_seen, NEW.vote_date), updated_at = CURRENT_TIMESTAMP
        WHERE id = NEW.council_member_id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE TRIGGER votes_maintain_member_count
    AFTER INSERT OR DELETE OR UPDATE OF council_member_id ON votes
    FOR EACH ROW EXECUTE FUNCTION maintain_vote_count();

ALTER TABLE votes ADD COLUMN parse_run_id TEXT;
ALTER TABLE votes ADD COLUMN observation_ordinal INTEGER;
ALTER TABLE votes ADD FOREIGN KEY (parse_run_id, observation_ordinal)
    REFERENCES minutes_observations(run_id, ordinal);
