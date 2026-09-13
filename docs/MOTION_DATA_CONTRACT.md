# Engagic motion data contract

Migration **044_item_motions** separates retained evidence, validation, and the
current public projection. Motioncount owns its downstream implementation.

## Internal evidence and replay

- `minutes_text_snapshots` stores exact extracted text, keyed by its own SHA256.
- `minutes_parse_runs` stores document and text hashes, extraction version,
  parser version/build, exact item/roster/API comparison inputs, status, errors,
  and counts. Completed inputs are deduplicated; new inputs append a new run.
- `minutes_observations` stores raw passages and offsets, parsed evidence,
  interpretation, individual validation checks, and nullable publication claims.
  Missing item matches, unknown names, contradictions, procedural motions, and
  unsupported vote language are retained. No resolved identity is required.
- `minutes_publications` points to the completed run behind each meeting's
  current public projection. Old runs and observations survive republication.

These are internal tables, not public vote API payloads. The complete source
text is retained even where the observer does not recognize vote language.
Observation extraction and validation are pure functions: format drivers share
one set of publication checks with the generic observer.

Inspect or replay without a vendor fetch, object-store request, or write:

```sh
python -m scripts.audit_minutes_votes --meeting-id MEETING --details
python -m scripts.audit_minutes_votes --run-id RUN --replay
python -m scripts.audit_minutes_votes --run-id RUN --reason unresolved_name --details
```

Replay uses the exact saved text, items and roster with the **current** parser;
it reports whether the build matches and returns confirmed motion candidates.
It does not create members, resolve durable matter IDs, or move publication.

The small offline regression bucket is six actual corpus documents under
`tests/fixtures/rollcall/`: Denver, Milwaukee, Albuquerque, Acworth, Alameda
County, and Alpharetta. JSON files pin document/text hashes, source metadata,
item and roster inputs, an API snapshot where available, and manually reviewed
expected results. It covers multiple motions, repeated result boxes,
contradictory categories, tally-only votes, compact item identifiers and
unaligned evidence. It is a regression sample, not a claim of citywide accuracy.

```sh
python -m pytest tests/test_minutes_fixtures.py -q
```

## Confirmed facts

Item alignment, explicit outcome, tally consistency, and each member's identity
are checked independently. An unknown voter does not erase the clerk's stated
outcome or prevent independently confirmed names from being published. Raw
unresolved names remain internal. Conflicting full first names never match
merely because a surname matches. Attendance alone and the word "unanimous"
do not establish individual ballots.

Only an explicit result becomes a stored `outcome`. A successful motion to
table or recommend "do not pass" still has a **passed motion** outcome; this
is not an assertion of final adoption of the underlying matter. Majority,
quorum, and charter thresholds are not encoded as facts. The frontend may show
a clearly labeled simple-majority estimate when an outcome is unavailable.
Existing legacy appearance outcomes predate this distinction and should not
be treated as confirmed minutes facts without current run lineage.

`item_motions` stores every published minutes motion, including tally-only and
outcome-only decisions. Its key is `(item_id, motion_index, source)`. It carries
`meeting_id`, `matter_id`, `motion_text`, nullable `outcome` and `tally`,
`tally_basis`, `method`, `source`, `content_sha256`, `receipt`, `parse_run_id`,
`observation_ordinal`, and `updated_at`. The latter two lineage fields reference
the exact internal observation behind a public claim.

`motion_index` is zero-based within an item in this parser publication,
including withheld motion candidates. It can have gaps and is **not** a stable
identity across source revisions or parser builds. `motion_text` is the
extracted evidence sentence and may omit the full legal wording.

`votes` retains individual records. Its uniqueness key is
`(council_member_id, matter_id, meeting_id, item_key, motion_index, source)`.
`item_key` is a durable copy of the original item ID, retained when the nullable
`item_id` foreign key is cleared. Legacy API votes use `item_key = ''`,
`item_id = NULL`, normally `motion_index = 0`. Removing an item preserves vote
and motion evidence. Votes also carry run/observation lineage and receipts.

