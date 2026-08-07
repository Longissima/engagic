# Engagic Deployment Guide

**Last Updated:** 2026-03-02 (Post-Migration to Hetzner)

Operational guide for deploying and managing Engagic on the production VPS.

---

## Server Details

| Property | Value |
|---|---|
| Provider | Hetzner CPX21 |
| IP | 5.78.189.81 |
| OS | Ubuntu 24.04.4 LTS |
| Kernel | 6.8.0-101-generic |
| CPU | 3 vCPU |
| RAM | 3.8 GB |
| Disk | 75 GB (13% used) |
| Python | 3.14.3 (system), 3.14.3 (venvs) |
| PostgreSQL | 17.9 + PostGIS 3.6 |
| nginx | 1.24.0 |
| uv | 0.10.7 |

### User Setup

All application code runs under the `engagic` system user (UID 999), home at `/opt/engagic`. This user has passwordless sudo and two SSH keys configured (id_ed25519 for Engagic repos, id_bientou for smokepac).

Root SSH is disabled. Password auth is disabled. Connect via:

```bash
ssh engagic@5.78.189.81
```

### Repos

| Repo | Path | Port | Purpose |
|---|---|---|---|
| engagic | /opt/engagic | 8000 (API), 8003 (MCP) | Main platform |
| smokepac | /opt/smokepac | 8002 | Smokepac API |
| motioncount | /opt/motioncount | 8001 | Motioncount API |

---

## Prerequisites

- Ubuntu 24.04+
- Python 3.13+
- PostgreSQL 17+ with PostGIS
- Root or sudo access
- uv (Python package manager)

---

## Initial Setup

### 1. Install System Dependencies

```bash
apt update && apt upgrade -y
apt install -y git python3-pip python3-venv curl
```

### 2. Clone Repository

```bash
cd /opt
git clone git@github.com:yourorg/engagic.git
cd engagic
```

### 3. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# PATH is configured in ~/.bashrc and ~/.profile
```

### 4. Install Dependencies

```bash
cd /opt/engagic
uv sync
```

### 5. Configure Environment

Two files provide environment variables:

- `/opt/engagic/.env` — app config, DB connection, ports, rate limits
- `/opt/engagic/.llm_secrets` — API keys (Gemini, etc.)

---

## Database

PostgreSQL 17.9 with PostGIS 3.6. Two databases:

| Database | Size | Purpose |
|---|---|---|
| engagic | ~236 MB | Main platform (cities, meetings, items, queue, city_matters) |
| smokepac | ~17 MB | Smokepac data |

Connect:

```bash
psql -U engagic -h localhost engagic
```

### Key Table Counts (as of migration)

| Table | Count |
|---|---|
| cities | 840 |
| meetings | 9,145 |
| items | 88,487 |
| queue | 34,575 |
| city_matters | 35,005 |

---

## Systemd Services

Seven units, all enabled on boot:

| Unit | Type | Description |
|---|---|---|
| engagic-api.service | simple | Uvicorn API on :8000 |
| engagic-fetcher.service | simple | Auto city sync (pipeline.conductor fetcher) |
| engagic-processor.service | simple | Queue worker (pipeline.conductor processor), 3GB memory limit |
| engagic-mcp.service | simple | MCP server on :8003 |
| engagic-digest.timer | timer | Weekly digest (Sundays 14:00 UTC) |
| engagic-digest.service | oneshot | Runs weekly_digest (triggered by timer) |
| motioncount-api.service | simple | Motioncount API on :8001 |

All engagic services run as `engagic:engagic` user. motioncount-api currently runs as root.

### Common Commands

```bash
# Check all statuses
systemctl list-units 'engagic-*' 'motioncount-*' --all

# Start/stop/restart
systemctl start engagic-fetcher
systemctl restart engagic-api
systemctl stop engagic-processor

# View logs
journalctl -u engagic-api -f
journalctl -u engagic-processor -n 100

# After editing unit files
systemctl daemon-reload
```

---

## Nginx Reverse Proxy

Three site configs in `/etc/nginx/sites-enabled/`:

- `engagic-api` — api.engagic.org → :8000 (API) and /mcp/ → :8003
- `api.motioncount.com` — api.motioncount.com → :8001
- `smokepac-api` — api.smokepac.com → :8002

### Cloudflare Integration

All traffic goes through Cloudflare. nginx is configured with:

- **Cloudflare IP validation** — rejects direct (non-Cloudflare) requests with 403
- **Real IP extraction** — sets `$real_client_ip` from CF-Connecting-IP header
- **Rate limiting** — per-zone limits (api_general, api_matters)
- **IP blocking** — blocked IPs from `/opt/engagic/data/blocked_ips_nginx.conf`

Cloudflare IP ranges auto-update weekly via cron.

### SSL/TLS

Currently HTTP-only. After DNS cutover, run:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d api.engagic.org
certbot --nginx -d api.motioncount.com
certbot --nginx -d api.smokepac.com
```

Or use Cloudflare origin certificates (Full Strict mode).

---

## Cron Jobs

Root crontab (`sudo crontab -l`) has 11 active jobs:

| Schedule | Job |
|---|---|
| Daily 3 AM | pg_dump engagic (gzipped) |
| Sundays 2 AM | Full pg_dump engagic (.dump format) |
| Daily 4 AM | Cleanup daily backups > 14 days |
| Sundays 4 AM | Cleanup weekly backups > 28 days |
| Daily 4:30 AM | Session events cleanup (7-day retention) |
| Sundays 4 AM | Cloudflare IP range update (engagic) |
| Sundays 3 AM | Cloudflare IP range update (motioncount) |
| Daily 9 AM UTC | Motioncount daily alerts |
| Daily 1 PM UTC | Happening Today analysis (8am EST) |
| Daily 2 PM UTC | Happening Today email (9am EST) |
| Sundays 2 AM UTC | Watchlist sync |

