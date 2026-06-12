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
| `test_quality.py` | Garbage-title lint, repair strategies, segmentation smell |
| `test_ground_truth.py` | Extraction vs `truth/*.json` — recall/precision ratchets |
| `truth/` | Item lists read directly from the PDFs (committed; see `read_by`) |
| `grow_truth.py` | Gemini reads fixtures lacking truth files → pinned truth JSON |

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

## Goldens vs ground truth

Goldens pin *stability* (same PDF → same output), including yesterday's
mistakes. `truth/*.json` pins *correctness*: item lists read directly from
the rendered PDF pages (Claude can do this — `read_by` records provenance).
`expected_recall`/`expected_precision` are ratchets — chunker changes may
only move them up; when extraction improves, bump the pin and the diff
documents the win.

## The standing loop

Coverage grows at the speed of the failure pool, not your evenings:

1. **Select** — on the server, pull failing/smelly meetings from the audit
   pool into `../chunker_corpus_seed.tsv`:
   ```sql
   SELECT j.vendor, q.banana, m.id, m.date, m.agenda_url, m.packet_url,
          q.processing_metadata->'chunk'->>'failure_reason' AS outcome
   FROM queue q
   JOIN meetings m ON m.id = q.meeting_id
   JOIN jurisdictions j USING (banana)
   WHERE q.processing_metadata->'chunk'->>'failure_reason' IS NOT NULL
      OR q.processing_metadata->'chunk'->'quality'->>'seg_smell' IS NOT NULL
   ORDER BY q.created_at DESC;
   ```
2. **Fetch** — `uv run python tests/chunker/fetch_fixtures.py`
3. **Read** — `GEMINI_API_KEY=... uv run python tests/chunker/grow_truth.py`
   (LLM reads the front pages, scores the chunker against the reading,
   pins recall/precision at measured values — pennies per document)
4. **Review** — the truth-file diff is a draft of reality; spot-check
   titles against the fixture before trusting a pin
5. **Ratchet** — `uv run pytest tests/chunker -q` enforces the pins forever
