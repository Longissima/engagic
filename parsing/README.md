# parsing/ - PDF Extraction & Parsing

Document parsing utilities for legislative PDF extraction. Treats PDF parsing as adversarial: assumes malformed inputs, prioritizes extraction accuracy over speed.

## Files

### pdf.py - Core PDF Extractor

Primary extraction using PyMuPDF (fitz) with OCR fallback via Tesseract.

**Key class: `PdfExtractor`**

```python
from parsing.pdf import PdfExtractor

extractor = PdfExtractor(
    ocr_threshold=100,                  # Min chars per page before OCR triggers
    ocr_dpi=150,                        # DPI for OCR rendering (lower = faster, higher = better quality)
    detect_legislative_formatting=True,  # Enable [DELETED]/[ADDED] tag detection
    max_ocr_workers=None                # Auto-detects: min(cpu_count, 4)
)

# Extract from URL (fetches with browser-like headers)
result = extractor.extract_from_url(pdf_url, extract_links=True)

# Extract from bytes
result = extractor.extract_from_bytes(pdf_bytes, extract_links=True)

# Validate extracted text quality (length > 100, letter ratio > 0.3)
is_valid = extractor.validate_text(result["text"])
```

**Return format:**
```python
{
    "success": bool,
    "text": str,              # Extracted text with --- PAGE N --- delimiters
    "method": str,            # "pymupdf" or "pymupdf+ocr"
    "page_count": int,
    "extraction_time": float, # Seconds elapsed
    "ocr_pages": int,         # Pages where OCR actually improved over native text
    "links": list,            # Only present if extract_links=True
}
```

On failure, raises `ExtractionError` (from `exceptions` module).

**Extraction pipeline:**
1. **Pass 1** — Extract text from all pages (main thread, PyMuPDF is not thread-safe). Pages with < `ocr_threshold` chars are queued for OCR. Page images are pre-rendered to PNG bytes in the main thread.
2. **Legislative check** — If `detect_legislative_formatting` is enabled, scans the first 5 pages for a formatting legend and up to 30 pages for geometric redline evidence. Activates `[DELETED: ...]` / `[ADDED: ...]` tagging for a legend, three independent strike marks, or a paired strike-and-underline change.
3. **Pass 2** — Runs OCR in parallel via `ThreadPoolExecutor`. Uses `_is_ocr_better()` to decide whether OCR output replaces native text (requires 2x more chars with >40% letters, or more chars with >70% letters).

**OCR safeguards:**
- 100MP pixel limit (PIL `DecompressionBombWarning` converted to error)
- 60s timeout per page, 300s total budget across all pages
- `OMP_THREAD_LIMIT=1` to prevent Tesseract internal threading
- Auto-detects worker count from CPU cores (capped at 4)

### chicago_pdf.py - Chicago Agenda Parser

Parses Chicago City Council agenda PDFs to extract record numbers. Self-contained (no external deps beyond `re`).

```python
from parsing.chicago_pdf import parse_chicago_agenda_pdf

parsed = parse_chicago_agenda_pdf(pdf_text)
# Returns: {"items": [{"record_number": "O2025-0019668", "sequence": 1, "title_hint": "Amendment of..."}, ...]}
```

**Record number pattern:** `(O2025-0019668)` — 1-3 letter prefix + 4-digit year + hyphen + 7-digit sequence. Prefixes include O (Ordinance), R (Resolution), SO (Substitute Ordinance).

Returns empty items list if no records found. Does not raise exceptions.

### menlopark_pdf.py - Menlo Park Agenda Parser

Parses Menlo Park agenda PDFs with letter-based section structure (H., I., J., K.) and attachment mapping.

```python
from parsing.menlopark_pdf import parse_menlopark_pdf_agenda

parsed = parse_menlopark_pdf_agenda(pdf_text, links)
# Returns: {"items": [{item_id, title, sequence, attachments: [{name, url, type}]}, ...]}
```

**Key behavior:**
- Item IDs: `A1.`, `H1.`, `J5.`, etc. (letter + number format)
- Attachments matched by filename prefix — Menlo Park encodes item IDs in filenames (e.g., `h1-20251021-cc-tour-de-menlo.pdf`)
- Validates titles to reject form field garbage (short text, all-caps labels, known form keywords)
- Detects attachment markers: `(Attachment)`, `(Staff Report #XX-XXX-CC)`, `(Presentation)`

Returns empty items list if no items found. Does not raise exceptions.

### participation.py - Participation Info Extractor

Extracts civic engagement contact info from meeting text before AI summarization. Returns Pydantic models from `database.models`.

```python
from parsing.participation import parse_participation_info

info = parse_participation_info(meeting_text)
# Returns: ParticipationInfo or None
```

**Extracts:**
- Emails with inferred purpose (written comments, city clerk, media submissions, general contact)
- Phone numbers (normalized to `+1XXXXXXXXXX` format)
- Virtual meeting URLs (Zoom, Google Meet, Teams, WebEx, GoToMeeting)
- Streaming URLs with platform detection (YouTube, Facebook Live, Granicus, Midpen Media, Vimeo)
- Cable TV channel info
- Zoom meeting IDs (handles spaces/dashes)
- Hybrid vs virtual-only detection

Returns `None` if nothing found. Does not raise exceptions.

### identifiers.py - Durable Matter Identifiers

Pulls the identifier a legislative body uses for a thing that recurs across
meetings — a contract, a case, a law-department file — out of agenda text.
Called from the sync item funnel for every vendor, so any item whose vendor
supplies no matter key can still be linked to a matter.

```python
from parsing.identifiers import extract_identifier

extract_identifier(item.title, item.body_text)
# Returns: ("Contract 6007968", "Contract") or None
```

**Why it matters:** without it, one Detroit contract that passes through
committee, formal session and two amendments is four unrelated items with four
separately generated summaries. With it, they are one matter with one canonical
summary and a timeline.

Every pattern is anchored on an explicit label and never guesses from bare
numbers, because a wrong identifier permanently merges unrelated items into one
`city_matters` row. Notable guards, all learned from real corpus damage:

- `Master Contract No. X; Procurement Contract No. Y` keys on Y — the master is a
  vendor-level umbrella and would merge every agreement with that vendor.
- Amendment suffixes are preserved (`6006718-A1` is not `6006718`).
- A dangling hyphen or a funding share (`6006718-100%`) is not a suffix.

## Error Handling

Only `PdfExtractor` raises exceptions — wraps failures in `ExtractionError`:

```python
from exceptions import ExtractionError

try:
    result = extractor.extract_from_url(url)
except ExtractionError as e:
    logger.error("extraction failed", url=e.document_url, error=str(e))
```

The other parsers (`chicago_pdf`, `menlopark_pdf`, `participation`) return empty results or `None` on failure.

## Dependencies

- `PyMuPDF` (fitz) — Primary PDF parsing
- `pytesseract` — OCR fallback
- `Pillow` — Image processing for OCR
- `requests` — PDF URL fetching
- `pydantic` — Data models for ParticipationInfo (via `database.models`)

Chicago and Menlo Park parsers are self-contained (stdlib only: `re`, `typing`).
