# Agent Instructions

You're working on **Sleeve Notes** — a Discogs → printable-stickers app, multi-tenant. The runtime is **Django 5.2 + Postgres + Django-Q2** behind a thin Tailwind/HTMX template layer. A separate `sleeve_notes/` Python engine holds the pure cascade + render logic that both the CLI and the web app reuse. Users sign in via Discogs OAuth 1.0a; per-user data is keyed by `request.user` (a custom `users.User`) everywhere.

The README is the authoritative user-facing doc. CLAUDE.md is for *you* — codebase conventions that aren't obvious from reading the code, and traps to avoid that have already cost time once.

## Architecture in one paragraph

The engine in `sleeve_notes/` is the source of truth for **pure** logic: deterministic Python that talks to Discogs (HTTP + parsing), runs the 5-source BPM cascade, lays out stickers, and drives ReportLab/SVG output. No DB code lives here. The Django app in `webapp/` adds ORM-bound services on top: `webapp/<app>/services/*.py` wraps the engine helpers with Postgres reads/writes, `webapp/<app>/views.py` are thin HTTP layers, and `webapp/<app>/management/commands/*.py` give the CLI surface (`sleeve-notes fetch` → `manage.py sync_discogs`, etc.). Background work runs through Django-Q2: web views `enqueue_*` jobs in `webapp/records/jobs.py` and a worker process executes them. **Never fork engine logic into web-only helpers** — extend the engine module or add a service wrapper.

## Directory layout

```
.tmp/              # Disposable: stickers.pdf, CSV uploads, debug dumps. Wipe freely.
sleeve_notes/      # Engine (Python package, pure helpers). Imported by the CLI
                   # delegators and by webapp/<app>/services/*.py.
webapp/            # Django project. `manage.py` lives here. Apps under it:
                   #   core/       UserJobLock + shared models
                   #   users/      AbstractUser subclass (custom AUTH_USER_MODEL)
                   #   records/    Release, Track, BpmCache, Override + services + views
                   #   print_runs/ PrintRun + render service + PDF endpoint
                   #   audit/      AuditEvent (append-only history)
                   #   discogs_provider/  Custom allauth OAuth1 adapter for Discogs
roadmap.md         # Feature ideas / future work. See "Capturing feature ideas" below.
migration_plan.md  # The Django port plan + Voortgang. Mostly historical now that
                   # fase 5 is complete; kept for fase 6 (Render production cut-over).
docker-compose.yml # Local Postgres. `docker compose up -d db` to start.
render.yaml        # Render Blueprint (currently suspended to save costs — see RUNBOOK.md).
.env               # Discogs creds, DJANGO_SECRET_KEY, DATABASE_URL, Spotify/YT keys.
                   # NEVER commit.
.env.example       # Documents every required + optional key; safe to commit.
```

## Local dev

```
docker compose up -d db     # Postgres on localhost:5432
sleeve-notes web            # runserver on 127.0.0.1:8000 + in-process Q2 cluster
```

`sleeve-notes web` is a thin wrapper around `webapp/manage.py runserver` that also opens the browser. The in-process Q2 cluster (gated by `DEV_INPROCESS_QCLUSTER=true` in `.env`) means one terminal, one Ctrl-C — no separate worker process needed for dev. **Restart isn't needed for Python or template edits** since Django's autoreloader watches everything under `webapp/` and `sleeve_notes/`. (Once in a while the reloader misses a urls.py change and you get a stale `NoReverseMatch`; manual restart fixes it.)

## Auth & multi-tenancy

**Discogs OAuth 1.0a via django-allauth.** No passwords. Login flow lives in `webapp/discogs_provider/` (a 30-line custom allauth OAuth1 provider) and the standard `/accounts/...` URLs. After sign-in, allauth creates a `SocialAccount` row and (because `SOCIALACCOUNT_STORE_TOKENS=True`) a `SocialToken` row holding the user's Discogs access token + secret. Background jobs read those tokens off the `SocialToken` when calling the Discogs API on the user's behalf.

**Two credential pairs to keep distinct.** *Consumer* key+secret identify the **app** to Discogs (lives in `.env`, one pair total). *Access* token+secret identify a specific user (lives in `SocialToken`, one pair per logged-in user). Every Discogs API call is HMAC-SHA1 signed with both pairs combined.

**Every data view filters on `request.user`.** Use `@login_required` for any view that touches user-owned models; never trust unauthenticated traffic. Cross-tenant leaks happen when a view forgets to `Release.objects.filter(user=request.user, ...)`. UUID PKs on user-facing models (`Release.id`, `PrintRun.id`, etc.) help — they're not enumerable — but they're not a substitute for the filter.

