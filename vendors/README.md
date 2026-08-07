# Vendors Module - Civic Tech Platform Adapters

**Fetch meeting data from 19 civic tech platforms.** Unified adapter architecture with vendor-specific parsers and shared utilities.

**Last Updated:** June 2026

---

## Overview

The vendors module provides adapters for fetching meeting data from civic technology platforms used by local governments. Each adapter implements the `AsyncBaseAdapter` interface while handling vendor-specific quirks in HTML parsing, API integration, and data extraction.

**Architecture Pattern:** AsyncBaseAdapter + Vendor-Specific Parsers + Shared Utilities

```
vendors/
├── adapters/           # 19 async adapters
│   ├── base_adapter_async.py          # Async base (351 lines)
│   ├── legistar_adapter_async.py      # Legistar async (1254 lines)
│   ├── primegov_adapter_async.py      # PrimeGov async (320 lines)
│   ├── granicus_adapter_async.py      # Granicus async (778 lines)
│   ├── iqm2_adapter_async.py          # IQM2 async (576 lines)
│   ├── novusagenda_adapter_async.py   # NovusAgenda async (199 lines)
│   ├── escribe_adapter_async.py       # eScribe async (502 lines)
│   ├── civicclerk_adapter_async.py    # CivicClerk async (502 lines)
│   ├── civicplus_adapter_async.py     # CivicPlus async (369 lines)
│   ├── civicengage_adapter_async.py   # CivicEngage async (431 lines)
│   ├── civicweb_adapter_async.py      # CivicWeb async (360 lines)
│   ├── municode_adapter_async.py      # Municode async (582 lines)
│   ├── onbase_adapter_async.py        # OnBase async (464 lines)
│   ├── proudcity_adapter_async.py     # ProudCity async (930 lines)
│   ├── visioninternet_adapter_async.py # Vision Internet async (451 lines)
│   ├── wp_events_adapter_async.py     # WP Events async (572 lines)
│   ├── custom/
│   │   ├── berkeley_adapter_async.py  # Berkeley async (295 lines)
│   │   ├── chicago_adapter_async.py   # Chicago async (796 lines)
│   │   ├── menlopark_adapter_async.py # Menlo Park async (182 lines)
│   │   └── ross_adapter_async.py      # Ross async (420 lines)
│   └── parsers/        # Vendor-specific parsers (HTML) + PDF chunking cascade
│       ├── legistar_parser.py         # Legistar HTML tables (373 lines)
│       ├── primegov_parser.py         # PrimeGov HTML items (315 lines)
│       ├── granicus_parser.py         # Granicus HTML formats (865 lines)
│       ├── municode_parser.py         # Municode HTML sections (213 lines)
│       ├── novusagenda_parser.py      # NovusAgenda HTML items (116 lines)
│       ├── civicplus_parser.py        # CivicPlus HTML agendas (170 lines)
│       ├── router.py                  # Chunking cascade: ladders, audit, hints
│       ├── pdf_profile.py             # Morphology signal measurement
│       ├── morphology.py              # Profile -> named shape classifier
│       ├── quality.py                 # Title lint/repair, segmentation smell
│       ├── agenda_chunker.py          # v1 engine: URL + hardcoded-TOC paths
│       ├── agenda_chunker_v2.py       # v2 engine: anchor-first, relative TOC
│       └── text_chunker.py            # text engine: numbered-heading splitting
├── extractors/         # Data extraction utilities
│   └── council_member_extractor.py    # Sponsor extraction (281 lines)
├── utils/              # Shared utilities
│   └── attachments.py                 # Attachment version filtering (162 lines)
├── factory.py          # Adapter dispatcher (74 lines)
├── rate_limiter_async.py              # Async vendor rate limiting (53 lines)
├── session_manager_async.py           # Async HTTP pooling (143 lines)
├── validator.py        # Domain validation (270 lines)
└── schemas.py          # Pydantic validation schemas (155 lines)

Total: ~15,328 lines
```

---

## Architecture

### AsyncBaseAdapter Pattern

All vendor adapters inherit from `AsyncBaseAdapter`, which provides:
- **Async HTTP client** (aiohttp via `AsyncSessionManager`) with timeout and error handling
- **Date parsing** utilities (handles various vendor formats)
- **Metrics collection** via `MetricsCollector` protocol
- **Error handling** with `VendorHTTPError` (vendor, URL, city context)
- **Fallback vendor ID generation** (SHA256 hash for vendors without native IDs)
- **Meeting status detection** (cancelled, postponed, deferred, revised)
- **Meeting validation** (requires vendor_id and title; validates optional start)
- **FetchResult contract** - distinguishes "0 meetings" from "adapter failed"

