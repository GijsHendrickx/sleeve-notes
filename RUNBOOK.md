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

## Pause staging to save costs

Use this when you're working only locally for an extended period (weeks+) and
don't need staging up. Local dev is unaffected — `docker compose up -d db` +
`manage.py runserver` keep working with no Render touchpoint.

Three billable resources, ~$7/mo each on starter:

1. **Web service** `sleeve-notes-web-staging` → dashboard → Settings → **Suspend Service**. Stops billing for compute. Auto-deploys are blocked while suspended; `render.yaml` and env vars are preserved.
2. **Worker service** `sleeve-notes-worker-staging` → same as above.
3. **Database** `sleeve-notes-db-staging` — there is no "suspend" for Postgres, only "delete":
   - **Keep paying** (~$7/mo) to preserve all staging data, OR
   - **Delete** the database (Settings → Delete Database). Staging only holds dev test data, so this is usually fine. Saves the full $7/mo. The `render.yaml` `databases:` block re-creates an empty one on resume.

To prevent surprise resurrections from pushes while suspended: the suspend
state already blocks deploys, so no additional action needed. `render.yaml`
can stay as-is.

## Resume staging

After pause, when you want to deploy again:

1. **If the DB was deleted:** dashboard → Blueprints → **Sync Blueprint** (re-reads `render.yaml`, re-creates the DB). Wait until the database row shows "Available".
2. **Web service** → Resume. On first start after resume, the Dockerfile's `CMD` runs `manage.py migrate --noinput` which (re-)creates the schema if it's empty.
3. **Worker service** → Resume (only when you have routes wired to `enqueue_*` — until then leave the worker suspended; the web alone is enough for fase 5 development).
4. **Re-paste secrets** *only if* you also deleted the services: env vars survive suspend but die with delete. The values live in Bitwarden under `Sleeve Notes / staging-secrets`.
5. Push (or "Manual Deploy → Latest Commit" in the dashboard) to trigger the first build after resume.

Sanity check after resume:
- Web `/healthz` returns 200.
- Behind Basic Auth gate, sign in via Discogs works (the staging Discogs OAuth app's callback URL has not changed, so no reconfiguration needed).