Join votes to motions on original item identity, motion index, source, meeting,
and matter. Do not combine different motions or sources into one roll call.
API votes and minutes are stored separately, including when they disagree.
An internal comparison records agreement/disagreement only for an unambiguous
single-motion candidate; it is a debugging aid, not automatic adjudication.

A NULL tally means no numerical tally was established, not 0–0. Missing tally
categories are unknown/unreported. `tally_basis` distinguishes printed totals,
printed categories, and counted names. Recused, abstain, present and not_voting
remain distinct; an unlabeled third number is `other`. Do not invent voters
from totals or silently treat a partial named roll call as a complete tally.

`matter_appearances` remains one row per appearance. Minutes projects the last
observed motion only if that candidate is publishable; a later withheld
candidate prevents promoting an earlier result to the final scalar field.
`vote_source` marks ownership (`minutes`, `api`, or NULL for legacy/unknown).
API-owned appearance values remain separate from minutes motion records.

`council_members.vote_count` counts the public minutes-preferred ballot projection.
Any confirmed minutes motion suppresses API evidence for that item (or the
meeting/matter when the API has no item identity), including tally/outcome-only
minutes. Raw votes remain source-separated. Database statement triggers maintain
counts for vote and motion changes using `vote_is_preferred`; repository recounts
and body counts use the same policy. Migration 052 repairs existing counters.

## Minutes discovery and receipts

`minutes_documents` links meetings to content-addressed document revisions,
independently of `items.attachments`. A meeting's minutes are not automatically
evidence for every agenda item. Select the newest observed revision:

```sql
SELECT DISTINCT ON (meeting_id) * FROM minutes_documents
ORDER BY meeting_id, ingested_at DESC, content_sha256 DESC;
```

Join `document_blob` for corpus keys and extraction metadata. Compatible text
versions are exported as `corpus.store.COMPATIBLE_EXTRACT_VERSIONS`. Do not
substitute older ready minutes when the newest revision lacks compatible text.
The writer reuses saved text when extraction metadata matches; otherwise it
reads the existing corpus, without requesting the document from the city.

Receipts include original-byte `sha256`, exact extracted `text_sha256`,
`extract_version`, and half-open `start`/`end` with `unit: "unicode_codepoint"`.
These are Python Unicode indices, not UTF-8 bytes or JavaScript UTF-16 indices.
Public claims require a located source span. Historical receipts without units
also used Python indexing and may contain `-1` for an unlocated passage.

## HTTP/readers

- `GET /api/meetings/{meeting_id}/minutes`: revisions newest first, corpus keys,
  extraction metadata, `text_ready`, and `current_content_sha256`.
- `GET /api/meetings/{meeting_id}/votes`: `matters_with_votes`, each with a
  `motions` array, plus meeting-wide `motion_count`.
- `GET /api/matters/{matter_id}/votes`: top-level `motions` and a `motions`
  array in each `votes_by_meeting` entry, including motion-only meetings.
- Individual vote readers and member history preserve item identity, source,
  motion index/text, document hash, receipt and observation lineage.

Legacy scalar fields describe the last recorded motion, never a sum, and may
be NULL. `summary_scope = "last_recorded_motion"` documents this. Consumers
should prefer explicit motion groups; a scalar cannot summarize several
amendments, dispositions, or conflicting sources. Bundles use one database
snapshot. Internal raw mismatches are never returned as public votes.

## Publication and rollout

A completed parse saves its audit run and reconciles minutes-owned motions,
votes, appearance values and the current-run pointer in one transaction.
Superseded rows are retracted. A successful empty projection retracts prior
minutes facts while preserving their history. Missing text or an exception
saves an internal attempt and leaves the last successful publication in place.
Changed document or item identity prevents stale publication. API rows survive.

1. Drain/stop Engagic vote writers and prevent the minutes cron from starting
   during migration. Existing workers use old conflict targets and counters.
   Follow `run.sh`'s worker checks.
2. Apply 044 and restart Engagic services on the matching code.
3. Run `python -m scripts.parse_minutes_votes --apply --concurrency 4` against
   all linked minutes. The scheduled recent window does not repair all history.
