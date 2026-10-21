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
                   #   print_runs/ PrintRun + flow.py (the 4 steps) + render service
                   #   audit/      AuditEvent (append-only history)
                   #   discogs_provider/  Custom allauth OAuth1 adapter for Discogs
roadmap.md         # Feature ideas / future work. See "Capturing feature ideas" below.
migration_plan.md  # The Django port plan + Voortgang. Mostly historical now that
                   # fase 5 is complete; kept for fase 6 (Render production cut-over).
docker-compose.yml # Local Postgres. `docker compose up -d db` to start.
render.yaml        # Render Blueprint, staging deploys from main (suspended to save costs — see RUNBOOK.md).
.env               # Discogs creds, DJANGO_SECRET_KEY, DATABASE_URL, Spotify/YT keys.
                   # NEVER commit.
.env.example       # Documents every required + optional key; safe to commit.
```

## Local dev

```
docker compose up -d db     # Postgres on localhost:5432
sleeve-notes web            # runserver on 127.0.0.1:8000 + in-process Q2 cluster
```

`sleeve-notes web` is a thin wrapper around `webapp/manage.py runserver` that also opens the browser. The dev Q2 cluster (gated by `DEV_INPROCESS_QCLUSTER=true` in `.env`) means one terminal, one Ctrl-C — no separate worker process to babysit.

**The dev cluster must stay a child *process*, never a thread inside runserver.** `core/apps.py` launches `manage.py qcluster` via `subprocess.Popen` and kills it on exit. It used to run `Cluster().start` on a daemon thread, which fails hard on macOS: django-q2 (1.11.1) hardcodes the `fork` start method whenever it's available, and forking a parent that has threads running — or that has already touched the network stack — kills the child the instant it calls into CoreFoundation (`objc[…]: +[NSNumber initialize] may have been in progress in another thread when fork() was called. Crashing instead.`). `requests` triggers it on its first call through macOS proxy lookup. The symptom is nasty and easy to misread: the worker dies before its `finally` runs, so the task is never acked, the `UserJobLock` row is never released, and **the toast spins forever on a job that is already dead**. `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` does *not* fix it (verified — the child still takes SIGSEGV). If you ever see a job hang with no progress, check the server log for `objc[` and `reincarnated worker … after death` before suspecting the job itself. **Restart isn't needed for Python or template edits** since Django's autoreloader watches everything under `webapp/` and `sleeve_notes/`. (Once in a while the reloader misses a urls.py change and you get a stale `NoReverseMatch`; manual restart fixes it.)

## Auth & multi-tenancy

**Discogs OAuth 1.0a via django-allauth.** No passwords. Login flow lives in `webapp/discogs_provider/` (a 30-line custom allauth OAuth1 provider) and the standard `/accounts/...` URLs. After sign-in, allauth creates a `SocialAccount` row, and `discogs_provider/signals.py` writes the user's Discogs access token + secret to `DiscogsConnection`, Fernet-encrypted (`core.fields.EncryptedTextField`, key in `FIELD_ENCRYPTION_KEY`). Background jobs read the tokens from there. `SOCIALACCOUNT_STORE_TOKENS` is deliberately **off**: allauth's `SocialToken` keeps them in plain text. Never turn it back on, and put any future per-user credential (Spotify, Beatport) in an `EncryptedTextField` too.

**Two credential pairs to keep distinct.** *Consumer* key+secret identify the **app** to Discogs (lives in `.env`, one pair total). *Access* token+secret identify a specific user (lives in `DiscogsConnection`, one pair per logged-in user). Every Discogs API call is HMAC-SHA1 signed with both pairs combined.

**Every data view filters on `request.user`.** Use `@login_required` for any view that touches user-owned models; never trust unauthenticated traffic. Cross-tenant leaks happen when a view forgets to `Release.objects.filter(user=request.user, ...)`. UUID PKs on user-facing models (`Release.id`, `PrintRun.id`, etc.) help — they're not enumerable — but they're not a substitute for the filter. `core/tests_tenant_isolation.py` is the safety net: it logs in as a second user and hits every `<uuid:...>` route with the first user's ids. A new UUID route fails that test until you add it to `UUID_ROUTES`; a new view that takes ids in the POST body or query string has to be added to `BodyIdTests` by hand, because the URLconf can't reveal it.

**Sentry gets a user id, nothing more.** `send_default_pii=False` and `include_local_variables=False`; `middleware/sentry_user.py` tags events with the bare pk. Frame locals stay off because Sentry's scrubber only masks exact key names (`token`, `secret`), so a local called `token_secret` or a `user` whose repr is the username would ship with the traceback.

**Every engine helper that touches user data takes the `User` object as its first parameter.** Examples: `load_overrides(user)`, `derive_track_result(user, …)`, `build_bpm_lookup(user, releases)`, `releases_by_ids(user, ids)`. Adding a new helper? Follow the pattern. The user instance is the tenant boundary.

**Background jobs use `enqueue_*` helpers + `UserJobLock`.** Views POST to `/actions/<thing>`, the action view calls `enqueue_discogs_sync(user, ...)` or `enqueue_bpm_cascade(user, ...)` from `webapp/records/jobs.py`, which atomically acquires a `UserJobLock` row (OneToOne with user) and hands the task to `async_task`. **One running job per user**; a second click while one's in-flight returns HTTP 409. The worker's `finally` calls `UserJobLock.mark_done(user, summary)` or `.mark_failed(user, err)` so the toast can show the result for ~5s before lazy-deletion.

**Never delete a `UserJobLock` row while its worker is still running.** The Q2 worker holds the cascade state in memory (subprocess); deleting the lock from the outside leaves that worker orphaned but still consuming sources, AND lets `acquire()` succeed for a new click → two cascades fighting over the same `BpmCache` rows and `progress_text` column. The smoke-test that taught us this is documented in commit history. If you need to stop a running worker, restart `sleeve-notes web` (kills the subprocess); the lock then releases on next acquire.

**The toast is the universal job-status surface.** Fixed bottom-right, lives in `#run-banner` (base.html), polls `/run/banner` every 2s. Three states: running (blue determinate bar + live progress text), done (green bar + summary), failed (red bar + error). New batch actions get this for free as long as they go through `enqueue_*` and write progress via `records.jobs._make_progress_logger(user)`.

**Beatport is currently disabled.** The original SQLite kv-store kept Beatport's OAuth tokens; the Django port hasn't re-implemented that yet. The cascade silently skips Beatport — see `sleeve_notes/fetch_bpm.py:_load_beatport_tokens`. The other four sources (songbpm, Deezer, ReccoBeats/Spotify, AcousticBrainz) work normally.

## Web UI conventions

**Goal-oriented, not pipeline-oriented.** The CLI thinks in pipeline steps (fetch → bpm → render). The UI never names a task after its internals: step 2 says "41 of 54 tracks have a BPM" and offers the lookup as the consequence, not "run the BPM cascade". Collection-wide jobs that don't belong to one set (Discogs sync, CSV import, whole-library BPM lookup) live as named actions with tailored modals under Home's Library heading. If a new long-running CLI subcommand lands, add it as a named action there — not as an option in a generic dropdown.

**There is no sidebar.** It was removed when the flow landed. Outside the flow there is a slim top bar: wordmark (→ Home) + breadcrumb on the left, username + Sign out on the right. Inside the flow the top bar is replaced by the step rail (`templates/flow/_base.html` overrides `{% block chrome %}`) — four steps and one **Save & close**, nothing else. A visible nav mid-set invites wandering off, which is exactly what the old editor allowed. Both bars render only when `user.is_authenticated`; logged-out visitors get the `/` landing page with no chrome.

**The flow is the product; Home is the launcher.** Making sleeve labels is a four-step flow (`print_runs/flow.py`): Records → BPM → Design → Get them. There is no separate wizard and no one-page editor — the run *is* the flow. Home (`/`, `core.views.home`) carries exactly one primary action, the recent sets, and the Library. It is explicitly NOT a metrics dashboard; resist re-adding KPI cards.

**Drafts are the unit of resumability.** Entering the flow creates a `PrintRun` with `status='draft'`; `step` records how far the user got and the flow reopens there. One draft per user — `flow.start` reuses the open one. **Only `saved` runs count as printed**: `already_printed_ids` and `last_print_timestamp` filter through `saved_runs()`. If you add a query over print history, go through those helpers, or an abandoned draft will silently mark records as done.

**A job inside the flow renders in the step, not the toast.** Step 2 shows the cascade's progress inline and polls `/labels/<id>/bpm/status` every 2s *only while running*. The toast still exists for when the user navigates away. The status partial deliberately excludes the overrides table below it — swapping a table the user is typing into would wipe their work, so completion offers an explicit "Show updated list" instead.

**Step 3's checkboxes are the single source of truth.** The three style presets are client-side bulk-sets over the nine content toggles (`PRESETS` is shipped to the template via `json_script` so the two can't drift). The server re-derives which chip is lit with `active_preset()`. Never make a preset a separate persisted field.