```python
# vendors/adapters/base_adapter_async.py

@dataclass
class FetchResult:
    """Distinguishes success from failure."""
    meetings: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None
    error_type: Optional[str] = None

class AsyncBaseAdapter:
    def __init__(self, city_slug: str, vendor: str, metrics: Optional[MetricsCollector] = None):
        self.slug = city_slug
        self.vendor = vendor
        self.metrics = metrics or NullMetrics()

    async def fetch_meetings(self, days_back: int = 7, days_forward: int = 14) -> FetchResult:
        """Validates results, catches exceptions, returns FetchResult."""
        # Calls _fetch_meetings_impl(), validates each meeting, wraps result

    async def _fetch_meetings_impl(self, days_back: int, days_forward: int) -> List[Dict[str, Any]]:
        """Subclass must implement. Return raw meeting dicts."""
        raise NotImplementedError

    async def _get(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """GET request via shared session. Raises VendorHTTPError on failure."""

    async def _post(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """POST request. Raises VendorHTTPError on failure."""

    async def _get_json(self, url: str, **kwargs) -> Any:
        """GET + JSON parse. Handles content-type mismatches."""

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse vendor date formats (ISO, US, human-readable)."""

    def _generate_fallback_vendor_id(self, title: str, date: Optional[datetime], ...) -> str:
        """SHA256 hash (12 hex chars) for vendors without native IDs."""

    def _parse_meeting_status(self, title: str, date_str: Optional[str] = None) -> Optional[str]:
        """Detect cancelled/postponed/deferred/revised/rescheduled from text."""
```

### Adapter ID Contract

Adapters return `vendor_id` (the native vendor identifier), NOT canonical `meeting_id`. The database layer generates canonical meeting IDs.

```python
# Adapter returns meeting dict with vendor_id
{
    "vendor_id": "12345",           # Native ID from vendor (required)
    "title": "City Council",
    "start": "2025-11-10T18:00:00",
    "agenda_url": "https://...",    # HTML agenda (when items extracted)
    "packet_url": "https://...",    # PDF packet (monolithic fallback)
    "items": [...],                 # Optional: extracted agenda items
    "participation": {...},         # Optional: public comment info
    "meeting_status": "cancelled",  # Optional: cancelled/postponed/etc.
    "metadata": {...},              # Optional: vendor-specific extras
}
```

**vendor_id by Vendor:**

| Vendor | vendor_id Source | Example |
|--------|-----------------|---------|
| Legistar | `EventId` from API | `"98765"` |
| PrimeGov | `id` from API | `"12345"` |
| Chicago | `meetingId` from API | `"abc-uuid-123"` |
| NovusAgenda | `MeetingID` from HTML or fallback hash | `"4567"` |
| CivicClerk | `id` from OData API | `"789"` |
| IQM2 | ID from Detail_Meeting URL | `"1234"` |
| CivicPlus | `civic_` + ID from URL or MD5 hash | `"civic_456"` |
| eScribe | `escribe_` + UUID from meeting URL | `"escribe_7b0..."` |
| Granicus | `event_id` from ViewPublisher listing | `"5678"` |
| OnBase | Meeting ID from page JSON/HTML | `"9012"` |
| Municode | `MeetingID` from API or date+type composite | `"3456"` |
| CivicEngage | SHA256 hash of ADID or title+date | `"a1b2c3d4e5f6"` |
| CivicWeb | Meeting `Id` from MeetingInformation URL | `"7890"` |
| ProudCity | WP post `id` from REST API | `"1234"` |
| Vision Internet | Event ID from calendar detail URL | `"5678"` |
| WP Events | WP post `id` from REST API | `"9012"` |
| Berkeley | SHA256 hash of URL path + date | `"a1b2c3d4e5f6"` |
| Menlo Park | SHA256 hash of PDF URL + date | `"f6e5d4c3b2a1"` |
| Ross | Node ID from PDF path or detail page slug | `"3456"` |

**Database generates canonical ID:** `{banana}_{8-char-md5-hash}`

See `database/README.md` for ID generation details.

---

## Vendor Adapters

### Item-Level Adapters (18 adapters)

These adapters extract **structured agenda items** from HTML agendas, APIs, or PDFs. Items are stored separately with `matter_id`, `matter_file`, titles, and PDF links.

**1. Legistar (1212 lines)**
- **Dual mode:** API-first (`/Events` endpoint), HTML fallback (`Calendar.aspx` scraping)
- **API:** OData-style filtering, handles both JSON and XML responses (NYC returns XML)
- **HTML:** Calendar RadGrid → meeting detail pages → item tables via `legistar_parser.py`
- **Item extraction:**
  - API: `EventItems` endpoint per event, concurrent fetches
  - HTML: `parse_html_agenda()` parses `rgMasterTable` with column mapping
- **Matter tracking:** `EventItemMatterId`, `EventItemMatterFile`, sponsors from `/matters/{id}/sponsors`
- **Votes:** Fetched from `/EventItems/{id}/Votes` with normalization (yea→yes, nay→no, etc.)
- **Attachments:** From `/matters/{id}/attachments`, filters Leg Ver versions (keeps highest)
- **Roster:** `fetch_roster_data()` fetches Bodies and OfficeRecords for committee membership
- **City examples:** Seattle WA, NYC, Cambridge MA

**2. PrimeGov (320 lines)**
- **API + HTML scraping:** REST API for meeting lists, HTML scraping for item extraction
- **API endpoints:** `/api/v2/PublicPortal/ListUpcomingMeetings` and `/ListArchivedMeetings?year=`
- **Multiple agenda types:** Regular, Continuation, Special agendas fetched in parallel, merged with deduplication
- **Parser:** `primegov_parser.py` handles three HTML patterns (LA/newer, Palo Alto/older, Boulder/table)
- **Matter tracking:** `data-mig` GUID, `data-itemid`, `forcepopulate` table metadata
- **Attachments:** Via `historyattachment` API endpoint with `history_id`
- **Participation:** Extracted from agenda HTML (contact info, zoom links)
- **City examples:** Palo Alto CA, Mountain View CA, Sunnyvale CA

