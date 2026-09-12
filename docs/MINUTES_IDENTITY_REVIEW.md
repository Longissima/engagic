# Minutes identity reconciliation — 2026-09-12

## Scope

A bounded review of stored minutes/API disagreements, using saved text only.
The change resolves minutes attributions to existing member IDs; it does not
merge or delete member entities, change API votes, infer outcomes, or fetch new
documents. Original parse runs remain available for inspection.

## Identity rules

`parsing/rollcall/identity.py` resolves case, accents, punctuation and recognized
office titles, then prefers a unique API-backed existing identity. Multiple
API-backed identities with the same normalized name remain ambiguous. A shorter
minutes-only name may reuse an API identity when its tokens are an exact suffix
and the API surname is unique in the city. Compatible middle-initial variants
require the same full first and last name and a unique API surname. Conflicting
full first names or middle initials are not reconciled.

Original name spellings are checked again after gazetteer matching, so a
conflicting existing identity or middle initial cannot be concealed by the
chosen canonical spelling. Such claims are retained internally. Independently
supported tallies survive identity uncertainty; counted-name tallies are
withheld when multiple names resolve to the same person.

Each publication retains `member_identity_matches` with the chosen ID, matched
existing IDs and the matching rule. Multiple spellings resolving to one person
cannot produce two ballots: those claims are withheld, including a tally based
on counting those duplicate names. Explicit outcomes remain independent.

## Reviewed cases

| Stored example | Finding | Handling |
| --- | --- | --- |
| Milwaukee: `ALD. BROWER` / `BROWER` | Same printed ballot, different entity IDs | Reuse the unique API-backed ID |
| Denver: `Diana Romero Campbell` / `Romero Campbell` | Full and shortened name | Exact, unique API surname suffix; body/item gates remain independent |
| Fort Lauderdale, `fortlauderdaleFL_1e813512` | Vice-mayor title retained in a member name; `Not Present` category omitted | Strip the recognized title and read the explicit absence category |
| DuPage County, `dupagecountyIL_56a08503` | `REMOTE:` continuation was consumed as part of `ABSENT:`, falsely attributing an absence to LaPlante | Stop name-list continuation at any labeled field; remote participation does not establish a ballot or absence |
| Milwaukee, `milwaukeeWI_ab429a4b` | Absent/abstaining members printed after a page break were missing from the extracted roll call | Matching partial ballots are not scored as a ballot-value disagreement; source text is preserved |
| Albuquerque, `albuquerqueNM_6f13622a` | Minutes explicitly print nine ayes; stored API records all say `not_voting` | Keep both sources and label the comparison `api_reports_only_not_voting` |

The Legistar adapter also uses `not_voting` as the fallback for unrecognized
vendor labels, and its normalized vote records do not retain that original
label. Therefore this last finding is a disagreement with **stored API data**,
not proof that the city itself supplied contradictory records. No API ballot
was repaired by guessing from minutes.

## Comparison interpretation

Comparison normalizes member IDs before comparing ballots. Unresolved names,
failed member/tally checks, or matching partial ballots with different participant
sets are `not_comparable`, with the differences retained internally. A common
member's different ballot remains a disagreement. Recused, absent, abstain and
not_voting remain distinct values.

The initial read-only identity audit held the existing extracted ballots fixed:
615 of 1,611 apparent disagreements became agreements, while 378 became
unscorable because the minutes attribution was incomplete or unresolved. Those
are separate improvements; neither constitutes a measured accuracy rate. The
subsequent corpus replay also includes the reviewed category-boundary fixes.

## Repeatable checks

```sh
python -m pytest tests/test_minutes_identity.py tests/test_minutes_fixtures.py -q
python -m scripts.audit_minutes_identity --output /tmp/minutes-identity-audit.json
python -m scripts.audit_minutes_votes --meeting-id MEETING --details
```

`tests/fixtures/minutes_identity_passages.json` pins three actual reviewed
passages, including their source hashes and original text offsets, alongside
the existing six-document regression bucket. PostgreSQL tests verify that
republication moves minutes attributions, preserves API records byte-for-byte,
maintains counters, and saves identity-match receipts.

## Applied results

All 8,030 meetings were replayed from saved text without processing failures.
The comparable/attempted-comparison cohort remains 5,181 observations:

| Classification | Before | After |
| --- | ---: | ---: |
| Agreement | 901 | 1,684 |
| Disagreement | 1,611 | 184 |
| Not comparable | 2,669 | 3,313 |

Of the 184 remaining disagreements, 110 have API records containing only
`not_voting`; 74 have other differing ballot values. These are data comparisons,
not an accuracy score or proof that either source is correct. A separate raw-name
scan identified 38 meetings needing more conservative attribution. Their
conflicting claims were withheld and replayed, preserving independent totals
and outcomes. The final scan found no remaining flagged public attributions.

All 337,763 API vote records were verified unchanged across the update using a
hash of their complete stored rows. Counter and current-publication lineage
checks passed. The focused backend suite passed 170 tests. No migration was
needed, and historical member records and parse runs were retained.