**Every engine helper that touches user data takes the `User` object as its first parameter.** Examples: `load_overrides(user)`, `derive_track_result(user, …)`, `build_bpm_lookup(user, releases)`, `releases_by_ids(user, ids)`. Adding a new helper? Follow the pattern. The user instance is the tenant boundary.

**Background jobs use `enqueue_*` helpers + `UserJobLock`.** Views POST to `/actions/<thing>`, the action view calls `enqueue_discogs_sync(user, ...)` or `enqueue_bpm_cascade(user, ...)` from `webapp/records/jobs.py`, which atomically acquires a `UserJobLock` row (OneToOne with user) and hands the task to `async_task`. **One running job per user**; a second click while one's in-flight returns HTTP 409. The worker's `finally` calls `UserJobLock.mark_done(user, summary)` or `.mark_failed(user, err)` so the toast can show the result for ~5s before lazy-deletion.

**Never delete a `UserJobLock` row while its worker is still running.** The Q2 worker holds the cascade state in memory (subprocess); deleting the lock from the outside leaves that worker orphaned but still consuming sources, AND lets `acquire()` succeed for a new click → two cascades fighting over the same `BpmCache` rows and `progress_text` column. The smoke-test that taught us this is documented in commit history. If you need to stop a running worker, restart `sleeve-notes web` (kills the subprocess); the lock then releases on next acquire.

**The toast is the universal job-status surface.** Fixed bottom-right, lives in `#run-banner` (base.html), polls `/run/banner` every 2s. Three states: running (blue determinate bar + live progress text), done (green bar + summary), failed (red bar + error). New batch actions get this for free as long as they go through `enqueue_*` and write progress via `records.jobs._make_progress_logger(user)`.

**Beatport is currently disabled.** The original SQLite kv-store kept Beatport's OAuth tokens; the Django port hasn't re-implemented that yet. The cascade silently skips Beatport — see `sleeve_notes/fetch_bpm.py:_load_beatport_tokens`. The other four sources (songbpm, Deezer, ReccoBeats/Spotify, AcousticBrainz) work normally.

## Web UI conventions

**Action-oriented, not pipeline-oriented.** The CLI thinks in pipeline steps (fetch → bpm → render). The web UI exposes each long-running task as a named sidebar action with a tailored modal — "Discogs sync", "Import Discogs csv", "BPM lookup". If a new long-running CLI subcommand lands, add it as a named action, not as an option in a generic dropdown.

**Sidebar shape.** Two labelled sections in this order: **Collection** (Records `/collection`, Tracks `/tracks`) and **Actions** (the named jobs + Print runs `/print-runs/`). The Dashboard is the homepage (`/dashboard`) reached via the "Sleeve Notes" wordmark click; it is NOT a sidebar item. The wordmark is title-cased ("Sleeve Notes"), larger than nav items, with the version underneath. The sidebar footer shows the signed-in username + Sign out. The whole sidebar is rendered only when `user.is_authenticated`; logged-out visitors see the `/` landing page with no chrome.

**Toast progress text gets prettified.** `_prettify` in `records/jobs.py` strips the cascade's `[N/M] ●● artist - title -> 128 key=8A sources=[...]` down to `(N/M) artist - title` and writes `progress_done`/`progress_total` for the determinate progress bar. New job types should emit `[done/total]` somewhere in their log lines so the bar fills.

**After completion, no auto-refresh.** Older versions of the legacy app issued `htmx.ajax(GET, current_url, ...)` after a job; the current Django port doesn't. The user manually refreshes to see new data. (Adding it back is a future polish — would live in the toast partial's done-state.)

**Engine reuse, not duplication.** Views that need data the CLI also computes — sticker layout, BPM lookup, render — call the same module in `sleeve_notes/` plus the service wrapper in `webapp/<app>/services/`. Concrete example: the sticker preview in the `/collection` detail drawer reuses `print_runs.services.render.releases_by_ids` + `build_bpm_lookup` + `sleeve_notes.preview.render_release_stickers_svg` — exactly what the PDF endpoint uses.

**Consistency across listing pages.** New listing pages mirror `/collection`'s affordances: search input is the FIRST element (same shape: `field h-8 px-3`), filter selects/chips right after, count + primary action at `ml-auto`. When a page already has a POST form (e.g. `/tracks`' big Save), use an empty sibling `<form id="..." method="get">` and bind the GET-only inputs to it via the HTML5 `form="..."` attribute so everything stays on one row without nesting forms.

**Rename a feature → update every surface in one pass.** A label change is never just the sidebar. Also update the page title (`{% block title %}`), breadcrumb (`{% block breadcrumb %}`), dashboard cards, cross-page links, and README references. Partial renames produce confusing UIs where the sidebar says "Records" but the page header still says "Collection". Grep before declaring done.

