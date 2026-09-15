# Seeded vendor slug discovery

`scripts/discover_vendor_slugs.py` is the successor to the exploratory
`scripts/probe_vendors.py`. The old script still exists, but its raw state
substring and hard-coded year checks are not sufficient for registration.

The seed at `munis/discovery-seed-2026-09.csv` contains the supplied 3,438
city/state entries, with known registry bananas retained for explicit aliases.
The runner skips populated, inactive, and non-city registry records. It can
insert missing cities and repair unpopulated active records while preserving
existing bananas.

## Independent requirements for every addition

1. **Existence:** a completed successful HTTP response containing real platform
   data. A guessed hostname, a 200 shell page, or an empty calendar is not proof.
2. **Identity:** positive evidence for the requested city **and** state, not a
   plausible slug or a state abbreviation found somewhere in the HTML.
3. **Usability:** a recent municipal agenda works with an existing adapter.
4. **Registry uniqueness:** no conflicting banana, normalized city/state, or
   tenant assignment. Existing populated records are protected. Repairs must
   still match the original registry snapshot immediately before the write.

Current automated verification paths:

- **CivicClerk:** a recent municipal event has exact matching structured
  `eventLocation.city` and `.state`, an agenda ID, a published agenda/packet,
  and the current adapter parses agenda items with attachments. School,
  county, library, and other excluded body labels cannot satisfy this test.
- **CivicPlus AgendaCenter:** the title identifies the exact city/state; visible
  page text contains its matching postal address; the current adapter's listing
  and meeting parsers produce a recent municipal meeting; its linked PDF is
  downloaded successfully and contains readable city and municipal-body text.
  This proves meeting/PDF extraction, not item-level PDF segmentation.

Other vendor responses are retained as leads, not automatically registered.
They still need city/state and adapter evidence. The same is true for calendars
without usable agendas, old portals, TLS failures, timeouts, and empty results.

## Commands

Run from the repository root using the project's Python environment:

```bash
.venv/bin/python -m scripts.discover_vendor_slugs probe \
  --seed munis/discovery-seed-2026-09.csv \
  --out data/slug-discovery-2026-09 \
  --vendors civicclerk --patterns 1

.venv/bin/python -m scripts.discover_vendor_slugs validate \
  --out data/slug-discovery-2026-09

.venv/bin/python -m scripts.discover_vendor_slugs apply \
  --out data/slug-discovery-2026-09

# Apply the verified insert/repair proposals:
.venv/bin/python -m scripts.discover_vendor_slugs apply \
  --out data/slug-discovery-2026-09 --apply
```

`apply` defaults to a dry run. Its mutation flag is an operational safeguard,
not a requirement to ask for permission again when registration is authorized.
Database configuration comes from the standard `Config` object.

The probe ranks slug templates by observations in active city records with
stored meetings. For example, `cityst` dominates CivicClerk, `st-city` is common
in CivicPlus, and `pub-city` dominates eScribe. Stored meetings are only training
evidence for a naming pattern; they do not prove that the source is correctly
attributed. Every new candidate must independently pass the checks above.

`--patterns 2` expands the same saved run to the two highest-ranked templates
per vendor. `--vendors` accepts comma-separated names. The default providers
are CivicClerk, Legistar, Granicus, CivicPlus, PrimeGov, eScribe, IQM2, Municode,
NovusAgenda, and CivicWeb. Numeric tenant IDs and arbitrary custom-domain
configurations are not guessed as if they were city-name slugs.

`--retry-failures --timeout 15` retries transient failures with a longer timeout;
DNS nonexistence and certificate validation failures are not bypassed.
Concurrency and per-host limits bound the discovery requests. Validation uses
existing adapter rate limiting for CivicClerk and a bounded PDF fetch pool for
CivicPlus. Neither validation path invokes an LLM or enqueues ingestion.

Use one probe and one validator at a time **per output directory**. Rerun
validation after the probe finishes to consume any leads that arrived while
validation was running. Use separate output directories for concurrent vendor
sweeps. The original registry snapshot is retained across resumes.

## Evidence and reruns

Each output directory contains:

- `patterns.json`: learned templates, frequencies, and examples.
- `registry.json`: immutable original registry snapshot for repair preconditions.
- `targets.json`: the eligible targets from the most recent probe invocation.
- `attempts.jsonl`: URL, HTTP/error outcome, completeness, timestamp, and vendor.
- `responses/`: saved successful candidate responses.
- `leads.jsonl`: candidate source discoveries; not an approval list.
- `validation.jsonl`: identity and parser evidence, or the reason for holding.
- `proposed.jsonl`: dry-run insert/repair proposals.
- `applied.jsonl`: committed changes with prior values for repairs.

Attempts and validation records are resumable. Successful validation is not a
promise of perpetual endpoint health. The 120-day lookback and 30-day forward
window are computed from the runtime clock; insertion rejects stale evidence.
The broader city list also contains ambiguous township and submunicipal names;
those need jurisdiction resolution when the evidence cannot establish the
requested city/state identity.

Validation tests:

```bash
.venv/bin/pytest -q tests/test_vendor_slug_discovery.py
.venv/bin/ruff check scripts/discover_vendor_slugs.py tests/test_vendor_slug_discovery.py
```

## CivicClerk and CivicPlus source selection

A tenant existing on both platforms does not establish equivalent content.
Combine both vendors' validation records before applying a sweep. When both
are discovered, `apply` requires a reviewed `source-decisions.json` entry:

```json
{
  "exampleST": {
    "action": "select",
    "primary": {"vendor": "civicclerk", "slug": "examplest"},
    "relationship": "same_observed_meetings",
    "reason": "Compared published meetings and documents across both listings."
  }
}
```

Prefer CivicClerk when coverage is equivalent, and CivicPlus when Clerk is a
strict subset. Paginate Clerk's API before comparing: its first response may
contain only 15 events regardless of the requested page size. Compare published
agendas, not future draft placeholders, and inspect titles as well as dates.
An observed 120-day/30-day comparison does not prove archival equivalence.

For independently verified, distinct sets of boards, select a primary plus
`extras` (a list of vendor/slug objects) with relationship
`disjoint_verified_bodies`. These are written into the existing `extra_vendors`
field. CivicPlus committee-only secondary sources may be checked using
`validate_civicplus(..., allow_committees=True)`; city/state branding, postal
address, and an actual readable agenda remain mandatory. Cancelled meeting
notices cannot satisfy agenda verification.

Partially overlapping feeds need scoped ingestion or further review. Current
meeting IDs include native vendor IDs, so enabling both overlapping feeds can
create duplicates. Record `{"action":"hold","reason":"..."}` for those
cases. Unverified preferred sources are held rather than silently substituting
a known subset. `held-source-decisions.json` records deferred decisions.
Latest validation records supersede older successes, and tenant collision
checks cover primary and extra sources. Neither selecting nor applying a
source launches a bulk sync.
