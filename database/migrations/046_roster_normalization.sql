-- Roster normalization as a separate derived layer. The raw table is not altered.
--
-- council_members.name stays exactly what the minutes printed, including
-- "ALD. BAUMAN" and "District Attorney", and no column of that table is written
-- by normalization -- not even updated_at. Derived values live here instead, so
-- which half is authoritative is a question about which table you read, not a
-- question about which column and what its comment says.
--
-- Everything below is recomputable from council_members.name, votes and
-- meetings.title by scripts/normalize_roster.py. Dropping it loses nothing.

CREATE TABLE IF NOT EXISTS council_member_profiles (
    council_member_id TEXT PRIMARY KEY REFERENCES council_members(id) ON DELETE CASCADE,
    -- The label to show. clean_name already strips the title and the gazetteer
    -- already applies it when resolving, so the stored label was the only thing
    -- still carrying one.
    display_name      TEXT NOT NULL,
    -- Which rows are one person. A bare-surname row ("McNeill") and a full-name
    -- row ("Sean McNeill") are the same member; ingestion creates a row per
    -- distinct printed spelling, so these accumulate.
    person_key        TEXT,
    -- False for a staff role or parse artifact captured as a member. Such rows
    -- are flagged, never deleted: the row is evidence of what the document said
    -- and its id may already be referenced by votes.
    is_person         BOOLEAN NOT NULL,
    derived_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_member_profiles_person_key
    ON council_member_profiles(person_key) WHERE person_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_member_profiles_not_person
    ON council_member_profiles(council_member_id) WHERE is_person IS FALSE;

COMMENT ON TABLE council_member_profiles IS
    'Derived labels and clustering over council_members. Raw names are never '
    'edited; recomputed in full by scripts/normalize_roster.py.';

-- A member's votes belong to a body, not to a city. Wauwatosa's minutes include
-- the Milwaukee Metro Fire Rescue Board of Directors, whose directors are also
-- genuine Wauwatosa officials: the motions are real and belong to that board,
-- not to the Common Council, so a city-wide vote_count sums two offices.
-- meetings.title already records which body sat, so membership is derivable and
-- no row needs inventing or discarding.
CREATE TABLE IF NOT EXISTS member_bodies (
    council_member_id TEXT NOT NULL REFERENCES council_members(id) ON DELETE CASCADE,
    body              TEXT NOT NULL,
    vote_count        INTEGER NOT NULL DEFAULT 0,
    first_vote        DATE,
    last_vote         DATE,
    derived_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (council_member_id, body)
);
CREATE INDEX IF NOT EXISTS idx_member_bodies_body ON member_bodies(body);

COMMENT ON TABLE member_bodies IS
    'Derived: which bodies a member is recorded voting in, and how often. '
    'Recomputed from votes joined to meetings.title by scripts/normalize_roster.py.';
