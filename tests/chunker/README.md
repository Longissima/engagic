# Chunker corpus suite

Regression tests for the agenda PDF chunker cascade (`vendors/adapters/parsers/router.py`)
against real PDFs from prod cities. The corpus is stratified: for each vendor,
cities whose meetings got item-level processing ("ok") and cities that fell back
to monolithic summaries ("fallback" — the chunker's hardest inputs).

## Layout

| File | What |
|------|------|
| `../chunker_corpus_seed.tsv` | Prod-derived sample (vendor × outcome × city), pulled 2026-06-10 |
| `fetch_fixtures.py` | Downloads the PDFs → `fixtures/` (gitignored), writes `manifest.json` |
| `manifest.json` | Provenance + sha256 + fetch status per fixture (committed) |
| `update_goldens.py` | Runs the cascade, snapshots results → `golden/` (committed) |
| `test_routing.py` | Same PDF must keep winning the same rung (`v2:toc` etc.) |
| `test_behavior.py` | Item numbers/titles/pages/attachments match golden + sanity invariants |
| `test_failures.py` | Zero-items results must carry a classified `failure_reason` |
| `test_profile.py` | Morphology signal extraction (synthetic PDFs) + corpus invariants |
| `test_text_chunker.py` | Flat-text extractor guards and item shape |
| `test_morphology.py` | Classifier rule table + corpus blast-radius pinning |

## Workflow

```bash
uv run python tests/chunker/fetch_fixtures.py    # once, or after corpus changes
uv run python tests/chunker/update_goldens.py    # after INTENTIONAL chunker changes
uv run pytest tests/chunker -q                   # the actual gate
```

Tests skip cleanly when fixtures are absent, so CI without the corpus stays green.

After changing a chunker, regenerate goldens and **read the diff** — that diff is
the behavior change. A routing test failure means a real city's documents now
take a different branch; that's either the fix you intended or a regression.

URLs rot (see `pipeline/url_refresh.py` for why). `manifest.json` pins sha256 of
what the goldens were generated against; if a refetch returns different bytes,
the goldens may legitimately disagree — treat the city as a new fixture.