**Toast progress text gets prettified.** `_prettify` in `records/jobs.py` strips the cascade's `[N/M] ●● artist - title -> 128 key=8A sources=[...]` down to `(N/M) artist - title` and writes `progress_done`/`progress_total` for the determinate progress bar. New job types should emit `[done/total]` somewhere in their log lines so the bar fills.

**After completion, no auto-refresh.** Older versions of the legacy app issued `htmx.ajax(GET, current_url, ...)` after a job; the current Django port doesn't. The user manually refreshes to see new data. (Adding it back is a future polish — would live in the toast partial's done-state.)

**Engine reuse, not duplication.** Views that need data the CLI also computes — sticker layout, BPM lookup, render — call the same module in `sleeve_notes/` plus the service wrapper in `webapp/<app>/services/`. Concrete example: the sticker preview in the `/collection` detail drawer reuses `print_runs.services.render.releases_by_ids` + `build_bpm_lookup` + `sleeve_notes.preview.render_release_stickers_svg` — exactly what the PDF endpoint uses.

**Consistency across listing pages.** New listing pages mirror `/collection`'s affordances: search input is the FIRST element (same shape: `field h-8 px-3`), filter selects/chips right after, count + primary action at `ml-auto`. When a page already has a POST form (e.g. `/tracks`' big Save), use an empty sibling `<form id="..." method="get">` and bind the GET-only inputs to it via the HTML5 `form="..."` attribute so everything stays on one row without nesting forms.

