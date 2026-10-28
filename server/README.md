# server

This is the public HTTP API behind engagic.org: FastAPI over the shared asyncpg pool, served by `engagic-api` behind nginx and Cloudflare.

## Rules

- **Read from the database only.** A request never fetches a vendor site or runs extraction or an LLM. Sync and processing happen in the pipeline, and the API serves what they stored. The admin endpoints that force a sync or a meeting run are token-gated and whitelisted.
- **No aggregate over a large table runs per request.** Platform-wide numbers come from `platform_metrics_snapshot`, refreshed hourly. Measure any new heavy query with `EXPLAIN (ANALYZE, BUFFERS)` before it ships.
- **Stale processes serve stale code.** The API loads code once. After a deploy, restart it, especially when new code writes metadata keys that the API's strict models (`extra="forbid"`) would reject.

## Layout

| Path | Role |
|---|---|
| `main.py` | App, lifespan (pool, JWT init, Stripe), middleware, router wiring |
| `dependencies.py` | Injection of the `Database` and other shared services |
| `routes/` | One module per domain: search, meetings, topics, matters, votes, committees, engagement, feedback, deliberation, auth, dashboard, donate, flyer, events, happening, monitoring, admin |
| `services/` | Logic shared between routes: meeting assembly, search, flyer rendering |
| `utils/` | Validation, response shaping, geo lookup, vendor and meeting URL builders |
| `models/requests.py` | Request bodies |
| `metrics.py`, `middleware/metrics.py`, `middleware/request_id.py` | Prometheus instrumentation and request ids |

Most routes live under `/api`. Deliberation is versioned at `/api/v1/deliberations`. Auth and dashboard come from `userland/`: magic-link login, JWT sessions, and digest subscriptions.

## Security files (not in git)

Rate limiting, temporary bans, Turnstile verification and the security config are deliberately git-ignored:
- `rate_limiter.py`, `turnstile.py`, `security_config.py`;
- `middleware/` apart from the metrics and request-id modules;
- `routes/turnstile.py`, `routes/duckling.py`.

They exist only on the VPS. They are shared with the other properties through the security service in `/opt/engagic-ops`, which loads `conf/security.json` and holds the backups. Edits to them are live and have no git history, so back up first. A property that goes down without a crash in its journal was most likely disabled by the circuit breaker; check its log before anything else.

## MCP server

`mcp_server.py` at the repo root is a separate service (`engagic-mcp`, port 8003 behind nginx, streamable HTTP, bearer token `ENGAGIC_MCP_TOKEN`). It gives Claude clients search, meeting, matter timeline and city tools, plus `run_query`, which accepts read-only SQL only.

Endpoint reference: [docs/API.md](../docs/API.md).
