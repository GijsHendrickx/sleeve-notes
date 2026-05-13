# Agent Instructions

You're working on **Sleeve Notes** — a personal Discogs → printable-stickers app. It's a fully-fledged application: a Python engine (`sleeve_notes/`) exposed both as a CLI (`sleeve-notes`) and a FastAPI + HTMX web UI (`sleeve_notes_web/`), with all state in a single SQLite file (`data/sleeve_notes.db`).

The README is the authoritative user-facing doc. CLAUDE.md is for *you* — codebase conventions that aren't obvious from reading the code, and traps to avoid that have already cost time once.

## Architecture in one paragraph

The engine in `sleeve_notes/` is the source of truth: deterministic Python that talks to Discogs, runs the 5-source BPM cascade, manages overrides, and renders the PDF. The CLI (`sleeve_notes/cli.py`) is a thin dispatcher over those modules. The web UI is a thin presentation layer on top of the same engine — for any data the CLI computes, the web UI calls the same functions in-process; for long-running jobs, it shells out to a `sleeve-notes <subcommand>` subprocess. **Never fork engine logic into web-only helpers.**

## Directory layout

```
.tmp/             # Disposable: stickers.pdf output, debug HTML dumps. Wipe freely.
data/             # NOT disposable: sleeve_notes.db (collection, BPM cache, overrides, print history).
sleeve_notes/     # Engine (Python package). CLI subcommands dispatch into these modules.
sleeve_notes_web/ # FastAPI + HTMX + Jinja web UI. See "Web UI conventions" below.
.env              # API keys (Discogs, Spotify, Beatport). NEVER commit. NEVER store secrets elsewhere.
```

## Web UI conventions

The web UI lives in `sleeve_notes_web/` (FastAPI + HTMX + Jinja, launched via `sleeve-notes web`). It is a thin presentation layer on top of the same CLI engine. These conventions have been hard-won — please don't rediscover them.

**Action-oriented, not pipeline-oriented.** The CLI thinks in pipeline steps (fetch → bpm → render). The web UI does NOT expose that abstraction. Each long-running task is a discrete sidebar action with a tailored modal — "Discogs sync", "Import Discogs csv", "BPM lookup", "Generate stickers" — never a generic "step picker + freeform args" panel. If a new long-running CLI subcommand lands, add it as a named action, not as an option in a dropdown.

**Sidebar shape.** Two labelled sections in this order: **Collection** (Records `/collection`, Tracks `/tracks`) and **Actions** (the named jobs, including Generate stickers). The Dashboard is the homepage (`/`) reached via the "Sleeve Notes" wordmark click; it is NOT a sidebar item. The wordmark is title-cased ("Sleeve Notes", not "sleeve-notes"), larger than nav items, with only the app version underneath — no host name.

**Background jobs go through `JobRunner`.** Web routes that trigger work shell out to a `sleeve-notes <subcommand>` subprocess via `services.jobs.runner` — they do NOT call engine code in-process. One job runs at a time; the start endpoint refuses a new job while one is in flight. `runner.cancel()` MUST escalate SIGTERM → SIGKILL via a background watchdog (~3s grace), because the BPM cascade has non-daemon worker threads that block subprocess exit when stuck in network I/O.

**Job status is a toast, not a console.** A fixed bottom-right toast with an indeterminate sliding progress bar — not a slide-out panel and not a console scrollback. The toast:
- Title is a friendly label ("BPM lookup", "Discogs sync") derived from `_job_title(job)` — never the raw `job.kind`.
- Body grows to fit; never `line-clamp`. Long messages just push the bottom down.
- Body reads like product copy. Filter command-echo lines (`$ /…/python -m sleeve_notes.cli …`), per-source progress tallies (`progress: songbpm=3 …`), and final source summaries (`new this run by source: …`). Rewrite technical sentences like "Cache populated: 421/445 BPMs (…)" to "Found BPM for 421 of 445 tracks (…)". The filter/rewrite logic lives in both `routes/run.py` (`_filter_line`, `_prettify_line`) and `static/app.js` (`prettifyLine`) — keep them in sync.

**After any terminal status, refresh in place.** On `done` / `cancelled` / `failed`, `app.js` issues `htmx.ajax(GET, current_url, {target:"main", select:"main > *", swap:"innerHTML"})`. Cancelled and failed jobs can still have written partial DB state (some releases imported, some BPMs cached) so they're treated the same as success. Never `window.location.reload()` — it kills toast, sidebar focus, and scroll position.

**Static-asset caching.** Static files are served via `NoCacheStaticFiles` (`Cache-Control: no-store`). Browsers heuristically cache JS aggressively, and stale `app.js` after a refactor produces baffling UI bugs (the new template renders correctly because Jinja re-reads every request, but the cached JS targets DOM nodes that no longer exist). The `app.js?v=N` query param in `base.html` is a belt-and-suspenders cache-buster — bump it whenever app.js changes substantively.

