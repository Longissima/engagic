# parsing

This package turns document bytes into text and pulls structured facts out of text. It treats every input as hostile: scraped PDFs are malformed, enormous or adversarial often enough that containment is part of the design.

## Extraction (`pdf.py`)

`PdfExtractor` reads PDFs with PyMuPDF. It also handles DOCX, legacy DOC (antiword), RTF, PPTX and XLSX. Entry points are `extract_from_path`, `extract_from_bytes` and `extract_from_url`. The result carries:
- `text`, with a `--- PAGE n ---` marker before each page (1-indexed); page spans, vote receipts and motioncount all index into these markers, so never strip or renumber them;
- the method, the page count, OCR counts, `ocr_pending` pages, and success or error fields.

**OCR is per page, not per document.** A page whose text layer has fewer than `ocr_threshold` characters is rendered and passed to Tesseract. A mostly born-digital attachment with a few scanned pages is the common case. With `ocr_enabled=False`, thin pages are counted in `ocr_pending` instead of being OCR'd, which tells callers whether the output is already final.

`text_quality.py` detects garbled text layers (broken font CMaps) and excessive layout padding. A garbled page is re-read with OCR rather than trusted.

**Legislative formatting**: strikethrough and underline marks become `[DELETED: ...]` and `[ADDED: ...]`, so amendments keep their meaning in plain text.

## Containment (`subprocess_guard.py`, `memory_budget.py`)

PyMuPDF is a C extension that can segfault, eat unbounded memory or hang on one bad page. All native PDF work (extraction, chunking, page counting) runs in a child process through `run_guarded`. The child has an address-space cap, a deadline with kill on timeout, and exit-code reporting. Async callers use `run_guarded_thread`, which reaps the child before releasing temp files and slots.

Extraction, chunking and large downloads also share one FIFO memory budget per process. Work is admitted only when its reservation fits, so admission is bounded before allocations become visible to the kernel. Settings:
- `ENGAGIC_WORK_MEMORY_BUDGET_MB` (default 2048);
- `ENGAGIC_EXTRACTION_MIN_AVAILABLE_MB` of headroom (default 700);
- `ENGAGIC_EXTRACTION_MEMORY_WAIT_SECONDS` before admission gives up (default 120).

The production box has 3.8 GB, so budget for that box and not for one job.

## Facts from text

- **`identifiers.py`** finds the durable identifier a legislative body cites for something that recurs across meetings: a contract number, a law-department file, a zoning case. The sync item funnel calls it for every vendor, and its result becomes `matter_file` and so the matter id. A wrong identifier permanently merges unrelated items under one summary, so every pattern is anchored on an explicit label and never guesses from bare numbers. `canonical_matter_file` normalizes vendor-supplied files. `detect_numbering_runs` and `numbering_period` handle cities whose numbers restart each year, weekly via `scripts/rekey_matters.py`.
- **`participation.py`** extracts public-comment email, phone, Zoom and address details from meeting text before summarization.
- **`chicago_pdf.py`, `menlopark_pdf.py`** are agenda parsers for two custom adapters.

Agenda item chunking lives in `vendors/adapters/parsers/`, not here.
