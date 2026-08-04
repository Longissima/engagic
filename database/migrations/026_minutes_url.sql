-- 026: minutes_url on meetings.
-- Minutes are the universal source for per-member roll calls in the ~87% of
-- jurisdictions whose vendors expose no votes API. Adapters have discovered
-- minutes URLs for years and dropped them (dead metadata dicts); this column
-- is where discovery lands. Content ingestion (R2 corpus) and roll-call
-- parsing build on top — see spygov docs/MODEL_DOCTRINE.md, "The roll-call track".
-- Minutes publish AFTER the meeting (often approved at the next session), so
-- this fills in on resync within the fetcher's back-window; the upsert keeps
-- the last non-null value.

ALTER TABLE meetings ADD COLUMN IF NOT EXISTS minutes_url TEXT;
