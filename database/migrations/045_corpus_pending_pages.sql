-- NULL means older extraction did not retain page-level completeness.
-- An empty array means no pages were pending when the text was extracted.
ALTER TABLE document_blob ADD COLUMN IF NOT EXISTS ocr_pending_pages INTEGER[];
