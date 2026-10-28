# Custom adapters

These adapters cover bodies that are not on a supported platform. Before writing a class here, check whether the body fits one of the registry-driven adapters. A registry entry costs a JSON entry; a class costs maintenance forever.

## Registry-driven (one class, many bodies)

| Adapter | Covers | Registry (in `data/`, git-ignored, lives on the VPS) | Writer |
|---|---|---|---|
| `selfhosted_adapter_async.py` (vendor `selfhosted`) | agencies, boards, regulators, utilities and transit authorities that post meetings on their own site | `selfhosted_sites.json` | the normal sync path |
| `publications_adapter_async.py` | reports, regulations, orders and findings published with no meeting around them | `publication_sources.json` | `scripts/ingest_publications.py` |
| `dockets_adapter_async.py` | state regulatory commissions' case systems: cases, filings, documents | `docket_systems.json` | `scripts/ingest_dockets.py` |

**Selfhosted** reduces every site to one model: a meeting is a dated group of document links. Nothing is located by a site-specific selector. A listing row is the smallest ancestor of a document link whose text holds exactly one date. Items on a detail page are keyed by the item number in the document's own label or URL. Agenda PDFs without item documents go through the ordinary chunker cascade. To add a body, add a registry entry, fetch it once to verify, then run `scripts/onboard_selfhosted.py --apply`. `scripts/archive_meetings.py --vendors selfhosted` backfills its history through the normal write path.

**Publications** stores a date only when the listing states a day for that document: in the link text, the file name, or a row holding that document alone. A month, a term or a page-level "updated" stamp is not a publication date.

**Dockets** walk a system's own case keys, the only enumeration those systems expose. A missing case counts as absence. Public comment letters and ZIPs are not collected.

Government sites sit behind WAFs that challenge bursts. Selfhosted is paced per host at one slot every 4 seconds, index pages are cached per adapter instance, and a challenge page raises instead of parsing as "no meetings". Once a host challenges, stop rather than retry.

## One city each

| Adapter | Source |
|---|---|
| `berkeley_adapter_async.py` | Drupal agenda table, HTML agendas with linked staff reports |
| `chicago_adapter_async.py` | City Clerk eLMS REST API; record numbers from the agenda PDF when the API's agenda groups are empty |
| `menlopark_adapter_async.py` | one page listing eight bodies; council agendas chunk by URL, commission packets by outline |
| `ross_adapter_async.py` | Drupal FastTrack meeting tables; staff reports from detail pages |

Write one of these only when a high-value body has no platform and no registry shape fits. Register it in `vendors/factory.py`, give it a pacing entry in `vendors/rate_limiter_async.py`, and keep its URL patterns and page structure in the module docstring, where the next reader will look.

New jurisdictions are created through `scripts/db_viewer.py` (the shared flow in `scripts/_jurisdiction_onboarding.py`): pick a kind from `jurisdiction_kinds`, and the banana comes from `scripts/_jurisdiction_naming.py`.