**Engine reuse, not duplication.** Web routes that need data the CLI also computes — sticker layout, BPM lookup, render — call the same modules in `sleeve_notes/`. Example: the sticker preview in the collection-detail drawer uses the same `render_release_stickers_svg` as `/preview`, ensuring printed PDF and on-screen preview stay in lockstep. Never fork engine logic into web-only helpers.

**Restart `sleeve-notes web` after Python changes.** The server is launched without `--reload` — anything you touch under `sleeve_notes_web/routes/`, `sleeve_notes_web/services/`, or `sleeve_notes/` is invisible to the running process until restart. Jinja templates (`templates/**/*.html`) DO live-reload because FastAPI re-reads them per request, so template-only tweaks land in place. After a Python edit, kill the existing process (`kill $(lsof -i :8765 -t)`) and relaunch — otherwise the next request 404s/500s for stale-code reasons that look like bugs.

**Rename a feature → update every surface in one pass.** A label change is never just the sidebar. Also update the page title (`{% block title %}`), breadcrumb (`{% block breadcrumb %}`), dashboard cards, cross-page links, and README references. Partial renames produce confusing UIs where the sidebar says "Records" but the page header still says "Collection". Grep the repo for the old name before declaring done.

**Consistency across listing pages.** New listing pages mirror the affordances of the Records page (`/collection`): a single filter row at the top with the search input as the FIRST element (same shape: `field h-8 px-3`), filter chips/selects right after, count + primary action at `ml-auto`. Don't re-invent the filter row per page. When a page already has a POST form (e.g. /tracks' big Save), use an empty sibling `<form id="..." method="get">` and bind the GET-only inputs to it via the HTML5 `form="..."` attribute so everything stays on one row without nesting forms.

**Visual style.**
- No greyed-out text in actionable buttons — it reads as disabled. `.btn-quiet` uses `--ink` for text; reserve `--dim` for decorative glyphs inside buttons or genuinely inactive states.
- No italics on song or release titles.
- No abbreviated column headers, except `BPM` (universally abbreviated). `Position` over `Pos`, `Format` over `Fmt`, `Duration` over `Dur`.
- Form controls placed on the same row share an explicit height (`h-8` for `.field` inputs/selects/buttons). Native `<select>` adds invisible vertical chrome that doesn't match `<input>` even with identical Tailwind padding classes.
- Don't show redundant information in detail panels (e.g. an explicit ID immediately above a link that already encodes the same ID).
- Table rows that mix multi-line text (e.g. release-artist + release-title stacked) with single-line inputs use `align-middle`, NOT `align-top`. Top-align makes the inputs in adjacent cells look misaligned next to the multi-line text.
- Confirm async-feeling actions visually, even when the result is a no-op. The `/tracks` per-row sync button takes 5–10 s to refetch from all 5 sources, and the displayed BPM rarely changes — without the `.flash-sync` cell animation on the response render, the action looks broken when the value is unchanged. Apply the same principle to any new per-row mutation.

## Engine conventions

**Engine modules in `sleeve_notes/` are reusable.** They're imported by both the CLI dispatcher (`cli.py`) and the web routes (`sleeve_notes_web/routes/*`). Avoid `print(...)` in library functions — return values, use `logging`, or accept a `progress_callback`. The job toast parses CLI stdout, but in-process callers shouldn't have to.

**State migrations live in `db.py`.** When you change a schema, add a migration (idempotent `ALTER TABLE` / `CREATE` guarded by an introspection check) — never assume a clean DB. Existing users have years of cache. See `db.py` for the established pattern (`if "<column>" not in existing: …`).

**`SLEEVE_NOTES_ROOT` is the root-resolution hook.** All paths (`data/`, `.tmp/`, `.env`) are resolved relative to `project_root()` in `sleeve_notes/__init__.py`, which respects the `SLEEVE_NOTES_ROOT` env var. Don't hardcode `Path.cwd()` or `__file__`-relative paths in engine code — break this and every test/install path gets weird.

**Every long-running step must be idempotent and resumable.** The DB caches (`releases.raw_tracklist IS NOT NULL`, `bpm_cache.sources_tried`, `print_runs`) exist so a `Ctrl-C` mid-run loses at most a handful of seconds. Commit to the DB at coarse intervals (fetch: every 25 releases; bpm: every 5 cascade runs) — never per row (too slow) and never only at the end (loses everything on interrupt).

## Before pushing to GitHub

Whenever I ask you to push (`push`, `push to <branch>`, `ship it`, etc.) — do NOT just `git push` the pending diff. First do a thorough sweep of the repo to confirm everything still matches reality, then commit any fixes alongside the push:

- **README.md** especially — feature labels, sidebar/page names, screenshot text, CLI flags, command examples. UI renames are the #1 source of stale docs.
- **CLAUDE.md (this file)** — conventions you've added or invalidated this session.
- **CLI `--help` strings + the corresponding README snippets** — they should agree.
- **In-code comments and docstrings** referencing renamed/removed concepts.

If anything is out of date, fix it in the same push (separate commit is fine). If you're unsure whether a doc update is needed, ask before pushing — it's cheaper than a follow-up "you forgot to update X" cycle.
