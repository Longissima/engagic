# Roll-call parser spike (2026-08-04)

Feasibility evidence for deterministic (zero-LLM) extraction of per-member roll-call
votes and motion outcomes from meeting MINUTES PDFs, scored against Legistar API vote
records as ground truth. Doctrine and sequencing: spygov `docs/MODEL_DOCTRINE.md`
("The roll-call track").

**Result:** 8 council meetings (milwaukee + denver, Jun-Jul 2026), 540 roll-call items,
7,690 member-vote tuples: **100% per-member precision, 99.8% recall, 100% outcome
accuracy.** Zero wrong publishes in any configuration — the publish gate (roster
gazetteer resolution + tally arithmetic) converts every failure mode into abstention.
The one true miss is a clerk defect (vote lists present, motion sentence never written);
the parser correctly abstained.

Design being validated, in order of load-bearing-ness:
1. Roster gazetteer (production: `council_members`) — names are a closed per-city set.
2. Template drivers per vendor dialect (Denver needed 2 patterns, Milwaukee 3; each
   new client converges in hours, not weeks).
3. Publish gate: per-category name-count must equal stated count, every name must
   resolve uniquely against the roster, and every canonical member must appear
   exactly once across all categories; mismatch = abstain, never guess.

Non-Legistar probe (CivicClerk/Wheatland, Revize/Craig, self-hosted/Hugo): the pattern
family transfers; what varies is *attribution power* — named-list minutes give full
per-member data; tally-only minutes still yield full attribution for unanimous votes
via the attendance roster (tally == present count is deterministic arithmetic), plus
movers/seconders by name.

Known limits: ~95% of sampled votes unanimous (split-vote precision rests on 28 items,
all correct); 2 clients, one window; born-digital PDFs only (no OCR tested);
consent-calendar block-to-items attribution not built; document-order alignment for
repeated same-file motions.

Files: `parse.py` (gazetteer + two template drivers + publish gate), `fetch.py`
(cache-first Legistar API fetcher), `score.py` (GT scoring harness), `scores.json`
(machine-readable results). Full cache/PDFs/ground-truth stayed in the originating
session scratchpad; regenerate from a clean checkout with:

```bash
python scripts/spikes/rollcall/fetch.py
python scripts/spikes/rollcall/score.py
```

The fetcher creates the ignored `cache/`, `pdfs/`, and `out/` directories. The
scorer updates the tracked `scores.json` beside these scripts.
