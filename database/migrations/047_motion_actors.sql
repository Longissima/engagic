-- A body acts, not only hosts. Formalizes what the pipeline already produces.
--
-- 460 rows in council_members are not people, and they already hold 9,242
-- sponsorships and 1,020 votes: Jacksonville's "Land Use & Zoning Committee"
-- sponsors 378 matters, Napa's "Board of Supervisors" 366, Madison's "BOARD OF
-- PUBLIC WORKS" 188. A committee recommending or sponsoring at the council level
-- is real and already recorded -- as a fake person. This gives it somewhere to be.
--
-- Derived layer again: the raw rows stay exactly as captured, and everything here
-- is recomputable by scripts/normalize_roster.py.

-- Which kind of actor a raw roster row actually names. "Not a person" conflated
-- four things that need different handling: a body can move and sponsor, a bare
-- role is a person the minutes declined to name, an artifact is a parse failure.
ALTER TABLE council_member_profiles
    ADD COLUMN IF NOT EXISTS actor_kind TEXT
        CHECK (actor_kind IN ('person', 'body', 'role', 'artifact'));

CREATE TABLE IF NOT EXISTS bodies (
    id          TEXT PRIMARY KEY,
    banana      TEXT NOT NULL,
    name        TEXT NOT NULL,
    -- A body observed only as an actor has no meetings of its own in our corpus;
    -- one observed only as a venue has never been recorded acting. Both are real.
    seen_as_venue BOOLEAN NOT NULL DEFAULT FALSE,
    seen_as_actor BOOLEAN NOT NULL DEFAULT FALSE,
    derived_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (banana, name)
);
CREATE INDEX IF NOT EXISTS idx_bodies_banana ON bodies(banana);

COMMENT ON TABLE bodies IS
    'Deliberative bodies, derived from meetings.title (as venue) and from roster '
    'rows that name a body rather than a person (as actor).';

-- Who moved, seconded, sponsored or recommended. One motion can carry a mover, a
-- seconder and a recommending committee at once, so this is a join and not
-- columns on item_motions. The actor is a person or a body, never both.
CREATE TABLE IF NOT EXISTS motion_actors (
    item_id      TEXT NOT NULL,
    motion_index SMALLINT NOT NULL,
    source       TEXT NOT NULL,
    role         TEXT NOT NULL CHECK (role IN ('mover', 'seconder', 'sponsor', 'recommender')),
    person_id    TEXT REFERENCES council_members(id) ON DELETE CASCADE,
    body_id      TEXT REFERENCES bodies(id) ON DELETE CASCADE,
    derived_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((person_id IS NULL) <> (body_id IS NULL)),
    FOREIGN KEY (item_id, motion_index, source)
        REFERENCES item_motions(item_id, motion_index, source) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_motion_actors
    ON motion_actors(item_id, motion_index, source, role,
                     COALESCE(person_id, ''), COALESCE(body_id, ''));
CREATE INDEX IF NOT EXISTS idx_motion_actors_person ON motion_actors(person_id) WHERE person_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_motion_actors_body ON motion_actors(body_id) WHERE body_id IS NOT NULL;

COMMENT ON TABLE motion_actors IS
    'Who acted on a motion and in what role. A recommending committee is an actor '
    'here rather than a withheld observation; see reported_committee_action.';
