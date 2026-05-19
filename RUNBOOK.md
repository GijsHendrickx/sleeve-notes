# Runbook — Sleeve Notes

Incident response notes. Fill in incrementally as things break in staging or
production. The point is: in a 3 AM panic, you read this file, not your terminal.

## Where things live

- **Render dashboard:** https://dashboard.render.com — services, env vars, logs, deploys.
- **Sentry:** https://sentry.io → project `sleeve-notes` — errors + performance.
- **GitHub:** https://github.com/GijsHendrickx/sleeve-notes
- **Bitwarden vaults:** `Sleeve Notes / staging-secrets`, `Sleeve Notes / production-secrets`.

## Branches & deploy

- `main` → production (when cut over; pre-cut-over: legacy FastAPI app)
- `django-port` → staging (auto-deploys on push)

## Common incidents

### Staging URL returns 5xx
- Check Render dashboard → service → Events for failed deploy.
- Check Render logs for stack trace.
- If recent deploy: rollback in Render dashboard → Manual Deploy → previous commit.

### "DB connection limit reached"
- Check active connections in Render Postgres dashboard.
- Restart web service to drop stale connections.
- Longer term: pgbouncer + DATABASE_POOL_URL for workers.

### Discogs API returns 503 or rate-limit errors
- Discogs is down or we hit per-user 60 req/min limit.
- Workers should auto-retry with backoff (Django-Q2 default).
- If persistent: throttle BPM tasks more aggressively.

### OAuth redirect loop
- Verify Discogs callback URL matches `https://<host>/accounts/discogs/login/callback/`.
- Check session cookie isn't being rejected (HTTPS only in staging+prod).
- Clear cookies and try again to rule out stale state.

### Rollback a deploy
- Render dashboard → service → Manual Deploy → select a previous commit → Deploy.
- Or: `git revert <bad-commit>` + push, auto-deploys.

### Find a specific user
- Django admin: `https://<host>/admin/` → Users → search by username or email.
- Render shell access (paid plans): `render ssh <service> "python manage.py shell"`.
