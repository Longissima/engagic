-- Meetings city-timeline index aligned with query ordering.
--
-- Every city-timeline consumer orders `date DESC NULLS LAST` (spygov city
-- layout, state meeting lists). The existing idx_meetings_banana_date is
-- `date DESC`, which in btree terms means NULLS FIRST — the orderings
-- don't match, so the planner can't use it for an ordered scan with LIMIT
-- early-exit and falls back to seq scan + sort (~12ms on the busiest
-- cities). With this index the same query is a sub-millisecond ordered
-- index scan with no sort step.
--
-- Already applied to the live DB on 2026-06-11 via CREATE INDEX
-- CONCURRENTLY; IF NOT EXISTS makes this migration a no-op there. Plain
-- CREATE INDEX here because migrations may run inside a transaction,
-- where CONCURRENTLY is not allowed.
CREATE INDEX IF NOT EXISTS idx_meetings_banana_date_nl
    ON meetings(banana, date DESC NULLS LAST);

-- The old composite is now fully redundant: get_meetings_for_city orders
-- NULLS LAST (aligned with this migration), and the remaining banana-scoped
-- query (get_meetings_by_date_range) filters `date >= $2`, which excludes
-- NULLs — equality-on-banana + range-on-date scans work identically on
-- either index. Dropping it halves index maintenance on every meetings
-- write. (DROP INDEX takes a brief ACCESS EXCLUSIVE lock on meetings;
-- momentary at this table size.)
DROP INDEX IF EXISTS idx_meetings_banana_date;
