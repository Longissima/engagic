# Userland Deployment Guide

## Current Deployment

**Location:** `/opt/engagic`
**User:** `engagic` (non-root, security hardened)

### Services

| Service | Purpose | Status |
|---------|---------|--------|
| `engagic-api.service` | Main API (includes auth) | `systemctl status engagic-api` |
| `engagic-digest.timer` | Weekly digest (Sun 14:00 UTC) | `systemctl status engagic-digest.timer` |

### Environment

All environment variables in `/opt/engagic/.env`:

```bash
# Required for userland
USERLAND_JWT_SECRET=<secret>
MAILGUN_API_KEY=<key>
MAILGUN_DOMAIN=mail.engagic.org
MAILGUN_FROM_EMAIL=digest@engagic.org
FRONTEND_URL=https://engagic.org
APP_URL=https://engagic.org
```

## Common Operations

### Test Weekly Digest Manually

```bash
sudo systemctl start engagic-digest.service
journalctl -u engagic-digest.service --since "5 minutes ago"
```

### Test Email Templates

```bash
# Sends all 3 email types (magic link signup, login, digest) with real DB data
cd /opt/engagic && uv run userland/scripts/test_emails.py your@email.com
```

### Test Signup Flow

```bash
curl -X POST http://127.0.0.1:8000/api/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","name":"Test User","city_banana":"paloaltoCA"}'
```

### Check User Data

```bash
PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U engagic -d engagic -h localhost -c "
SELECT u.email, u.name, a.cities, a.criteria
FROM userland.users u
LEFT JOIN userland.alerts a ON u.id = a.user_id;"
```

### View Timer Schedule

```bash
systemctl list-timers | grep engagic
```

## Troubleshooting

### Email Not Sending
1. Check Mailgun credentials: `grep MAILGUN /opt/engagic/.env`
2. Test manually: `sudo systemctl start engagic-digest.service`
3. Check logs: `journalctl -u engagic-digest.service`

### Auth Endpoints 401
1. Verify JWT secret: `grep JWT /opt/engagic/.env`
2. Restart API: `sudo systemctl restart engagic-api`
3. Check logs: `journalctl -u engagic-api`

### Database Issues
```bash
# Check schema exists
PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U engagic -d engagic -h localhost -c "\dt userland.*"

# Check user count
PGPASSWORD="$ENGAGIC_POSTGRES_PASSWORD" psql -U engagic -d engagic -h localhost -c "SELECT COUNT(*) FROM userland.users"
```

## Rollback (Emergency)

```sql
-- WARNING: Destroys all user data
DROP SCHEMA userland CASCADE;
```

Then re-apply schema:
```bash
cd /opt/engagic && /opt/engagic/.venv/bin/python -m userland.scripts.setup_db
```
