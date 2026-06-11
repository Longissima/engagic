# HTML parser corpus suite

The chunker playbook applied to the HTML layer (CivicPlus, Granicus,
PrimeGov): real agenda pages from prod cities, golden snapshots, and dialect
pattern pinning. Parsers now tag every parse with `html_pattern`
(`primegov_la`, `civicplus_hierarchical`, `granicus_generated`, ...) which
rides the meeting's `html_audit` into `queue.processing_metadata`.

| File | What |
|------|------|
| `../html_corpus_seed.tsv` | Prod-derived sample (vendor × outcome × city), pulled 2026-06-11 |
| `fetch_fixtures.py` | Fetches agenda HTML as the adapter would → `fixtures/` (gitignored) |
| `manifest.json` | Provenance + sha256 + final_url (drives Granicus dispatch) |
| `update_goldens.py` | Snapshot results → `golden/` (committed) |
| `test_html_corpus.py` | Pattern routing + item structure + invariants |

```bash
uv run python tests/html/fetch_fixtures.py
uv run python tests/html/update_goldens.py
uv run pytest tests/html -q
```

Non-HTML fetch outcomes are corpus signal: `pdf_redirect` (18 of 31 seed
rows!) means the city has no HTML agenda rendering — structurally why
CivicPlus/Granicus lean on the PDF chunker. Zero-item fixtures with a
matched pattern (woodsideCA hierarchical, topekaKS s3_fallback) are the
HTML-layer failure specimens to ground-truth next.