**Not yet scheduled** (added 2026-08-04, run manually until cadence is proven):
`scripts/sweep_minutes.py` fills `meetings.minutes_url` for meetings the 14-day
resync window left behind (minutes often publish 2-4 weeks post-meeting; listing
or API metadata for most adapters, including extra-vendor streams) — suggested
weekly. ProudCity and CivicPlus may make one lightweight meeting-page request per
candidate; WP Events queries its media API per event. No discovery path downloads
documents, parses PDFs, or writes to the corpus.
`scripts/ingest_minutes.py` pulls minutes bytes into the R2 corpus via the
analyzer's extraction-only path (no LLM client or key); incomplete writes retry
and completed stable URLs are revalidated every seven days by default so revised
minutes are captured. Deterministic download and extraction failures retry
weekly and are suppressed after three attempts for the current extractor
version; transient network, rate-limit, and server failures continue with weekly
backoff. Known HTML-only viewer URLs are excluded — suggested daily after sync.
Sweep before ingest.

---

## Monitoring

### Health Checks

```bash
# Engagic API
curl http://localhost:8000/api/health

# System metrics
curl http://localhost:8000/api/metrics

# Queue stats
curl http://localhost:8000/api/queue-stats
```

### Log Monitoring

```bash
journalctl -u engagic-api -f
journalctl -u engagic-fetcher -f
journalctl -u engagic-processor -f
journalctl -u engagic-mcp -f

# All engagic services at once
journalctl -u 'engagic-*' --since "10 minutes ago"
```

### Memory Monitoring

```bash
free -m
ps aux --sort=-%mem | head -10
```

### Expected Memory Usage (4GB RAM)

- Base system: ~200 MB
- engagic-api: ~200 MB
- engagic-processor: up to 3 GB (hard-limited via MemoryMax)
- engagic-fetcher: ~200 MB
- engagic-mcp: ~100 MB
- Total stable: ~1.5-2 GB

---

## Updating Code

```bash
ssh engagic@5.78.189.81

cd /opt/engagic
git pull
uv sync

# Restart affected services
systemctl restart engagic-api
systemctl restart engagic-fetcher engagic-processor

# Verify
systemctl status engagic-api
curl http://localhost:8000/api/health
```

---

## Backup Strategy

### Automated (via root crontab)

- **Daily 3 AM**: `pg_dump engagic | gzip` → `/opt/engagic/data/backups/`
- **Weekly Sunday 2 AM**: Full custom-format dump
- **Retention**: 14 days daily, 28 days weekly

### Manual Backup

```bash
PGPASSWORD=... pg_dump -U engagic -h localhost engagic | gzip > /tmp/engagic_manual_$(date +%Y%m%d).sql.gz
```

### Restore

```bash
systemctl stop engagic-api engagic-fetcher engagic-processor
gunzip -c /opt/engagic/data/backups/engagic_YYYYMMDD.sql.gz | psql -U engagic -h localhost engagic
systemctl start engagic-api engagic-fetcher engagic-processor
```

---

## Security

- [x] UFW firewall (22, 80, 443 only)
- [x] fail2ban on sshd
- [x] Root SSH disabled
- [x] Password auth disabled
- [x] Cloudflare-only nginx (direct access blocked)
- [x] Rate limiting active
- [x] IP auto-blocking for repeat offenders
- [x] Automated DB backups
- [ ] SSL/TLS (pending DNS cutover)

---

## Troubleshooting

### Service Won't Start

```bash
journalctl -u <service-name> -n 50

# Common issues:
# 1. Missing env vars in .env or .llm_secrets
# 2. Port already in use (lsof -i :8000)
# 3. Database connection refused (systemctl status postgresql)
# 4. Missing dependencies (uv sync)
```

### High Memory

```bash
# Check for OOM kills
dmesg | grep -i "out of memory"

# Processor has a 3GB MemoryMax limit — if it hits it, systemd kills it and restarts
journalctl -u engagic-processor | grep -i "memory\|kill\|oom"
```

### Database Issues

```bash
# Check PostgreSQL is running
systemctl status postgresql

# Check connections
psql -U engagic -h localhost -c "SELECT count(*) FROM pg_stat_activity;"

# Vacuum if needed
psql -U engagic -h localhost -c "VACUUM ANALYZE;"
```

---

## Migration History

### 2026-03-02: Old VPS → Hetzner CPX21

Migrated from previous VPS to Hetzner CPX21 (5.78.189.81). Key changes:

- **User model**: Moved from running everything as root (`/root/engagic`) to dedicated `engagic` system user (`/opt/engagic`)
- **Database**: SQLite → PostgreSQL 17 + PostGIS (migrated during an earlier refactor, restored from pg_dump)
- **Python**: 3.13 → 3.14.3
- **Services**: Monolithic daemon split into separate fetcher/processor/mcp services
- **Security**: Added UFW, fail2ban, disabled root SSH, Cloudflare-only nginx

### Still TODO after migration

See `/opt/engagic/.claude/projects/-opt-engagic/memory/migration-todo.md` for the full checklist. Key items:

1. Start remaining services (fetcher, processor, mcp, motioncount-api, smokepac-api)
2. Create smokepac-api.service (wasn't included in initial migration)
3. SSL certs via certbot (after DNS cutover)
4. DNS cutover — point Cloudflare A records to 5.78.189.81
5. Install zsh + bun for root user (quality of life)
6. Commit uv.lock changes back to repo
7. Monitor for Python 3.14 compat issues