**Rename a feature → update every surface in one pass.** A label change is never just one screen. Also update the page title (`{% block title %}`), breadcrumb (`{% block breadcrumb %}`), Home, cross-page links, and README references. Note the deliberate split the "print run" → "sleeve labels" rename left behind: the **UI** says sleeve labels everywhere, while the **CLI flags and the model/table stay `PrintRun` / `--print-run-id` / `--mark-printed`**. Don't "finish" that rename in the CLI without deciding to break those flags. Partial renames produce confusing UIs where one screen says "Records" but the page header still says "Collection". Grep before declaring done.

**Visual style.**
- **`DESIGN.md` is the design system — read it before any UI work.** Hardware-grey housing, near-black display, one orange LED (`--led`) that only means active/look-here. Light, calm, functional (Operate mode). Tokens live in `base.html` `:root`; `PRODUCT.md` holds product truth for the Impeccable skill (`.claude/skills/impeccable/`, run `/impeccable <command>`). Keep new UI to these tokens and the `.t-good/.t-warn/.t-bad`, `.link`, `.row-hover`, `.row-selected` helpers — no raw Tailwind palette colours (`text-emerald-400` etc.).
- Deliberately not used, because they read as generated UI: eyebrow/kicker labels above headings (use `.section-title`), uppercase tracked table headers, glow or gradient surfaces, backdrop blur/glass, numbered circle badges, unicode glyphs as icons (draw an SVG), fully rounded pills, text under 12px.
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

## Challenge ideas — always

The user has explicitly asked to be challenged on ideas, every time, not only when they ask for an opinion. Agreeing by default is not helpful here.

When the user proposes a feature, approach or technology, check it before building it or writing it down:
- **Does it already exist?** Grep first. An idea that is already (partly) in the code should be answered with what's there and what's actually missing (e.g. `BpmCache` already covered "cache BPMs once fetched"; the new part was sharing across users).
- **Does it conflict with a recorded decision?** The `## Uitgangspunten` at the top of `roadmap.md` (minimal customer data, the fixed Django stack) and the conventions in this file win unless the user decides otherwise.
- **Is there a cheaper or lower-maintenance variant?** Say which, and why.
- **What's the failure mode?** Especially anything that can silently put a wrong value on a printed sticker.

How to challenge: state the counterargument concretely and briefly, give a recommendation, and keep going — don't block the work on it. When capturing the idea in `roadmap.md`, still capture it, and put the counterargument next to it (a `*Challenge*` / `*Tegenargument*` line or sub-section) so it isn't re-debated from scratch later. If the user reaffirms the idea after hearing the challenge, that's their call: build it.

## Before pushing to GitHub

Whenever I ask you to push (`push`, `push to <branch>`, `ship it`, etc.) — do NOT just `git push` the pending diff. First do a thorough sweep of the repo to confirm everything still matches reality, then commit any fixes alongside the push:

- **README.md** especially — feature labels, page names, screenshot text, CLI flags, command examples. UI renames are the #1 source of stale docs.
- **CLAUDE.md (this file)** — conventions you've added or invalidated this session.
- **CLI `--help` strings + the corresponding README snippets** — they should agree.
- **In-code comments and docstrings** referencing renamed/removed concepts.

If anything is out of date, fix it in the same push (separate commit is fine). If you're unsure whether a doc update is needed, ask before pushing — it's cheaper than a follow-up "you forgot to update X" cycle.
