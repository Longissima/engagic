# userland

Accounts and the weekly civic digest. A resident watches a city, optionally adds keywords, and gets one email a week about what is coming up. There are no passwords and no paid tier.

## Flow

1. The watch button or signup on engagic.org sends a magic link (`email/transactional.py`, Mailgun).
2. The link becomes a JWT session (`auth/jwt.py`); `server/routes/auth.py` and `server/routes/dashboard.py` expose the endpoints.
3. The dashboard edits the watched city and keywords, stored in the `userland` schema (`database/schema_userland.sql`; repository `database/repositories_async/userland.py`).
4. The `engagic-digest` timer runs `userland/scripts/weekly_digest.py` on Sundays.

## The digest

For each watched city, the digest builds a section of upcoming meetings and a section of keyword matches. Matching (`matching/matcher.py`) runs two tracks:
- **string-based**: keyword hits in item summaries, at item granularity;
- **matter-based**: hits on deduplicated matters, so one bill heard by five committees is one alert.

When a reader has no keywords, or nothing matched, the digest adds editorial picks for the city's week. Candidates are ranked by motioncount's importance scores when enough of the week has been extracted (read through `ENGAGIC_MC_DATABASE_URL`), and an LLM writes the headlines from engagic's own summaries. Without that coverage an LLM picks from every candidate. The last resort is item titles, with summarized items first.

Templates live in `email/templates.py`, with dark mode and one-click unsubscribe tokens for CAN-SPAM.

## Configuration

`USERLAND_JWT_SECRET`, `MAILGUN_API_KEY`, `MAILGUN_DOMAIN`, `MAILGUN_FROM_EMAIL`, `FRONTEND_URL` (magic links) and `APP_URL` (digest links), all in the VPS environment.

## Scripts

| Script | Use |
|---|---|
| `scripts/weekly_digest.py` | the digest; safe to run by hand, but it emails every active user |
| `scripts/test_emails.py <address>` | sends every template with real data to one address (`run.sh test-emails`) |
| `scripts/create_user.py` | creates an account by hand |
| `scripts/setup_db.py` | applies `schema_userland.sql` (idempotent) |
| `scripts/happening_email.py` | daily "happening today" email; reads the deprecated `happening_items` |
| `scripts/interest_form_blast.py` | one-off announcement to an interest-form CSV |
