-- Where items.body_text was cut from, so every piece of item text points at an
-- archived original. A list of {url, pages?, content_sha256?}: a chunked
-- agenda's item names the PDF pages its listing came from, a scraped item the
-- HTML/JSON page it was parsed out of (content_sha256 pins the exact bytes
-- parsed). Several entries when a body merges sources (Granicus packet
-- evidence). Documents an item cites stay in items.attachments; this column
-- only locates the inline text. Existing rows stay NULL: no backfill.
ALTER TABLE items ADD COLUMN IF NOT EXISTS body_source JSONB;