**3. Granicus (600 lines)**
- **HTML scraping:** Two-step process - ViewPublisher.php listing → AgendaViewer/AgendaOnline detail
- **Config dependency:** Requires `data/granicus_view_ids.json` mapping base URLs to view IDs
- **Parser:** `granicus_parser.py` handles four HTML formats:
  - AgendaOnline accessible view (`ViewMeetingAgenda`)
  - AgendaOnline table-based (older format)
  - Original AgendaViewer with File IDs and MetaViewer attachments
  - S3/CloudFront grid HTML — native Granicus sites (e.g. Bozeman MT, Carson City NV) where AgendaViewer.php redirects to an S3-hosted HTML page with CSS grid layout, h2/h3 sections, and CloudFront PDF links
- **Extraction flow:** HTML parsing (try all 4 formats) → PDF fallback with agenda/packet escalation → monolithic fallback
- **PDF fallback (two-step):** When HTML parsing yields no items, `_find_agenda_and_packet_urls` locates both the agenda PDF and the packet PDF from the HTML page. Tries the agenda PDF first (may have hyperlinked attachment URLs via URL-based chunking). If the agenda PDF yields hollow items (no attachments, no body_text), escalates to the packet PDF for TOC-based chunking with embedded memo body_text. Falls back to monolithic `packet_url` if neither produces usable items.
- **Attachments:** Three strategies depending on format:
  - AgendaOnline: fetched from item detail pages, DownloadFile→ViewDocument URL translation
  - S3 grid HTML: each item's staff report PDF is downloaded and parsed with PyMuPDF to extract embedded Legistar S3 attachment links
  - Legacy AgendaViewer: MetaViewer links from blockquote elements
- **Encoding:** UTF-8 with latin-1 fallback (Granicus often misreports encoding)
- **SSL:** Disabled for Granicus domains (cert issues on S3 redirects)
- **Participation:** Council member extraction from blue-styled header spans; participation info from page text
- **City examples:** Santa Monica CA, Redwood City CA, Bozeman MT, Carson City NV

**4. IQM2 (576 lines)**
- **HTML scraping:** Calendar page → Detail_Meeting.aspx → MeetingDetail table
- **Multiple URL patterns:** Tries `/Citizens`, `/Citizens/Calendar.aspx`, `/Citizens/Default.aspx`
- **Inline parsing:** No dedicated parser; parses MeetingDetail table directly in adapter
- **Matter tracking:** LegiFile IDs from `Detail_LegiFile.aspx` links, case numbers extracted via regex
- **Metadata:** Fetches matter_type, sponsors, department, attachments from Detail_LegiFile pages
- **Attachment dedup:** By file ID parameter in URL (same files appear in multiple views)
- **City examples:** Boise ID, Santa Monica CA, Cambridge MA, Buffalo NY

**5. NovusAgenda (199 lines)**
- **HTML scraping:** `/agendapublic` page → RadGrid table rows → MeetingView.aspx for items
- **Parser:** `novusagenda_parser.py` extracts items via CoverSheet.aspx links
- **Agenda selection:** Prioritizes "HTML Agenda" over "View Agenda" over generic links
- **Date format:** `%m/%d/%y` (2-digit year)
- **City examples:** Hagerstown MD, Houston TX

**6. Chicago (796 lines) - Custom**
- **REST API** at `api.chicityclerkelms.chicago.gov`
- **OData-style filtering** with pagination (500 per page, 2000 cap)
- **Item extraction hierarchy:** API `agenda.groups[].items[]` → PDF extraction → packet fallback
- **PDF fallback:** Extracts record numbers (O2025-0019668) from agenda PDF, fetches matter data via `/matter/recordNumber/`
- **Matter data:** Attachments, sponsors, status, votes all from `/matter/{id}` (votes embedded in actions)
- **Vote normalization:** Yea→yes, Nay→no, etc.
- **Stats tracking:** Per-sync metrics (meetings, items, votes, attachments, API requests)
- **Participation:** Public comment deadline and instructions
- **Metadata:** Video links, transcript links, body info, related meetings

**7. eScribe (420 lines)**
- **Calendar API + HTML scraping:** POST to `/MeetingsCalendarView.aspx/GetCalendarMeetings`, then `/Meeting.aspx?Agenda=Merged` for items
- **Date handling:** Parses `/Date(timestamp)/` format (millisecond timestamps)
- **Item extraction:** `.AgendaItemContainer` elements with numeric IDs from CSS classes
- **Matter tracking:** Extracts case numbers via regex patterns (BOA-0039-2025, RES-2025-123, etc.)
- **Matter type inference:** Derives type from prefix (BOA→Board of Adjustment, ORD→Ordinance, etc.)
- **Per-item attachments:** `FileStream.ashx?DocumentId=` links
- **City examples:** Raleigh NC