**Visual style.**
- No greyed-out text in actionable buttons — reads as disabled. `.btn-quiet` uses `--ink` for text; reserve `--dim` for decorative glyphs.
- No italics on song or release titles.
- Prefer full column headers (`Position`, `Format`, `Duration`) over abbreviations. `BPM` is the one universal exception. Abbreviate (`Pos`, `Fmt`, `Dur`) only when space is genuinely tight (e.g. the detail drawer on `/collection`) — never on full-width listing pages.
- Form controls on the same row share an explicit height (`h-8` for `.field` inputs/selects/buttons). Native `<select>` adds invisible vertical chrome that doesn't match `<input>` even with identical padding classes.
- Table rows that mix multi-line text with single-line inputs use `align-middle`, NOT `align-top`.
- Confirm async-feeling actions visually, even on a no-op. The `/tracks` per-row sync button takes 5–10 s; without `.flash-sync` on the response render, it looks broken when the value didn't change. Apply the same principle to any new per-row mutation.

**Django template gotchas.**
- `{% extends "..." %}` MUST be the first tag (load tags after extends if needed).
- `{# ... #}` is single-line only — use `{% comment %}...{% endcomment %}` for multi-line, otherwise tags inside the "comment" get parsed and you get cryptic "block X appears more than once" errors.
- Use `{% if x is not None %}` (not `{% if x %}`) when 0 is a valid value — Django evaluates `0` as falsy.

**Custom HTMX events use kebab-case.** Server-sent `HX-Trigger` values and matching `hx-on:*` / `hx-trigger=`... `from:body` listeners must use `kebab-case` event names (e.g. `run-status`, `close-modal`). Browsers lowercase HTML attribute names, so `hx-on:closeModal` becomes `hx-on:closemodal` in the DOM, which silently never matches a JS event named `closeModal`. Kebab-case survives the lowercase round-trip.

## Engine conventions

**`sleeve_notes/` is pure.** No DB code, no Django imports (except the CLI delegators and `django_setup.py` which intentionally boot Django). The package holds:
- HTTP clients for each BPM source + the cascade orchestrator (`fetch_bpm.py`)
- Sticker layout primitives + the `Drawer` protocol (`sticker_layout.py`)
- ReportLab PDF adapter (`generate_sticker_pdf.py`'s `PdfDrawer`) and the SVG variant (`preview.py`'s `SvgDrawer`)
- Release / track normalization helpers (`ingest.py`, `classify.py`)

**ORM-bound logic lives in `webapp/<app>/services/`.** `discogs_sync.py` wraps the Discogs HTTP layer with Django writes; `bpm_cascade.py` wraps the cascade with overrides + BpmCache; `render.py` wraps the PDF/SVG generators with release/lookup helpers. CLI commands and views both call these.

**Every long-running step must be idempotent and resumable.** Sync skips releases whose tracklist is already cached; the cascade skips tracks whose `BpmCache.sources_tried` already covers `ALL_SOURCES` (unless `force=True`). Background jobs commit at coarse intervals (sync: every 25 releases; cascade: per-track via `save_cache_entry` inside the thread pool) — never per row at the boundary, never only at the end.

**Don't add type/code-style comments that explain WHAT the code does.** Well-named identifiers already do that. Reserve comments for non-obvious WHY: a workaround for a bug in an upstream library, a constraint that's easy to violate, a subtle invariant.

## Capturing feature ideas

Feature ideas often slip into messages that are about something else — "and it would be cool if X", "we should probably also be able to Y", "what if users could also Z". The user has explicitly said these are easy to lose and they want them preserved.

Whenever the user describes a feature, capability, or non-trivial improvement that **isn't in scope of the current task**, proactively append it to `roadmap.md` — no need to ask first. Match the existing style: `## Section` headers with `**Name** — description` items for short entries, or short prose sections for larger ideas. Flag genuinely open design questions in a sub-section so the implementer doesn't have to rediscover them later.

Briefly mention in your reply that you captured it (one line is enough), so the user can correct if you misread the intent.

When in doubt, capture it. Re-finding a forgotten idea costs more than a one-line entry in a file.

## Before pushing to GitHub

Whenever I ask you to push (`push`, `push to <branch>`, `ship it`, etc.) — do NOT just `git push` the pending diff. First do a thorough sweep of the repo to confirm everything still matches reality, then commit any fixes alongside the push:

- **README.md** especially — feature labels, sidebar/page names, screenshot text, CLI flags, command examples. UI renames are the #1 source of stale docs.
- **CLAUDE.md (this file)** — conventions you've added or invalidated this session.
- **CLI `--help` strings + the corresponding README snippets** — they should agree.
- **In-code comments and docstrings** referencing renamed/removed concepts.

If anything is out of date, fix it in the same push (separate commit is fine). If you're unsure whether a doc update is needed, ask before pushing — it's cheaper than a follow-up "you forgot to update X" cycle.
