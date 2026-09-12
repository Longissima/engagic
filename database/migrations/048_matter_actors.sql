-- Who sponsors a matter, when the sponsor is a body rather than a person.
--
-- sponsorships is keyed council_member_id, so the 6,308 rows where a committee
-- sponsors a matter are stored as a fake person: Jacksonville's "Land Use &
-- Zoning Committee" holds 378, Napa's "Board of Supervisors" 366. motion_actors
-- cannot hold them -- it is keyed (item_id, motion_index) and a matter sponsored
-- at introduction has no motion to hang on. This is the matter-level equivalent.
--
-- Derived: sponsorships stays exactly as captured and is still the raw record.

CREATE TABLE IF NOT EXISTS matter_actors (
    matter_id   TEXT NOT NULL REFERENCES city_matters(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('sponsor', 'co_sponsor', 'requester', 'referrer')),
    person_id   TEXT REFERENCES council_members(id) ON DELETE CASCADE,
    body_id     TEXT REFERENCES bodies(id) ON DELETE CASCADE,
    -- Retained from the raw sponsorship so ordering and primacy survive the move.
    is_primary  BOOLEAN NOT NULL DEFAULT FALSE,
    actor_order INTEGER,
    derived_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK ((person_id IS NULL) <> (body_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_matter_actors
    ON matter_actors(matter_id, role, COALESCE(person_id, ''), COALESCE(body_id, ''));
CREATE INDEX IF NOT EXISTS idx_matter_actors_person ON matter_actors(person_id) WHERE person_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_matter_actors_body ON matter_actors(body_id) WHERE body_id IS NOT NULL;

COMMENT ON TABLE matter_actors IS
    'Derived from sponsorships: the same sponsorship expressed with the actor it '
    'actually names, a person or a body. A role-only sponsor ("Mayor") and a parse '
    'artifact are excluded -- neither identifies an actor to attribute to.';
