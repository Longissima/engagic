# City Lists

Status tracking and regional lists for city coverage.

## Usage

Process cities from a list file:
```bash
./deploy.sh sync-cities @munis/bay-area.txt
./deploy.sh process-cities @munis/synced.txt
./deploy.sh sync-and-process-cities @munis/florida.txt
```

Or use comma-separated list directly:
```bash
./deploy.sh sync-cities 'paloaltoCA,oaklandCA,berkeleyCA'
```

## File Format

- One city banana per line
- Comments start with `#`
- Blank lines are ignored

## Status Tracking Files

| File | Count | Description |
|---|---|---|
| `processed.txt` | 555 | Item/matter-level summaries generated |
| `meeting-level.txt` | 31 | Monolithic packet summaries only (their ceiling) |
| `synced.txt` | 71 | Have meetings in DB, pending processing |
| `never-synced.txt` | 302 | In DB but 0 meetings fetched from vendor |

Last updated: 2026-03-28. Total jurisdictions: 959.

## Regional Lists

- `bay-area.txt` - SF Peninsula + East Bay core (45)
- `bay-area-2.txt` - Bay Area smaller cities + counties (66)
- `bay-area-all.txt` - Complete Bay Area coverage (109)
- `socal.txt` - Southern California (70)
- `ca-counties.txt` - California counties (22)
- `florida.txt` - Florida cities (81)
- `texas-triangle.txt` - Texas Triangle metro (82)
- `east-coast.txt` - BosWash corridor (12)
- `metro-areas.txt` - All working metro area cities (140)
- `sbc.txt` - SBC office locations (18)
- `notable-unprocessed.txt` - High-value cities to onboard (62)
- `test-small.txt` - Quick validation set (2)

## September 17 expansion

`new-jurisdictions-2026-09-17.txt` is the consolidated list of all 111 jurisdictions
from the regional expansion: 103 additions and 8 existing jurisdictions with
source or extraction repairs. It includes agency batches 7, 8, and 9.

```bash
./run.sh sync @munis/new-jurisdictions-2026-09-17.txt
```
