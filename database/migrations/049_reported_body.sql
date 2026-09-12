-- The body the minutes name as having acted, verbatim.
--
-- reported_committee_action previously withheld the whole observation: a
-- committee's recommendation appearing in council minutes was detected and then
-- discarded, so the fact that the Planning Commission recommended denial was
-- lost entirely. 1,326 such mentions exist across ~400 meetings and the old
-- detector caught 16 of them.
--
-- Pass one stores the name as printed. Pass two resolves it to bodies.id and
-- writes motion_actors, so raw never depends on a derived table.
ALTER TABLE item_motions ADD COLUMN IF NOT EXISTS reported_body TEXT;

COMMENT ON COLUMN item_motions.reported_body IS
    'Verbatim name of the body the minutes credit with this action, when it is '
    'not the body whose meeting this is. Resolved to bodies.id by '
    'scripts/normalize_roster.py into motion_actors(role=recommender).';
