-- Detected numbering runs per jurisdiction and identifier series ("Bill",
-- "Resolution", "Ordinance"). Derived, never configured: written by
-- scripts/rekey_matters.py from the city's own numbers
-- (parsing.identifiers.detect_numbering_runs), read by the sync funnel.
-- `starts` holds every run start after the first; a series with no row
-- never restarted and its numbers carry no period.
CREATE TABLE IF NOT EXISTS numbering_runs (
    banana TEXT NOT NULL REFERENCES jurisdictions(banana) ON DELETE CASCADE ON UPDATE CASCADE,
    series TEXT NOT NULL,
    starts DATE[] NOT NULL,
    detected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (banana, series)
);
