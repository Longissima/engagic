# Minutes semantics review — 2026-09-12

Minutes are the primary evidence. API agreement is a diagnostic, never a gate on
publication or permission to change a recorded ballot. Public motion groups and
member history/topic profiles prefer confirmed minutes for the same item. API-only
items remain explicitly sourced fallback; API ballots do not fill holes in a
minutes roll call. Internal motion inspection can request both sources with
`include_api_comparison=True`. Stored API rows are retained.

The outcome is the outcome of the **motion**, not the application, appeal, or
policy it concerns. A motion to deny that carries is passed. A `DENIED` disposition
alone leaves the motion outcome unknown. When a nearby result box repeats the
same tally/unanimous narrative, it is linked to that motion and retains its explicit
carried/failed outcome. Subject disposition is retained separately in the internal
observation. No majority or charter rule overrides an explicit result.

## Reviewed findings

- Wauwatosa and Winder: successful motions to dismiss/deny were followed by
  `RESULT: DENIED`. These are passed motions, not failed motions.
- Winter Park and other jurisdictions: historical permit/request denials in
  testimony matched an overbroad result pattern. Restrict that pattern and retain
  unsupported language internally rather than publishing it as a current motion.
- Wausau: `Yes 7, No 1, Abstained 0` and dash-separated named categories were
  misread as an empty category. Preserve the actual counts/names and the explicit
  `MOTION FAILED` result.
- Contra Costa: district-prefixed supervisor titles prevented deterministic roster
  matching. Strip the role prefix, then use the existing unique-name checks.
- Cincinnati: vertical office-prefixed name lists were being collapsed into a
  single unparseable name. Preserve the separate named entries.
- Fresno: `Recuse:` is an explicit recusal category. Keep it separate from absence,
  abstention, and yes/no; the final roll call takes precedence over introductory
  discussion of possible recusals.
- Date strings on result lines are not tallies. Require a vote/tally context.

## Interpreting the original review bucket

The 184 disagreements included 165 whose differing API ballots were exclusively
`not_voting`; this includes mixed API roll calls that also recorded absences or
other categories correctly. Eight further Fresno cases omitted the `Recuse:`
category. Other explicit differences include Contra Costa's recorded abstention,
King County/Waukesha's named ayes versus API absences, and Ocala's printed excused
member versus API yes/abstain records. Preserve what the minutes actually say;
these comparisons do not prove which municipal representation is correct.

The original 3,313 non-comparable observations comprised 1,872 without resolvable
minutes voters, 797 with multiple/ambiguous motions, 376 incomplete roll calls,
and 268 differing participant sets. The first bucket combined outcome-only records
with genuine unresolved names; the diagnostic now separates minutes that do not
name voters. Multiple motions and outcome-only minutes remain useful evidence.
Repeated surnames (such as two Carluccis in Jacksonville) and unresolved identities
remain withheld individually; the API is not used to guess missing ballots.

There were no failed processing runs in the stored ledger when this review began.
The 242 "failed" records were motion outcomes, not parser failures. A motion that
fails for lack of a second has an outcome without invented member ballots.

Seven short saved-source passages plus focused semantic cases extend the existing
fixture bucket. No new extraction infrastructure, schema, or source refetch is
required for these changes.

Additional spot checks of the largest replay changes caught inline `No-None` /
`Abstain-None` being consumed as names (Green Bay), and named committee approvals
quoted in council minutes being treated as council motions (Nashville). These now
remain separate categories and reported committee actions respectively. Summary
receipts include both the motion narrative and its repeated result box.

## Rollout status

The implementation is ready and the focused suite passes 122 tests, including
isolated PostgreSQL checks that minutes replace API-owned appearance summaries and
subsequent API refreshes cannot overwrite them. The full saved corpus was reviewed
read-only; final follow-up fixes were checked against the saved passage bucket and
largest-change samples. Provisional parser-candidate totals are not published-data
counts: the writer still requires a durable matter identity.

Production republication has **not** run. Automatic approval review rejected the
corpus-wide write because of its scope. The pending action is to replay all 8,030
saved meetings with the tested writer, verify API rows/counters/lineage, and restart
the Engagic API/MCP readers. It uses existing saved text, retains prior audit runs,
and requires no migration or city-document refetch. No Motioncount files are changed.
