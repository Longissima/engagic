# munis

Target lists for `run.sh sync|process|sync-and-process`. A list has one banana per line; `#` starts a comment and blank lines are ignored.

```bash
./run.sh sync @munis/bay-area.txt
./run.sh process knowns
./run.sh sync 'paloaltoCA,oaklandCA'
```

`run.sh` resolves a bare name to `munis/<name>.txt`, so `./run.sh sync socal` works too.

## Generated on every use

`scripts/deploy.sh` (`resolve_targets`) rebuilds these from the database whenever its target name is used, so they never drift. The copy here is whatever the last run wrote.

| Target | File | Contents |
|---|---|---|
| `knowns` / `processed` | `processed.txt` | active non-school jurisdictions with at least one summary; the routine target |
| `all` | `all.txt` | every active jurisdiction with a meeting; the onboarding pass that earns new ones a first summary |
| `unpulsed` | `unpulsed.txt` | active jurisdictions that pulse is not watching or has not probed cleanly in a day, plus all of BoardBook; the daily sweep |
| `schools` | `school-districts.txt` | active school districts |

## Hand-made

Regional sets (`bay-area*.txt`, `socal.txt`, `ca-counties.txt`, `monterey-county.txt`, `florida.txt`, `texas-triangle.txt`, `texas-isds.txt`, `east-coast.txt`, `metro-areas.txt`, `sbc.txt`, `basket.txt`) and onboarding batches (`new-*.txt`, `top-50-pending-*.txt`). These are snapshots from when they were written. The older status lists (`synced.txt`, `never-synced.txt`, `meeting-level.txt`, `notable-unprocessed.txt`, `newly-synced.txt`, `queued-no-summaries.txt`) date from March 2026 and are no longer maintained. `discovery-seed-2026-09.csv` seeds `scripts/discover_vendor_slugs.py`.
