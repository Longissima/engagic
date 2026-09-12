-- The body an item's narrative credits, when it has no motion of its own here.
--
-- "At its June 24th meeting, the Planning Commission voted 5-2 to recommend
-- denial" is a fact about this matter's journey, not about any motion in this
-- meeting. Attaching it to the motions in the block credited the council's own
-- vote to the commission, so it lives at appearance grain: this matter, at this
-- meeting, referred by that body.
--
-- Verbatim in pass one; scripts/normalize_roster.py resolves it to bodies.id and
-- writes matter_actors(role='referrer').
ALTER TABLE matter_appearances ADD COLUMN IF NOT EXISTS reported_referrer TEXT;

COMMENT ON COLUMN matter_appearances.reported_referrer IS
    'Verbatim name of a body the minutes credit with referring or recommending '
    'this matter, captured when no motion in this meeting is that body''s action.';
