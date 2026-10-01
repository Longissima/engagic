-- Numbering period for matter files that restart (St. Louis "Board Bill 66"
-- exists once per session; St. Louis County prints "Bill No. 65, 2024").
-- Informational: the period is already part of the matter id hash
-- (generate_matter_id), this column makes it visible without re-deriving.
ALTER TABLE city_matters ADD COLUMN IF NOT EXISTS matter_year TEXT;
