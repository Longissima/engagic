ALTER TABLE jurisdictions DROP CONSTRAINT IF EXISTS jurisdictions_type_class;
DROP TRIGGER IF EXISTS jurisdictions_type_from_kind ON jurisdictions;
DROP FUNCTION IF EXISTS jurisdictions_type_from_kind();
UPDATE jurisdictions j SET type = l.type FROM jurisdiction_type_legacy l WHERE l.banana = j.banana;
DROP INDEX IF EXISTS idx_jurisdictions_kind;
ALTER TABLE jurisdictions DROP COLUMN IF EXISTS kind;
DROP TABLE IF EXISTS jurisdiction_kinds;
DROP TABLE IF EXISTS jurisdiction_type_legacy;