**8. CivicClerk (362 lines)**
- **OData REST API** at `{slug}.api.civicclerk.com`
- **OData pagination:** Follows `@odata.nextLink` for multi-page results
- **Item extraction:** Fetches structured items from `/v1/Meetings/{agendaId}`, flattens section hierarchy (recursive `isSection` traversal)
- **Bill parsing:** Extracts Board Bill, Resolution, Ordinance numbers from HTML titles
- **Attachments:** Per-item `attachmentsList` with `pdfVersionFullPath`/`mediaFullPath` URLs
- **CORS headers:** Requires Origin/Referer headers matching portal domain
- **City examples:** St. Louis MO, Montpelier VT, Burlington VT

**9. Municode (503 lines)**
- **Dual mode:** Subdomain API (`{slug}.municodemeetings.com/api/v1/`) or PublishPage HTML (`meetings.municode.com/PublishPage/`)
- **Mode detection:** Hyphens in slug = subdomain API; short alphanumeric = PublishPage city code
- **Config dependency:** `data/municode_sites.json` for ppid overrides
- **Parser:** `municode_parser.py` extracts items from `<section class="agenda-section">` structure
- **HTML agenda:** Fetches from `/adaHtmlDocument/index?cc={code}&me={guid}&ip=True`
- **City code discovery:** Auto-discovers from API response URLs (`cc=` param, blob paths)
- **Participation:** Extracted from HTML (contact info, zoom links)
- **City examples:** Columbus GA, Tomball TX, Los Gatos CA, Cedar Park TX

**10. OnBase (464 lines)**
- **Config-based:** Sites configured in `data/onbase_sites.json`, keyed by banana
- **Platform:** Hyland OnBase Agenda Online (direct instances, not via Granicus)
- **Deployments:** Hyland Cloud (`{city}.hylandcloud.com`), self-hosted, multiple sites per city
- **Meeting listing:** JSON extraction from inline page data or static HTML links
- **Item extraction:** Reuses `granicus_parser.parse_agendaonline_html()` (shared format)
- **Attachments:** Fetched from item detail pages, DownloadFile→ViewDocument URL translation
- **Multi-URL strategy:** Tries `Documents/ViewAgenda` and `Meetings/ViewMeetingAgenda`, keeps best result
- **City examples:** San Diego CA, Tucson AZ, Tampa FL, Durham NC

**11. CivicPlus (580 lines)**
- **Domain discovery:** Tries `{slug}.civicplus.com`, `{slug}.gov`, `{slug}.org` variants
- **HTML scraping:** AgendaCenter pages → ViewFile/Agenda links or meeting detail pages
- **Four-tier item extraction** (HTML → monolithic packet detection → PDF → monolithic):
  1. **HTML agenda:** Fetches `?html=true` version of ViewFile URLs, parses structured `div.item.level{1,2,3}` hierarchy via `civicplus_parser.py`. Level 1 = section headers, level 2+ = substantive items. Generic titles ("Consent A") replaced with description text.
  2. **Monolithic packet detection:** After HTML parsing, checks if items are mostly attachment-less and one "item" is actually a full "Agenda Packet" PDF. If detected (≥70% of substantive items lack attachments), extracts the packet PDF URL and runs the agenda chunker on it for TOC-based body_text from embedded memos. Falls back to HTML items if chunking doesn't produce body_text.
  3. **PDF fallback:** Downloads agenda PDF and extracts items via `agenda_chunker.py` (section detection, numbering heuristics, CivicPlus-specific patterns)
  4. **Monolithic fallback:** Keeps `packet_url` if all parsing paths fail
- **Parser:** `civicplus_parser.py` extracts items with nested section tracking (e.g., "REGULAR BUSINESS > RESOLUTION(S)")
- **Meeting ID:** Extracts from URL `id=` param or generates MD5 hash of normalized URL
- **Date extraction:** From URL pattern (`_MMDDYYYY-ID`) or page text
- **Deduplication:** By date (keeps last uploaded, typically packet over agenda)
- **City examples:** Ardmore OK, Citrus Heights CA, various mid-size cities

**12. CivicEngage (431 lines)**
- **CivicPlus Archive Center:** Document archive system on custom `.gov` domains (not a meeting calendar)
- **Two listing modes** (auto-detected with fallback):
  - **Search mode** (`lngArchiveMasterID`): Server-side date filtering via `Archive.aspx?ysnExecuteSearch=1&lngArchiveMasterID=...`
  - **AMID mode**: Returns all documents in category, client-side date filtering
- **Document links:** ADID links (`Archive.aspx?ADID=8497`) resolve directly to PDFs
- **PDF chunking:** Downloads agenda/packet PDFs and runs chunker for item extraction
- **City examples:** Wichita KS, and other CivicPlus `.gov` domains

**13. CivicWeb (360 lines)**
- **Platform:** eSCRIBE's older CivicWeb Portal (Drupal-based, ASP.NET backend)
- **HTML scraping:** `MeetingTypeList.aspx` lists all bodies and recent meetings in static HTML
- **Meeting details:** `MeetingInformation.aspx?Id={mid}` with embedded packet PDF viewer
- **PDF extraction:** Packet PDFs with TOC bookmarks are chunked for structured items
- **Date formats:** Handles both "18 Mar 2026" (Sonoma) and "Mar 31 2026" (Calistoga)
- **Concurrency:** Semaphore-bounded (5 meetings, 3 PDFs) for parallel enrichment
- **City examples:** Sonoma CA, Calistoga CA

