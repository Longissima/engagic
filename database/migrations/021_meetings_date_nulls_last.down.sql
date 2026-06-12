CREATE INDEX IF NOT EXISTS idx_meetings_banana_date ON meetings(banana, date DESC);
DROP INDEX IF EXISTS idx_meetings_banana_date_nl;
