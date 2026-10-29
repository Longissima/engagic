-- Which archived documents each item lists: the join of an item's attachments
-- to the corpus, published by its owner. Readers ask it both ways: an item's
-- documents, and every item that carries one document (a pending bill's packet
-- on twenty weekly agendas, one staff report under three meetings). motioncount
-- kept its own 4.6 GB copy of this join; it now reads this table.
--
-- Only resolved attachments have rows: an attachment whose bytes the corpus
-- does not hold yet appears once they are archived (refresh_item_documents runs
-- at item store, after sync archival, and in the corpus window). `ordinal` is
-- the attachment's position on the item; `pages` is set for a page span of a
-- packet, so siblings citing different pages of one packet are different
-- documents. Hashes are raw bytes, half the size of the hex text.
CREATE TABLE IF NOT EXISTS item_documents (
    content_sha256 BYTEA NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    ordinal SMALLINT NOT NULL,
    page_first INTEGER,
    page_last INTEGER,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (content_sha256, item_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_item_documents_item ON item_documents (item_id);
-- New appearances since a reader's cursor, for re-projecting cards whose history grew.
CREATE INDEX IF NOT EXISTS idx_item_documents_recorded ON item_documents (recorded_at);
