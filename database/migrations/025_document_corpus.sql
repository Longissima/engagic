-- The ground-truth corpus index (docs/CORPUS_ARCHITECTURE.md).
--
-- Extraction writes once; everything downstream reads. Original document
-- bytes and their extracted text live in R2 (engagic-corpus bucket,
-- originals/<sha256> and text/<sha256>.txt), content-addressed by
-- sha256(source bytes) so identical bytes dedup automatically and
-- re-extraction becomes a fact-check ("do we have text for this hash?")
-- instead of re-paid work. These tables are the pointer + provenance
-- layer: big blobs never sit inline in hot rows.
--
-- document_blob is one row per distinct byte sequence ever acquired.
-- Provenance (extract_method + extract_version) is mandatory design: a
-- corpus without "how was this produced" rots, and we will re-extract
-- selectively when the extractor upgrades (Tesseract -> VLM OCR, Layout).
--
-- No foreign keys into meetings/items: the corpus is an append-only
-- archive that outlives any particular meeting row. Linkage runs through
-- document_source.source_identity, the signature-stripped attachment URL
-- (pipeline.utils.attachment_identity) that item attachments already carry.

CREATE TABLE IF NOT EXISTS document_blob (
    content_sha256 TEXT PRIMARY KEY,
    bytes BIGINT NOT NULL,
    content_type TEXT,
    original_key TEXT,              -- R2 key of archived source bytes; NULL until archived
    text_key TEXT,                  -- R2 key of extracted text; NULL until extracted
    extract_method TEXT,            -- 'pymupdf' | 'pymupdf+ocr' | 'antiword' | ...
    extract_version TEXT,           -- bump corpus.store.EXTRACT_VERSION to force re-extraction
    page_count INTEGER,
    ocr_page_count INTEGER,         -- pages that hit the per-page OCR fallback
    text_chars BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    text_extracted_at TIMESTAMP
);

-- Where these bytes have been seen. Many-to-many in practice: one document
-- appears at many URLs (agenda vs packet vs re-signed SAS links) and one
-- URL can serve revised bytes over time (same identity, new hash).
CREATE TABLE IF NOT EXISTS document_source (
    content_sha256 TEXT NOT NULL REFERENCES document_blob(content_sha256) ON DELETE CASCADE,
    source_identity TEXT NOT NULL,  -- attachment_identity(url): stable across re-signing
    banana TEXT,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (content_sha256, source_identity)
);

-- Read path: attachment URL -> identity -> newest hash -> text.
CREATE INDEX IF NOT EXISTS idx_document_source_identity
    ON document_source (source_identity, first_seen DESC);

-- Re-extraction sweeps: archived originals still awaiting text. Partial so
-- the index stays proportional to the backlog, not the corpus.
CREATE INDEX IF NOT EXISTS idx_document_blob_untexted
    ON document_blob (created_at)
    WHERE text_key IS NULL;