**14. ProudCity (930 lines)**
- **Platform:** WordPress-based white-label gov CMS (hundreds of small municipalities)
- **WP REST API:** `/wp-json/wp/v2/meetings` custom post type with pagination
- **HTML fallback:** Scrapes `/city-council-meetings/`, `/council-meetings/`, `/meetings/` when REST API unavailable
- **Date caveat:** WP `date` field is publication date, not meeting date. Meeting date extracted from title (e.g. "City Council Meeting: April 13, 2026")
- **Tab structure:** `#tab-agenda`, `#tab-agenda-packet`, `#tab-minutes`, `#tab-video` for document extraction
- **Four-step item extraction:** HTML agenda-packet tab -> agenda tab -> chunker on agenda PDF -> chunker on packet PDF
- **Body taxonomy:** Extracts committee name from WP taxonomy classes (`meeting-taxonomy-*`)
- **Domain discovery:** Probes `{slug}.gov`, `www.{slug}.gov`, `{slug}.org`, etc.
- **Config:** Optional `data/proudcity_sites.json` for domain overrides
- **City examples:** Belvedere CA, Colma CA, Fairfax CA

**15. Vision Internet (451 lines)**
- **Platform:** Granicus govAccess CMS (formerly Vision Internet)
- **HTML scraping:** Calendar widgets render meetings in HTML tables at configured page paths
- **Multi-body:** Each body (Planning Commission, Town Council) has its own calendar page URL
- **Table structure:** `.event_title`, `.event_datetime`, `.event_agenda`, `.event_minutes` cells
- **Multiple agendas:** Main packet + supplementals stored as `supplemental_docs` metadata
- **Pagination:** `/-npage-{n}` suffix, sorted newest-first
- **PDF chunking:** Packet PDFs chunked for structured item extraction
- **Config dependency:** `data/visioninternet_sites.json` for base URL and calendar paths
- **City examples:** Portola Valley CA

**16. WP Events (572 lines)**
- **Platform:** Bespoke WordPress sites with custom `events` post type and media attachments
- **WP REST API:** Paginated events CPT, media attachments parented to event post
- **Filename-based classification:** Media filenames mapped to structured agenda items:
  - `Agenda-Item-Number-{N}-{desc}.pdf` -> agenda item with sequence N
  - `Resolution_Number_{N}_{year}_{desc}.pdf` -> resolution attachment
  - `FINAL[_-].*Council[_-].*Agenda` -> main agenda PDF
  - `(?:Approved|Draft)[_-].*Minutes` -> minutes PDF
  - `Public[_-]Comment` / `_Redacted` -> public comment
- **Config:** Optional `data/wp_events_sites.json` for domain and CPT slug overrides
- **City examples:** Sebastopol CA

**17. Berkeley (295 lines) - Custom**
- **Custom Drupal CMS** at `berkeleyca.gov`
- **HTML scraping:** Table rows with `<time>` tags for dates
- **Item extraction:** `<strong>1.</strong><a href="...pdf">Title</a>` pattern
- **Sponsors:** From `From:` lines following items
- **Recommendations:** Extracted from `Recommendation:` lines
- **Participation:** Zoom URL, phone number, email from intro paragraphs
- **Attachments:** PDF links from item anchors

**18. Ross (420 lines) - Custom**
- **Platform:** AHA Consulting's FastTrack platform (Drupal 7) for Town of Ross CA
- **HTML scraping:** `/meetings` page with 8-column table (Date, Meeting, Agendas, Minutes, Staff Reports, Audio, Video, Details)
- **Item extraction:** Detail pages (`/towncouncil/page/...`) have structured staff report attachments labeled "Item {N}. {title}"
- **Body mapping:** URL prefix -> body name (`/towncouncil/` -> Town Council, `/advisorydesignreview/` -> Advisory Design Review Group, etc.)
- **Fallback chain:** Structured items from detail page -> chunker on agenda PDF
- **Agenda selection:** Prefers non-closed-session agenda when multiple available

---

### Monolithic Adapters (1 adapter)

These adapters fetch **PDF packet URLs only** (no structured items). Meetings are processed with comprehensive LLM summarization.

**19. Menlo Park (182 lines) - Custom**
- **Custom scraper** for Menlo Park CA's table-based website
- **PDF item extraction:** Downloads agenda PDF, extracts text via `PdfExtractor`, parses items via `parse_menlopark_pdf_agenda()`
- **Item format:** Letter-based sections (H., I., J., K.) with numbered items (H1., J1.)
- **Attachments:** Hyperlinked PDFs within agenda PDF (Staff Reports, Presentations)
- **Note:** While items are extracted from PDFs, the source document is a PDF rather than structured HTML/API data

---

## HTML Parsing Architecture

**Separation of Concerns:** Adapters fetch data, Parsers extract structure.

### Parser Inventory

