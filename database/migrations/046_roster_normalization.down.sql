-- Derived data only: recomputable from council_members.name, votes and
-- meetings.title, so dropping it loses nothing. The raw table was never altered.
DROP TABLE IF EXISTS member_bodies;
DROP TABLE IF EXISTS council_member_profiles;
