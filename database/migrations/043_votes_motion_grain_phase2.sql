-- 043: let a member vote more than once on a matter in one meeting.
-- Migration 041 added motion_index and the four-column unique index but kept
-- UNIQUE(council_member_id, matter_id, meeting_id) so writers still running
-- the old code would not break. That constraint is what limited each item to
-- a single stored motion, which meant an amendment that failed and the
-- adoption that followed collapsed into one row and the dissent disappeared.
--
-- Order matters and has been followed: every writer targets
-- uq_votes_member_matter_meeting_motion first, services restarted, then this.
-- Rolling back requires the table to hold one motion per member per matter
-- per meeting again; the down migration below will fail while extra motions
-- exist, which is the correct outcome rather than silent data loss.

ALTER TABLE votes DROP CONSTRAINT IF EXISTS votes_council_member_id_matter_id_meeting_id_key;