| Parser | Adapter | Patterns Handled |
|--------|---------|-----------------|
| `legistar_parser.py` | Legistar | `rgMasterTable` RadGrid with column mapping; `LegislationDetail.aspx` attachments |
| `primegov_parser.py` | PrimeGov | LA pattern (meeting-item + forcepopulate), Palo Alto (agenda-item), Boulder (table data-itemid) |
| `granicus_parser.py` | Granicus, OnBase | ViewPublisher listing, AgendaOnline accessible/table views, AgendaViewer with MetaViewer, S3 grid HTML (Bozeman/Carson City style) |
| `municode_parser.py` | Municode | `agenda-section` → `agenda-items` → `agenda_item_attachments` |
| `novusagenda_parser.py` | NovusAgenda | CoverSheet.aspx links, exploratory multi-pattern detection |
| `civicplus_parser.py` | CivicPlus | `div.item.level{1,2,3}` hierarchy from `?html=true` agendas; section/sub-section nesting; generic title replacement |
| `router.py` + 3 chunker engines | All PDF-chunking adapters (Granicus, CivicPlus, CivicClerk, CivicWeb, ProudCity, BoardBook, Destiny, eScribe, Municode, Vision Internet, WP Events, Menlo Park, Ross, ...) | PDF chunking cascade — see **PDF Chunking Cascade** section below. Three engines (v1 URL/hardcoded-TOC, v2 anchor-first/relative-TOC, text numbered-heading splitting) behind declarative ladders with a per-attempt audit trail |

**Adapters without dedicated parsers** (inline parsing): IQM2, eScribe, CivicClerk, CivicEngage, Berkeley, Chicago, Menlo Park, Ross, ProudCity, Vision Internet, WP Events.

**Why separate parsers?**
- **Vendor updates:** HTML changes → update parser only, adapter unchanged
- **Testing:** Can test parsing logic independently
- **Reusability:** Granicus parser reused by OnBase; chunking cascade shared by all PDF-dependent adapters
- **Clarity:** Adapter focuses on HTTP/retry, parser focuses on DOM traversal

---

## PDF Chunking Cascade (parsers/router.py)

Routing policy for PDF item extraction is **data, not control flow**. A *rung*
is one engine invocation written `"engine:method"`; a *ladder* is an ordered
rung list tried until one yields items. Defined in `router.LADDERS`:

| Ladder | Rungs | Used by |
|--------|-------|---------|
| `agenda` | `v2:url → v1:url → v2:auto → text:auto` | `_chunk_agenda_then_packet` agenda branch |
| `packet` | `v2:toc → text:auto` | packet branch, `force_method="toc"` callers |
| `url_legacy` | `v1:url → v2:auto → text:auto` | `force_method="url"` callers (Granicus) |
| `auto` | `v2:auto → v1:auto → text:auto` | unforced `_parse_packet_pdf` callers |
| `v2_url_only` | `v2:url` | `force_method="v2_url"` |

**Engines:**
- **v1** (`agenda_chunker.py`) — URL-anchored extraction + four hardcoded TOC
  variants (hierarchical, flat, deep_hierarchical, document_bundle). Kept alive
  by measured wins (Chicago, King County WA, Menlo Park); on an atrophy clock —
  delete rungs when their prod win rate hits zero.
- **v2** (`agenda_chunker_v2.py`) — anchor-first with relative TOC level
  detection; internal auto-dispatch (toc/url/pageref/url_then_toc).
- **text** (`text_chunker.py`) — numbered-heading splitting for short flat-text
  agendas. Terminal rung everywhere; self-limits (≤20 pages, 3-80 deduped
  headings, titles must contain words) so it only converts no_items failures.

**ChunkResult / audit trail:** every `chunk_pdf()` call returns the winning
items plus per-rung attempts (item counts, durations, classified failure
reasons: `download_failed | too_small | open_failed | encrypted |
no_text_layer | no_items | engine_error`). Audits ride the meeting dict into
`queue.processing_metadata` — chunker error rates are SQL queries, not
estimates.

**Morphology telemetry:** `pdf_profile.py` measures explicit signals in one
bounded pass (TOC shape, external vs internal links, item-number heading
lines, text layer); `morphology.py` classifies the profile into a named shape
(`linked_agenda`, `anchored_packet`, `toc_packet`, `toc_agenda`,
`flat_text_agenda`, `scanned`, `monolith`) with **all detection thresholds in
one table**. Classification + agreement-with-winner land in the audit (shadow
mode = passive confusion matrix in prod).