4. Verify counters, current publication lineage, and motion-only/multi-motion
   API responses. Repeat runs reuse cached text and unchanged completed inputs.

Rollback refuses to discard populated audit history, motion evidence, detached
item identity, or collapse distinct source/item votes into the old unique key.

### Rollout verification — 2026-09-12

044 is applied; the API and MCP services are running on the matching backend.
All 8,030 linked meetings were processed successfully, then replayed from saved
text with the final identity/concurrency checks. The current projection has
17,287 motion records and 43,103 individual minutes votes. All 337,763 original
API vote records were preserved. Counter, revision and publication-lineage
checks passed. The focused backend suite passed 156 tests.

The frontend build and Cloudflare deployment dry run passed; its 23 existing
Svelte typecheck errors are unchanged. Production frontend deployment remains
pending explicit user approval after automatic approval review rejected that
external publication. No frontend deployment was performed.

### Member identity reconciliation

Minutes publication now reuses uniquely matching API-backed member IDs across
recognized titles, case/accent variants and conservative shortened-name matches.
Original member records and API ballots are retained. Internal observations
record the identity matches; ambiguous identities and duplicate attributions
are withheld. Comparisons distinguish incomplete roll calls from differing
ballot values. See [the bounded identity review](MINUTES_IDENTITY_REVIEW.md) for
rules, reviewed source examples and regression commands.

### Storage is verbatim; preference is a read

What the page says and what we show are separate decisions. Storage records the
document as printed, including where the document is wrong or contradicts itself:
a stated tally of 7 is stored as 7 even when we can only name six of the voters,
because a wrong thing recorded accurately is still evidence, while a tally quietly
rewritten to match our own incomplete name list is fabrication. API rows are
likewise retained in full. No source preference ever deletes a row.

The preference applies at read: which source a page shows, and the denormalized
counters derived from those reads. That makes the counters a cache of a read
policy, not a record -- change the policy and every stored count is stale until
`recompute_attribution_counts` sweeps.

It follows that a published tally exceeding its own published name list is our
defect, not the document's, and belongs in the name-resolution path (roster
coverage, title stripping) rather than in the tally. 1,431 of 6,036 published
named motions are short this way, 943 of them by exactly one name; none has more
names than its tally.

### Minutes preference and motion semantics

Public motion groups now prefer confirmed minutes for an item; remaining API-only
groups carry `selection_basis: api_fallback_no_confirmed_minutes`.
`get_motion_groups(include_api_comparison=True)` retains both sources for
internal inspection.

Minutes take precedence whenever a confirmed minutes motion exists, whether it
names voters, reports a tally, or records only an outcome. API ballots for that
item remain in the audit store but do not supplement the public minutes record.
When the API lacks item identity, the preference is scoped to meeting and matter.
Retraction of the last minutes motion restores the API fallback.

The SQL function `vote_is_preferred` defines this choice for member history,
topic profiles, counters, body counts, and public metrics. Python motion bundles
apply the same item/matter selection. `votes.item_key` uses `''` for missing item
identity; it is never NULL. Metrics include motion-only records and call a vote
divided only when both yes and no are recorded.

Referrers identify the source of a referral, never the recipient of “referred
to”. Publication replaces `reported_referrer`, `referrer_receipt`, and
`referrer_parse_run_id` together. A successful empty reparse clears them; source
text and prior parse runs remain available. Run normalization after publication
to refresh the derived actor tables.

Apply migrations 052 and 053 with writers stopped, then restart on this code.
Reparse saved minutes and rerun roster normalization to refresh attribution and
its derived tables. The CivicClerk repair can recover previously purged mappings
from meeting/attachment references and the failure ledger; it now verifies
replacement text and the portal alias before removing any remaining shell map.

`outcome` describes the motion itself. A successful motion to deny is passed;
`DENIED` alone describes the subject and does not establish motion failure. See
[the saved-minutes review](MINUTES_SEMANTICS_REVIEW.md).
