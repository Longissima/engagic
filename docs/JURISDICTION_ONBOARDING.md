# Jurisdiction onboarding

Run `uv run python scripts/db_viewer.py`, choose **7. Add jurisdiction**, then
select a type or enter a new type name. Spaces and hyphens become underscores.
The choices combine suggestions with types already stored in the database;
a new type needs no migration or menu action.

Enter the full display name, including County/District/etc. where appropriate.
An optional common abbreviation controls the new banana without changing the
display name. City Census/ZIP enrichment runs only for `city`. Other types can
have manually supplied ZIPs and service-area population; leave unknown values
blank. Enrollment is not population. A regional body can leave its parent
county blank. County creation still offers to link existing cities.

## Schema and identity

- `jurisdictions`: `banana` is the primary key; `(name, state)` is also unique.
  Required fields are banana, name, state, vendor, slug, and type. Type is text,
  not a database enum. Optional fields include `county_banana`, `population`,
  `geom`, `extra_vendors`, and `participation`.
- `zipcodes`: many-to-many ZIP associations keyed by `(banana, zipcode)`.
  These are discovery associations, not authoritative district boundaries.
- `meetings` and downstream agenda records attach to the jurisdiction through
  its banana; new organization types do not need their own meeting tables.
- Bananas use `scripts/_jurisdiction_naming.py`: name plus state, or a supplied
  vernacular abbreviation plus state. Existing bananas are frozen. Collisions
  need review, not generated suffixes or an upsert that changes another entity.
- `county_banana` references another jurisdiction. The add flow accepts only an
  existing county in the same state. It cannot represent a multi-county service
  area; leave it blank for those bodies.

## Adding a supplied list of names and links

1. Establish each full name, state, and agreed type. Deduplicate against both
   existing bananas and `(name, state)`; preserve existing identities.
2. Inspect the agenda/meeting link to identify its vendor and exact slug format.
   `vendors/factory.py` contains supported adapters; consult the adapter for
   slug requirements. A homepage URL is not necessarily a usable vendor slug.
   Some adapters also need site configuration under `data/`.
3. An unfamiliar jurisdiction type works with an existing vendor adapter.
   An unsupported website platform needs adapter/configuration work before
   ingestion can work. Adding a database row alone does not establish that.
4. Build `database.models.Jurisdiction` records using the shared naming helper.
   `await db.jurisdictions.add_city(record, geometry=optional_boundary)` is the
   existing repository insert API despite its historical city-specific name.
   It inserts population, geometry, and ZIP associations in one transaction;
   duplicates raise rather than overwrite. For a batch, validate all proposed
   entries and collisions before writing and report any unresolved entries.
5. Check the stored records and verify ingestion for the configured sources.
   New rows default to active; `last_synced_at` remains null until a successful
   sync. Do not fabricate ZIP coverage, boundaries, population, or parent links.

The interactive flow lives in `scripts/_jurisdiction_onboarding.py`; optional
city lookups live in `scripts/_jurisdiction_geography.py`. Generic new types
need no code. New type-specific enrichment belongs in a helper called from
the shared flow, rather than another copied add command.

## Correcting a source assigned to the wrong jurisdiction

When a verified source collision imported another jurisdiction's content, keep
one correct source configuration and purge the content imported under the wrong
record. Do not migrate those processed records or require the correct record to
have been ingested first; its next sync can rebuild them. Preserve jurisdiction
identity, ZIP associations, tenant coverage, and user subscriptions.

Before purging, record the wrong source and affected IDs, verify that no content
from a newly corrected source has arrived, and check for active processing.
Delete in a transaction, including dependent meeting/matter records, stale queue
and outbox work, and attribution/provenance rows without cascading foreign keys.
Keep shared document blobs. Verify that wrong-source content is gone and correct
configurations remain intact. This applies to confirmed source mistakes, not
legitimate multiple sources or shared portals.