**Quality signals (`quality.py`), by failure layer:** every winning chunk's
audit carries `quality: {garbage_titles, repaired_titles, seg_smell}`.
Garbage-pattern titles (bookmark labels, filenames) are an *extraction-layer*
problem and get repaired from trustworthy sources only (filename cleaning;
SUBJECT:/RE: harvest from the item's own page). `seg_smell` flags
*chunking-layer* problems — extracted count diverging from the document's own
numbered headings (known false-positive mode: numbered tables in packet
attachments). Ground truth in `tests/chunker/truth/` is the correctness
backstop with recall/precision ratchets.

**Routing hints (reorder-only, cascade always intact):**
1. *Sticky per-city*: last winning rung per (vendor, slug, ladder), seeded at
   Fetcher startup from persisted audits, updated in-process on wins.
   Steady-state chunking = one attempt.
2. *Classifier suggestion*: fills the hint slot for cities with no history
   (`ENGAGIC_CHUNKER_CLASSIFIER_HINTS`, default on).

**Legacy shims:** `_parse_packet_pdf(url, vendor_id, force_method)` and
`_parse_pdf_bytes(bytes, vendor_id, force_method)` map force_method strings to
ladders and return bare item lists — adapters need no changes. New code should
use `_chunk_packet_pdf(...) -> ChunkResult` for audit access.

**Regression suite:** `tests/chunker/` — 52 sha256-pinned prod PDFs with
committed goldens pinning winning rung, morphology, item structure, and
failure reasons per fixture. Change a chunker → `update_goldens.py` → read the
diff. See `tests/chunker/README.md`.

---

## Shared Utilities

### Attachment Version Filtering (vendors/utils/attachments.py)

**Version-based deduplication:** Filters to include at most one version of versioned documents.

```python
def filter_version_attachments(attachments, version_patterns=None, name_key='name'):
    """
    Filter attachments to include at most one version of versioned documents.
    Prefers higher version numbers (Ver2 > Ver1).
    Default patterns: ['leg ver', 'legislative version'] (Legistar)
    """

def normalize_attachment_metadata(attachment, vendor):
    """Normalize attachment fields across vendors to consistent {name, url, metadata} format."""
```

### Council Member Extractor (vendors/extractors/council_member_extractor.py)

**Sponsor extraction** from adapter output with normalization and deduplication.

```python
class CouncilMemberExtractor:
    """Extract council member/sponsor data from vendor output."""

    @staticmethod
    def extract_sponsors_from_item(item) -> List[str]:
        """Handles: item["sponsors"] (list), item["sponsor"] (str), item["metadata"]["sponsors"]."""

    @staticmethod
    def extract_all_sponsors_from_meeting(meeting) -> Dict[str, List[str]]:
        """Returns mapping of matter_id -> sponsor names for linking."""

    @staticmethod
    def normalize_sponsors(sponsors) -> List[str]:
        """Normalize via database.id_generation.normalize_sponsor_name()."""

async def process_meeting_sponsors(meeting, banana, council_member_repo, meeting_date=None) -> int:
    """Convenience: extract sponsors, create/update council members, create sponsorship links."""
```

---

## Pydantic Validation (schemas.py)

**Runtime validation** at adapter boundaries before database storage.

```python
class MeetingSchema(BaseModel):
    vendor_id: str          # Required, non-empty
    title: str              # Required, non-empty
    start: Optional[str]    # ISO format string, or None when undated
    location: Optional[str]
    agenda_url: Optional[str]
    packet_url: Optional[str]
    items: Optional[List[AgendaItemSchema]]
    participation: Optional[Dict]
    meeting_status: Optional[str]
    vendor_body_id: Optional[str]   # Legistar committee/body ID
    metadata: Optional[Dict]

class AgendaItemSchema(BaseModel):
    vendor_item_id: Optional[str]   # Falls back to sequence
    title: str                      # Required, non-empty
    sequence: int                   # Coerced from string if needed
    attachments: List[AttachmentSchema] = []
    matter_id: Optional[str]
    matter_file: Optional[str]
    matter_type: Optional[str]
    agenda_number: Optional[str]
    sponsors: Optional[List[str]]
    votes: Optional[List[Dict]]
    metadata: Optional[Dict]

class AttachmentSchema(BaseModel):
    name: str
    url: str                        # Required, non-empty
    type: str                       # pdf, doc, spreadsheet, unknown
    history_id: Optional[str]       # PrimeGov-specific
```

---

## Rate Limiting Strategy

**Delay-based rate limiting** prevents overwhelming civic tech platforms with vendor-specific delays between requests.

```python
# vendors/rate_limiter_async.py
class AsyncRateLimiter:
    delays = {
        "primegov": 3.0,
        "granicus": 4.0,
        "civicclerk": 3.0,
        "legistar": 3.0,
        "civicplus": 8.0,    # Aggressive blocking, needs longer delays
        "novusagenda": 4.0,
        "iqm2": 3.0,
        "escribe": 3.0,
        "unknown": 5.0,      # Fallback (municode, berkeley, chicago, menlopark, onbase)
    }
```

**Features:**
- Async-safe with `asyncio.Lock()`
- Per-vendor delay tracking
- CivicPlus gets extra random jitter (0-2s) to avoid pattern detection
- All other vendors get 0-1s random jitter
- Non-blocking delays via `asyncio.sleep()`

---

## Session Management (session_manager_async.py)

**Centralized HTTP pooling** using aiohttp for all vendor adapters.

- **One session per vendor** (not per city) with connection reuse
- **Connection limits:** 20 total, 5 per host, DNS cache 5 min
- **Browser-like headers** to avoid bot detection
- **Configurable timeout:** 30s default (total), 10s connect
- **Lazy creation:** Sessions created on first use
- **Stats:** `get_stats()` returns active sessions and connection counts

---

## Vendor Factory (factory.py)

**Adapter dispatcher:** Maps vendor name → async adapter class.

```python
VENDOR_ADAPTERS = {
    "granicus": AsyncGranicusAdapter,
    "iqm2": AsyncIQM2Adapter,
    "legistar": AsyncLegistarAdapter,
    "novusagenda": AsyncNovusAgendaAdapter,
    "onbase": AsyncOnBaseAdapter,
    "primegov": AsyncPrimeGovAdapter,
    "civicclerk": AsyncCivicClerkAdapter,
    "civicplus": AsyncCivicPlusAdapter,
    "civicengage": AsyncCivicEngageAdapter,
    "civicweb": AsyncCivicWebAdapter,
    "escribe": AsyncEscribeAdapter,
    "municode": AsyncMunicodeAdapter,
    "proudcity": AsyncProudCityAdapter,
    "wp_events": AsyncWPEventsAdapter,
    "visioninternet": AsyncVisionInternetAdapter,
    "berkeley": AsyncBerkeleyAdapter,
    "chicago": AsyncChicagoAdapter,
    "menlopark": AsyncMenloParkAdapter,
    "ross": AsyncRossAdapter,
}

def get_async_adapter(vendor: str, city_slug: str, metrics: Optional[MetricsCollector] = None, **kwargs):
    """Get async adapter instance. Raises VendorError if unsupported.
    Passes api_token for Legistar if provided in kwargs."""
```

---

## URL Domain Validation (validator.py)

**Domain validation** prevents data corruption by verifying URLs match vendor configuration.

- Validates `packet_url`, `agenda_url`, and attachment URLs against expected vendor domains
- Supports domain patterns per vendor (including CDN domains like S3, CloudFront)
- **Return actions:** `store` (valid), `warn` (suspicious but allowed), `reject` (domain mismatch)
- **Note:** Currently used in tests only; not integrated into production pipeline

Vendors with configured domains: primegov, granicus, legistar, civicclerk, novusagenda, civicplus, civicengage, civicweb, iqm2, municode, escribe, proudcity, visioninternet, wp_events, menlopark, berkeley, chicago, ross.

---

## Configuration Dependencies

Several adapters require static configuration files:

| Adapter | Config File | Contents |
|---------|------------|----------|
| Granicus | `data/granicus_view_ids.json` | Maps base URLs to `view_id` integers |
| OnBase | `data/onbase_sites.json` | Maps banana to list of site URL paths |
| Municode | `data/municode_sites.json` | Per-city overrides (ppid, etc.) |
| Vision Internet | `data/visioninternet_sites.json` | Base URL + calendar paths per body |
| ProudCity | `data/proudcity_sites.json` | Optional domain overrides |
| WP Events | `data/wp_events_sites.json` | Optional domain + CPT slug overrides |

Granicus, OnBase, Municode, and Vision Internet fail fast in `__init__` if their config is missing. ProudCity and WP Events configs are optional (domain auto-discovery is the default).

---

## Adding a New Vendor Adapter

### 1. Create Async Adapter Class

```python
# vendors/adapters/newvendor_adapter_async.py
from vendors.adapters.base_adapter_async import AsyncBaseAdapter, logger
from pipeline.protocols import MetricsCollector

class AsyncNewVendorAdapter(AsyncBaseAdapter):
    def __init__(self, city_slug: str, metrics: Optional[MetricsCollector] = None):
        super().__init__(city_slug, vendor="newvendor", metrics=metrics)
        self.base_url = f"https://{self.slug}.newvendor.com"

    async def _fetch_meetings_impl(self, days_back: int = 7, days_forward: int = 14) -> List[Dict[str, Any]]:
        # 1. Fetch meeting list (API or HTML)
        # 2. Filter by date range
        # 3. Fetch details concurrently
        # 4. Return list of meeting dicts with vendor_id, title, start, items/packet_url
        ...
```

### 2. Register in Factory

```python
# vendors/factory.py
from vendors.adapters.newvendor_adapter_async import AsyncNewVendorAdapter

VENDOR_ADAPTERS = {
    ...
    "newvendor": AsyncNewVendorAdapter,
}
```

### 3. Add Rate Limiting (Optional)

Add vendor-specific delay to `rate_limiter_async.py` if needed (otherwise defaults to 5.0s "unknown").

### 4. Test

```bash
python -m pipeline.conductor sync-city examplecityCA --force
```

---

## Debugging Adapters

### Common Issues

**1. No meetings returned**
- Check date range (some vendors only show future meetings)
- Verify URL construction (city_slug might be case-sensitive)
- Check HTTP response via debug logging

**2. Parsing failures**
- HTML structure changed (vendor updated their site)
- BeautifulSoup selectors too specific
- Missing null checks on DOM elements

**3. Rate limiting (429 errors)**
- Increase delay in `rate_limiter_async.py`
- CivicPlus is most aggressive; uses 8s + 0-2s jitter

**4. SSL errors**
- Granicus has known SSL cert issues on S3 redirects (SSL disabled in base adapter for granicus domains)

---

**See Also:**
- [pipeline/README.md](../pipeline/README.md) - How adapters integrate with processing pipeline
- [database/README.md](../database/README.md) - How meeting data is stored

**Last Updated:** 2026-06-11 (PDF chunking cascade: declarative ladders + audit trail in router.py, morphology profiler/classifier, flat-text engine, sticky per-city routing hints, corpus regression suite in tests/chunker/. Prior: 2026-03-25 added CivicEngage, CivicWeb, ProudCity, Vision Internet, WP Events, Ross — see CHANGELOG)
