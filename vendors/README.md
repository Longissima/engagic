# vendors

Adapters turn a publishing platform's pages and APIs into raw meeting dicts. They adapt; they never decide. Ids, filtering, matter identity and storage all belong to `pipeline/`.

## Contract

`AsyncBaseAdapter.fetch_meetings(days_back, days_forward, *, start, end, originals_only)` returns a `FetchResult`. A successful empty result ("0 meetings") is distinct from a failed fetch. A bot wall (a WAF answering 200 with a script shell) must raise, not parse as empty.

A meeting dict carries:
- the vendor's own `vendor_id` (never a canonical id; `database/id_generation.py` mints those);
- `title`, `start`, `agenda_url`, `packet_url` (a string or a list), `minutes_url`, `participation`, `meeting_status`;
- `items`.

Each item carries:
- `vendor_item_id` or `sequence`, `title`, `agenda_number`, `matter_file` / `matter_id` when the vendor exposes them;
- `body_text` and `attachments` (url, name, type, optional `pages` span).

`vendors/schemas.py` validates this at the boundary. A malformed attachment is dropped with a warning rather than failing its item. Fields the funnel normalizes itself (`body_source`, chunker metadata) ride as undeclared extras, so a bad value cannot invalidate a meeting.

Two side channels ride along with the meetings:
- **Chunk and HTML audits.** Every chunker or parser run records its rungs, failure reasons and morphology. The audits land in `queue.processing_metadata`, so chunker health is a SQL query.
- **Source pages.** When items are scraped from an HTML or JSON page, the adapter calls `self._source_page(response, items)`. That keeps the page bytes for the funnel to archive and sets a URL-only `body_source` on the items.

## Shared machinery (`base_adapter_async.py` and siblings)

- **HTTP**: one pooled aiohttp session per vendor (`session_manager_async.py`), retries with `Retry-After`, and `VendorHTTPError` carrying vendor, URL and city. Akamai-blocked vendors (Municode, Vision Internet) route through `config.RESIDENTIAL_PROXY`.
- **Pacing** (`rate_limiter_async.py`): each vendor has a minimum interval per slot and a slot count. Multi-tenant SaaS gets several slots. Vendors where each city has its own domain (CivicPlus, selfhosted, dockets) are paced per host, because politeness is owed to each server. City-direct scrapers stay at one slot.
- **Fan-out** goes through `_bounded_gather`: fixed workers, never one task per input.
- **PDF items**: `_chunk_agenda_then_packet` chunks the agenda PDF first (short and hyperlinked, so items with attachments win) and falls back to the packet. Every path calls `pipeline.ground_truth.produce_ground_truth`, which archives the bytes, chunks in a guarded subprocess and persists the text.
- **Sub-attachments**: `_resolve_sub_attachments` and `_resolve_sharepoint_urls` follow cover sheets and SharePoint wrappers to the real document.
- **Ranking**: `utils/documents.py` picks one document per meeting slot (a PDF over the HTML viewer that wraps it). `utils/attachments.py` collapses version duplicates ("Leg Ver1/Ver2", "Revised").
- **Domain check**: `validator.py` rejects packet URLs that do not match the configured vendor and slug, which catches a slug pointing at another government.

## Chunker cascade (`adapters/parsers/`)

Item extraction from PDFs is routing data, not control flow; see [parsers/README.md](adapters/parsers/README.md). `router.LADDERS` names ordered rungs (`engine:method`) tried until one yields items, e.g. agenda `v2:url -> v1:url -> v2:auto -> text:auto` and packet `v2:toc -> text:auto`. Sticky per-city hints and a morphology classifier reorder rungs without removing any. `tests/chunker/` pins real production PDFs with goldens; any chunker change goes through it.

## Adapters

`factory.py` maps the `jurisdictions.vendor` value to a class. A vendor value with no entry there (a platform not yet built) is simply not synced.

| Vendor | Platform | Notes |
|---|---|---|
| legistar | Granicus Legistar Web API + HTML | Richest source: matters, votes, sponsors; pulse uses its delta query |
| granicus | Granicus ViewPublisher, AgendaOnline | HTML agendas, packet PDFs; chunk sites double as URL probes |
| agendaonline | AgendaOnline on custom domains | config in `data/agendaonline_sites.json` |
| primegov, iqm2, civicclerk, escribe, civicweb, novusagenda, municode, onbase, destiny | vendor SaaS | CivicClerk signed URLs are refreshed at fetch time (`pipeline/url_refresh.py`) |
| boardbook | Sparq BoardBook | school boards and special districts; slug is the org id; excluded from pulse |
| civicplus, civicengage, visioninternet, proudcity, wp_events | city CMS sites | paced as city infrastructure |
| selfhosted | agencies' own sites | one class, one registry entry per site; see [custom/README.md](adapters/custom/README.md) |
| berkeley, chicago, menlopark, ross | one city each | custom scrapers or APIs |

`dockets` and `publications` adapters are not in the factory: they serve `scripts/ingest_dockets.py` and `scripts/ingest_publications.py` for bodies that do not hold meetings.

## Adding an adapter

1. Measure the platform first: `scripts/probe_vendors.py` and `scripts/discover_vendor_slugs.py`. A behavior seen in one city is not yet a vendor behavior.
2. Subclass `AsyncBaseAdapter`, implement `_fetch_meetings_impl`, and return raw vendor ids. Prefer structured items; otherwise hand PDFs to the chunker through the base helpers rather than parsing them yourself.
3. Call `_source_page` wherever item text comes from a page.
4. Register in `factory.py`, add pacing in `rate_limiter_async.py`, and add a pulse probe in `pipeline/pulse.py` if the platform has a cheap change signal.
5. Sync a few real jurisdictions and read the stored items and audits.
