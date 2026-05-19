# Sleeve Notes

Generate a printable A4 PDF with one sticker per record from your Discogs collection. Each sticker lists every track on the release, grouped by side (A / B / …), with **position, artist, title, duration, musical key (Camelot) and BPM**, plus a **QR code** linking to the Discogs release page and the **playback RPM** (33⅓ / 45) when Discogs lists it. Sticker size, the grid on A4, edge-to-edge tile vs gutters + crop marks, and which fields appear on each sticker (artist / title / RPM / BPM / key / duration / Discogs QR / side labels) are all configurable per print run — defaults give you 96 × 50.8 mm (two per row on A4), perfect for the sleeve of a 12".

The web UI manages **print runs** — each run is a saved "which releases + which layout settings" recipe you can re-download, Duplicate as the starting point for the next print, or delete. Collection views filter by `type` (Album / EP / Single / Compilation / Other) and `format` (12" / 10" / 7" / Other), both auto-classified from Discogs metadata at fetch time.

BPM and key are looked up by firing 5 sources in **parallel per track** (songbpm + Deezer + ReccoBeats/Spotify + Beatport + AcousticBrainz) and reconciling them by consensus. When two or more sources agree on a BPM (within ±1), the sticker prints a small filled dot **●** before the digits — your "trust this blind" signal. Single-source or disputed hits get just the digits. Tracks where no source returned a BPM get an **empty box** for a hand-written needle-drop value.

---

## Table of contents
- [How it works (high level)](#how-it-works-high-level)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Credentials & `.env`](#credentials--env)
- [Running the workflow](#running-the-workflow)
- [Web UI](#web-ui)
- [Inspecting the data: `query` + `overrides`](#inspecting-the-data-query--overrides)
- [Outputs](#outputs)
- [The CSV-export shortcut](#the-csv-export-shortcut)
- [Resumability, caches and re-runs](#resumability-caches-and-re-runs)
- [BPM cascade details](#bpm-cascade-details)
- [Manual overrides](#manual-overrides)
- [Beatport: one-time auth](#beatport-one-time-auth)
- [Printing](#printing)
- [Edge cases](#edge-cases)
- [Failure modes & troubleshooting](#failure-modes--troubleshooting)
- [Project layout](#project-layout)

---

## How it works (high level)

```
┌────────────────────────────┐
│ Discogs collection         │
│  (API  OR  CSV export)     │
└────────────┬───────────────┘
             │  sleeve-notes fetch   →  Release + Track rows
             ▼
        ┌──────────────────────────┐
        │   Postgres (Django ORM)  │   ← one DB, Django models in `webapp/`
        │   ─────────────────      │
        │   Release / Track        │
        │   BpmCache               │
        │   Override               │
        │   PrintRun               │
        └────────────┬─────────────┘
             │  sleeve-notes fetch   →  ingests releases + tracks in one pass
             │  sleeve-notes bpm     →  cascade fills BpmCache
             │  sleeve-notes render  →  ad-hoc PDF, or replay a saved print run
             │                          via `--print-run-id <uuid>`
             ▼
   .tmp/stickers.pdf          ← print this on A4, 100% scale
```

All persistent state — the Discogs collection, the BPM cache, your manual
overrides, the print history — lives in a single Postgres database
managed by the Django app in `webapp/`. `.tmp/` still holds the final
`stickers.pdf` (and any debug dumps), but nothing it contains is
load-bearing — wipe it freely.

Each step is re-runnable and **incremental**: re-runs only do new work
because every cache lives in the DB.

---

## Prerequisites

- **Python 3.10+** (the codebase uses `str | None` PEP 604 syntax)
- **macOS, Linux or WSL** — the tools are pure Python, but the workflow has been exercised on macOS
- A **Discogs account with a public collection** *or* a Discogs CSV export of your collection
- A printer that can print **A4 at 100% scale** (no "fit to page")

Optional, only if you want maximum BPM coverage:
- Free **Spotify developer app** (Client Credentials, used by the ReccoBeats source)
- A **Beatport account** (extra editorial BPMs for house/techno/disco/electro)

---

## Setup

Two paths — the `sleeve-notes` CLI installed via pipx (recommended for end users) or a local virtualenv (for hacking on the code):

```bash
# 1. Clone & enter
git clone <this-repo> vinyl-sleeve-notes
cd vinyl-sleeve-notes

# 2a. Recommended: install the CLI in an isolated env via pipx
pipx install .                 # exposes `sleeve-notes` on $PATH
# 2b. Or, for development:
python3 -m venv .venv
source .venv/bin/activate
pip install -e .               # editable install; sleeve-notes + sleeve_notes/ both work

# 3. Create your .env
cp .env.example .env   # then fill in the values — see next section

# 4. Start a local Postgres (Docker) — the Django app writes here
docker compose up -d db
```

After install, the CLI is available as `sleeve-notes`. All commands resolve `.tmp/`, `.env`, and the Postgres connection from `DATABASE_URL` against the project root.

**First run** apply migrations once: `cd webapp && python manage.py migrate`. Then sign in via the web UI (`sleeve-notes web`) to create your user row; the per-user tables (collection, BPM cache, print runs) fill up as you sync.

---

## Credentials & `.env`

All secrets live in `.env` in the repo root. **Never commit this file** — it is already gitignored. A `.env.example` ships with every required key documented.

Sleeve Notes uses **"Sign in with Discogs" (OAuth 1.0a)** as its login system: there's no separate account, no password. Discogs access tokens are stored encrypted-at-rest in Django via `allauth.SocialToken`, so background jobs can call the API on your behalf without you needing to be at the keyboard. The server needs the Discogs *consumer* credentials (identifying the app to Discogs) plus Django's own bootstrap config in `.env`; everything else is optional infrastructure for the BPM cascade.

### Minimal `.env`

```dotenv
# --- Discogs OAuth (required) -----------------------------------------
# Register a developer app and paste its consumer credentials below.
DISCOGS_CONSUMER_KEY=...
DISCOGS_CONSUMER_SECRET=...

# --- Django config (required) -----------------------------------------
# Long random string. Generate with:
#   python -c "import secrets; print(secrets.token_urlsafe(64))"
DJANGO_SECRET_KEY=...

# Local Postgres URL — `docker compose up -d db` brings up this database.
DATABASE_URL=postgres://sleevenotes:dev@localhost:5432/sleevenotes

# Dev convenience: run the Django-Q2 worker on a daemon thread inside
# runserver so sidebar action buttons work in one terminal.
DEV_INPROCESS_QCLUSTER=true
```

That's enough to start the web app, sign in with your Discogs account and sync your collection. The BPM cascade still works at reduced coverage without the optional creds below — only sources whose creds are present will be queried.

### Full `.env`, every BPM source enabled

```dotenv
# (Discogs OAuth + session — see above)

# --- Spotify (for the ReccoBeats source) ------------------------------
# Free app: https://developer.spotify.com/dashboard → "Create app".
# Redirect URI can be anything (we use Client Credentials, no user OAuth).
# App-wide: one set of creds serves every Sleeve Notes user.
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...

# --- Beatport (for the Beatport v4 source) ----------------------------
# Same email + password you use to log in at beatport.com. App-wide.
BEATPORT_USERNAME=...
BEATPORT_PASSWORD=...

# --- YouTube (per-track "listen" icon, YouTube fallback) --------------
# Free key, ~100 first-clicks per day before quota runs out. Skip if you
# don't care about the YouTube fallback — the icon then opens
# youtube.com/results instead of resolving to a specific video.
YOUTUBE_API_KEY=...

```

**What happens if a source is missing creds?** The tool prints a notice and skips that source for affected tracks. The cascade simply tries the next source. You can add creds later and re-run — only the previously-skipped source will be retried (see [Resumability](#resumability-caches-and-re-runs)).

### Where to get the Discogs OAuth credentials

1. Log in at https://www.discogs.com
2. Visit **Settings → Developers → Create an Application**
3. Set the **Callback URL** to:
   - `http://127.0.0.1:8000/accounts/discogs/login/callback/` for local dev
   - `https://<your-host>/accounts/discogs/login/callback/` for a production deploy
4. Copy the **Consumer Key** and **Consumer Secret** into `DISCOGS_CONSUMER_KEY` / `DISCOGS_CONSUMER_SECRET`.

Each end-user who signs in goes through the standard Discogs OAuth authorize flow in their browser — no extra developer accounts on their side.

### Where to get Spotify creds

1. https://developer.spotify.com/dashboard → log in
2. **Create app** — name and description are arbitrary; redirect URI is **not** used by this project (we use the server-to-server Client Credentials flow), but Spotify still requires one to be set — `http://localhost/` is fine
3. Copy the **Client ID** and **Client Secret** into `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`

### Where to get Beatport creds

Just the email + password you normally use to log in at beatport.com. No developer account or app registration needed — this project uses Beatport's public Swagger client_id and a fully-scripted OAuth flow (no browser). See [Beatport: one-time auth](#beatport-one-time-auth).

### Standalone CLI use (advanced)

The web UI is the normal entry point: signing in once persists your Discogs access token + secret in allauth's `SocialToken` table, and all subsequent CLI subcommands resolve the active user via `DISCOGS_USER_ID` (your numeric Discogs id, find it under the Django admin or via `sleeve-notes query users.User`). Set it once in `.env` and you can run `sleeve-notes fetch / bpm / render` directly. The OAuth tokens themselves are read from the DB, not the env — you only need `DISCOGS_USER_ID` to pick the right row.

---

## Running the workflow

The three steps are exposed as `sleeve-notes` subcommands (the legacy `python sleeve_notes/*.py` invocations still work — they call the same `main()` functions):

```bash
sleeve-notes fetch     # Step 1 — Discogs collection (API or --csv); ingests
                       # releases + tracks in one pass
sleeve-notes bpm       # Step 2 — parallel cascade for BPM + key
sleeve-notes render    # Step 3 — generate the PDF
sleeve-notes run       # all three, with --csv/--folder forwarded to fetch,
                       # render flags forwarded to render (see below)
```

Every step is idempotent — re-running picks up where the last left off. Pass `-h` to any subcommand for its own flags. The sections below show both forms (`sleeve-notes <cmd>` and `python sleeve_notes/<script>.py`) so you can mix and match.

### Step 1 — fetch your collection

```bash
# Whole collection, via the Discogs collection API
python sleeve_notes/fetch_discogs_collection.py

# Only one folder (case-insensitive match by name, or folder id)
python sleeve_notes/fetch_discogs_collection.py --folder "DJ"

# Use a Discogs CSV export instead of the collection API (recommended for first runs)
python sleeve_notes/fetch_discogs_collection.py --csv path/to/your-discogs-export.csv

# CSV + folder filter (matches against the CSV's CollectionFolder column)
python sleeve_notes/fetch_discogs_collection.py --csv path/to/your-discogs-export.csv --folder "DJ"

# Debug: stop after N releases
python sleeve_notes/fetch_discogs_collection.py --limit 10
```

- **Runtime:** roughly **1.1 s per release** (Discogs allows 60 authenticated req/min). 500 releases ≈ 10 minutes. Every request is OAuth-signed with the signed-in user's token.
- **Output:** rows in `releases` (`basic_information`, `raw_tracklist`, `notes` JSON columns plus the normalized `artist`/`title`/`year`/`rpm`/`type`/`format` columns) and rows in `tracks`. The fetch step now owns both — normalization happens inline. Inspect via `sleeve-notes query releases`.
- **Resumable:** rows with `raw_tracklist IS NOT NULL` are served from the DB, so re-runs after an interruption finish quickly.

See [The CSV-export shortcut](#the-csv-export-shortcut) for why `--csv` is often the easier path.

### Step 2 — look up BPMs

```bash
sleeve-notes bpm                  # default: 8 cross-track worker threads
sleeve-notes bpm --workers 12     # bump if you're bandwidth-rich
```

- Iterates over every track in the `tracks` table. A pool of worker threads (`--workers 8` by default) processes tracks in parallel; inside each worker, all 5 sources fire in parallel and are reconciled via consensus (BPM cluster ±1; key by exact-match majority). See [BPM cascade details](#bpm-cascade-details).
- Output: rows in `bpm_cache` (one per track, with `sources_tried` array) plus rows in `bpm_source_hits` (one per source-that-returned-something). Per-track consensus is derived on the fly by the renderer — no `bpm_results.json` needed.
- Beatport is opt-in (see [Beatport: one-time auth](#beatport-one-time-auth)).

Typical runtimes — bounded by the slowest per-host rate limit shared across workers (MusicBrainz and SongBPM, ~1 req/s each):
- **First run, all sources configured:** ~12 tracks/min sustained (≈ 35 min for a 445-track collection).
- **Cached re-run:** seconds for the whole collection — `sources_tried` makes the cascade no-op for fully-cached tracks.
- Bumping `--workers` past ~8 gives diminishing returns; the bottleneck is the global SongBPM / MB rate, not local concurrency.

### Step 3 — generate the PDF

```bash
sleeve-notes render                              # default 96 x 50.8 mm with gutters + crop marks
sleeve-notes render --sticker-w 70 --sticker-h 40    # smaller stickers, more per page
sleeve-notes render --sticker-w 140 --sticker-h 80   # bigger stickers, fewer per page
sleeve-notes render --tile                       # edge-to-edge: exactly 10 stickers per A4, slice with 5 ruler cuts
sleeve-notes render --tile --tile-cols 3 --tile-rows 4    # 12-per-A4 tile (70 x 74.2 mm)
sleeve-notes render --new-only                   # only releases not yet in the print history
sleeve-notes render --new-only --mark-printed    # render new + save as a new print run (releases + settings)
sleeve-notes render --print-run-id <uuid>         # re-render a previously-saved print run (UUID — get it from `/print-runs/` or `sleeve-notes query print_runs.PrintRun`)
sleeve-notes render -o ~/Desktop/crate-2026-05.pdf   # custom output path
sleeve-notes render -o out/                       # custom dir; filename defaults to stickers.pdf
```

- Output: `.tmp/stickers.pdf` by default. Override with `-o/--output <path>` — either a full PDF path, or a directory (filename falls back to `stickers.pdf`). Parent directories are created as needed; a `.pdf` suffix is appended if missing.
- **Default mode** ships with crop marks and a 4 mm gutter between stickers, sized via `--sticker-w` / `--sticker-h` (mm). Columns and rows per page are auto-derived from the size so as many stickers as possible fit. Sizes that don't fit on A4 are rejected.
- **Tile mode (`--tile`)** lays stickers edge-to-edge with zero gutters and zero page margin so the print can be sliced with just a few straight ruler cuts (`(cols − 1) + (rows − 1)` total). Sticker size is derived from `--tile-cols` × `--tile-rows` (default 2×5 = 10 per A4 → 105 × 59.4 mm). `--sticker-w` / `--sticker-h` are ignored when `--tile` is set. **Print borderless** or expect ~3 mm clipping on the outer stickers (most home printers have a small unprintable margin).
- Fonts auto-shrink to keep all text inside the sticker margins. The BPM number is rendered ~50 % larger than the track text and scales together with it, so a sticker that needs to fit 7–8 tracks shrinks the BPM proportionally — never overlapping the line above.
- **Per-row columns** (left → right): position, artist + title, duration, Camelot key, BPM. The BPM column prepends a small **●** dot only for `high`-confidence values (≥2 sources agreed within ±1 BPM) and manual overrides. Looser-confidence values (`octave`, `shared`, `single`, `disputed`) show just the digits — see [BPM cascade details](#bpm-cascade-details). Tracks without any BPM hit show an **empty rectangle** for a hand-written value.
- **Header**: the artist line shows the playback **RPM** (`33⅓` / `45`) in grey when Discogs lists it. Releases where the Discogs `formats[*].descriptions` array doesn't include an RPM string simply have no badge — about 30–40 % of community-submitted releases.
- **QR code** sits 1 mm from the top-right corner of each sticker (14 × 14 mm, ~0.5 mm modules) and links to `https://www.discogs.com/release/<id>`. Scan it from the sleeve to jump straight to the Discogs page.
- The console output reports the sticker count, pages used, the chosen mm size and the auto-derived grid (e.g. `2x5 grid edge-to-edge (tile mode)`), plus the exact number of straight cuts you need to make per page when in tile mode.

**Incremental printing (`--new-only` / `--mark-printed`):** you won't reprint a 500-record crate every month — you'll print the 8 records you bought last week. Print history lives in the `print_runs` + `print_run_releases` tables. Each `print_runs` row stores a timestamp, an optional name and the layout/content settings used (`settings_json`); the join table records the release IDs that were on it. `--new-only` filters the render to releases that don't appear in any prior print run; `--mark-printed` saves the rendered set + the current CLI settings as a new print run. Typical workflow:

```bash
# First time: print everything, save it as a print run
sleeve-notes render --mark-printed

# Later, after adding new records to Discogs and re-fetching:
sleeve-notes render --new-only                  # preview new arrivals
sleeve-notes render --new-only --mark-printed   # ready to print → save as a new print run

# Re-render exactly what was on a previous run (settings and all):
sleeve-notes render --print-run-id <uuid> -o /tmp/reprint.pdf
```

`--print-run-id` is mutually exclusive with `--new-only` / `--mark-printed` — the saved run already pins both the release set and the settings. Releases that drop out of your Discogs collection later are ignored when re-rendering a saved run — the IDs are kept but lossily skipped at render time.

Inspect history with `sleeve-notes query print_runs` and
`sleeve-notes query print_run_releases`.

---

## Web UI

If you'd rather not live in the terminal, every step above is also exposed
through a localhost web app. Launch it with:

```bash
sleeve-notes web                 # http://127.0.0.1:8000, auto-opens your browser
sleeve-notes web --port 9000     # change port
sleeve-notes web --no-browser    # bind only, don't pop a tab
```

The app is **Django + Tailwind + HTMX** on top of the same engine. Background jobs (Discogs sync, BPM cascade, CSV import) run through Django-Q2; with `DEV_INPROCESS_QCLUSTER=true` in `.env` the Q2 cluster runs on a daemon thread inside `runserver`, so one terminal is all you need. Caches, overrides and print history are identical whether you drive things from the terminal or the browser.

Visiting `/` while signed out shows a minimal landing page with a single **Sign in with Discogs** button — it kicks off the OAuth 1.0a dance via django-allauth. On return Discogs's user id + username are written into the Django `users.User` row and the access token + secret are persisted in allauth's `SocialToken` so background jobs can call the API on your behalf. Sign out via the **Sign out** link in the sidebar footer.

Once signed in, the sidebar splits into two sections — **Collection** (data views) and **Actions** (one-shot jobs that shell out to the CLI). The Dashboard is the homepage at `/`, reached by clicking the "Sleeve Notes" wordmark in the top-left.

| Surface | What it's for |
|---|---|
| **Dashboard** (`/`) | At-a-glance BPM coverage (high / octave / shared / single / disputed / missing), count of releases new since the last print, quick-action buttons. |
| **Records** (`/collection`) | Searchable, filterable table of every release (artist, title, year, type, format, BPM coverage). Click a row to inspect tracks, BPM sources and key in a side drawer that also renders an inline SVG sticker preview at the actual print size. |
| **Tracks** (`/tracks`) | Per-track table with text search (artist/title), filter chips (All / Without BPM / Has override), and inline editing of manual BPM / key / note overrides. Each row has a **▶** listen icon (Spotify-green when we have a Spotify ID, YouTube-red otherwise) that opens the track in a new tab, plus a **↻** sync button that re-fetches BPM/key for just that track from all 5 sources; the BPM and Key cells briefly flash blue when the request returns, so the user gets confirmation even when the value didn't change. The same listen icon also appears in the per-release drawer's tracklist on `/collection`. |
| **Print runs** (`/print-runs/`) | Manage print runs as saved {releases + settings} recipes. The list shows every past run with size + a quick `PDF` download button. The editor (`/print-runs/new`) is a single-page release picker: full release table with checkboxes (pre-checked on releases not yet in any print run), client-side search, and **Select new only** / **Select all** / **Clear** quick actions. Save lands you on the detail page where you can hit **Download PDF**. Settings UI (sticker size, tile mode, content toggles) is deferred — runs created from the browser use the defaults; use the CLI for fine-tuned layouts. |
| **Discogs sync** action | Modal-driven `sleeve-notes fetch` via the Discogs API. Optional folder name + result-limit. |
| **Import Discogs csv** action | Modal-driven `sleeve-notes fetch --csv …` against a CSV export uploaded from your machine. Optional folder filter. |
| **BPM lookup** action | Modal-driven `sleeve-notes bpm`. One option: **Force re-fetch all** — wipes the BPM cache before running, so every track is re-queried. |

**Job feedback.** Sidebar actions enqueue a Django-Q2 task; the worker writes progress into a per-user `UserJobLock` row that the toast polls every 2s. Three states map to three visual styles: running (blue determinate bar + `(N/M) artist - title` live), done (green bar + summary), failed (red bar + error). The toast lingers ~5s after completion, then vanishes. One running job per user is enforced at the DB layer — a second click while one's in-flight returns HTTP 409 and a flash on the modal.

State lives in Postgres (locally via `docker compose up -d db`) plus `.tmp/` for ad-hoc PDF output. The web app and the CLI are two doors into the same Django ORM; mix and match freely.

**CLI-only commands.** A few subcommands are not (yet) surfaced in the web UI and have to be run from the terminal:

- `sleeve-notes query …` — Django model listing + first-N row dump (see [Inspecting the data](#inspecting-the-data-query--overrides)). For richer browsing use Django admin at `/admin/`.
- `sleeve-notes overrides clear --yes` — batch-clear every manual override. Single-row add/remove/edit lives in `/tracks`.

---

## Inspecting the data: `query` + `overrides`

All persistent state is in Postgres, managed by the Django app. Two layers
of inspection:

```bash
# CLI: list every Django model with its row count
sleeve-notes query

# CLI: dump a model's first N rows
sleeve-notes query records.Release --limit 5
sleeve-notes query records.Track --limit 20

# For richer browsing — filtering, sorting, foreign-key traversal — use the
# Django admin in your browser:
#   sleeve-notes web   →  http://127.0.0.1:8000/admin/

# For arbitrary SQL: drop into Django's dbshell (Postgres psql under the hood).
cd webapp && python manage.py dbshell
```

Manual overrides — the only table you typically need to edit by hand —
get a dedicated subcommand:

```bash
# Precise: a single track on a specific release
sleeve-notes overrides add --release-id 123 --position A1 --bpm 128 --key 8A

# Broad: every track with this artist+title (matches the BPM cascade's hash)
sleeve-notes overrides add --artist "Daft Punk" --title "Around the World" --bpm 121 --key Am

sleeve-notes overrides list
sleeve-notes overrides remove <id>
sleeve-notes overrides clear --yes
```

Or just edit them inline on `/tracks` — every row has BPM / Key / Note input cells with a single batched **Save** button.

See [Manual overrides](#manual-overrides) for the full schema.

---

## Outputs

After a complete run, the project layout looks like:

| Location | Purpose |
|----------|---------|
| **Postgres database** | **All persistent state** — your Discogs collection, the BPM cache, your overrides, your print history. Connection via `DATABASE_URL` in `.env`. See `sleeve-notes query` or Django admin for inspection. |
| `.tmp/stickers.pdf` | The default printable PDF location (override with `sleeve-notes render -o <path>`). |
| `.tmp/uploads/*.csv` | Holding pen for browser-uploaded CSV exports; deleted by the worker after import. |
| `.tmp/debug/*.html` | (Only on songbpm parser failures.) Raw HTML dumped for selector repair. |

`.tmp/` is entirely **disposable** — wipe it freely.
Your Postgres database is **not** disposable: it contains your overrides
and print history. Back it up like any other source of truth (`pg_dump`
locally, Render's automatic dailies in production).

The Django models in summary:

| Model | What's in it |
|-------|--------------|
| `users.User` | One row per Discogs user that has signed in. `discogs_user_id` is the Discogs id; `username` mirrors Discogs. allauth's `SocialToken` (a separate table) holds the OAuth1 access token + secret. |
| `records.Release` | One row per (user, Discogs release). `basic_information` + `raw_tracklist` are JSONFields (per-release cache); `artist`, `title`, `rpm`, `release_type`, `format` are the normalized fields populated by sync via `sleeve_notes.ingest`. UUID PK. |
| `records.Track` | Normalized track rows. Populated by sync alongside the parent release. UUID PK. |
| `records.BpmCache` | One row per (user, artist+title hash). Tracks which sources have been queried in `sources_tried` and the raw per-source hits in `source_hits` (JSONField). |
| `records.Override` | Manual BPM/key overrides — either precise (`release` + `position`) or broad (`artist` + `title`). Per-user. |
| `print_runs.PrintRun` | First-class print runs: name + `settings` JSONField + `release_ids` array. Backs the CLI `--new-only` / `--mark-printed` / `--print-run-id` flags and the `/print-runs/` UI. UUID PK. |
| `core.UserJobLock` | OneToOne with user; enforces "one running background job per user". State + progress text + result message. The toast reads this. |
| `audit.AuditEvent` | Append-only history of user actions. Future-proofing for print-fulfillment orders. |

---

## The CSV-export shortcut

Discogs lets you export your collection as a CSV: **Collection → Export → "CSV" (without recommendations)**. The file looks like `yourname-collection-YYYYMMDD-HHMM.csv` and contains one row per release with columns including `release_id` and `CollectionFolder`.

**Why use `--csv` over the API?**
- Deterministic snapshot of your collection at export time (handy when running on a machine that does not have your Discogs creds).
- Lets you hand the export and the repo to a friend so they can print their own stickers without sharing tokens.
- The collection-listing API call is skipped (the CSV provides the release_ids directly), so first-time syncs of large collections start their tracklist fetch immediately.

**Why the per-release API is still hit:** the CSV does not contain tracklists, and tracklists are essential for the sticker rows. So Step 1 always fetches `/releases/{id}` per release (unless cached) — only the *collection listing* is bypassed.

Folder filtering in `--csv` mode matches `--folder "NAME"` case-insensitively against the CSV's `CollectionFolder` column. Folder **ids** (`--folder 0`) are rejected in CSV mode because the CSV has no ids. If you pass an unknown folder name, the tool prints the available folders from the CSV.

---

## Resumability, caches and re-runs

Every cache lives in Postgres so you can interrupt and resume safely.

- **`Release.raw_tracklist`** — the per-release Discogs detail. Rows where this is non-NULL are served from the DB and never re-fetched.
- **`BpmCache`** — one row per (user, artist+title hash). `sources_tried` lists which sources have been queried; `source_hits` (JSONField) holds the raw `{bpm, key_camelot, score, url}` per source. Re-runs only call sources that haven't been queried for that track; consensus is recomputed on the fly. Per-user.
- **Adding a source later:** if you add `SPOTIFY_CLIENT_ID` after a previous run already queried the other sources, those entries are automatically re-cascaded **only for the newly-available source** on the next run.
- **Re-running steps:** `fetch` is cache-aware (rows with a stored `raw_tracklist` skip the network call but still re-normalize). `bpm` and `render` are cache-aware too.
- **Commit granularity:** sync commits per release; cascade saves each track's cache row inside the thread pool. `Ctrl-C` mid-run loses at most one track's worth of work.

To start a step clean, drop the relevant rows — e.g. for a fresh BPM
lookup:

```bash
cd webapp && python manage.py dbshell <<< "DELETE FROM records_bpmcache;"
sleeve-notes bpm
```

Or just trigger the "BPM lookup" action with the **Force refetch all**
checkbox in the browser — same effect.

To start completely fresh: drop and re-create the Postgres database
(`docker compose down -v && docker compose up -d db && cd webapp &&
python manage.py migrate`).
Your overrides go too — export them first via Django admin if needed.

---

## BPM cascade details

`sleeve_notes/fetch_bpm.py` runs the cascade with **two levels of `ThreadPoolExecutor`**: an outer pool (`--workers 8` by default) processes tracks in parallel, and inside each worker an inner pool fires **all five sources in parallel per track**, then reconciles them by consensus. Each source can contribute BPM, musical key, or both — independently:

| Source | BPM | Key | Notes |
|---|:---:|:---:|---|
| **songbpm.com** | ✓ | ✓ | HTML scrape of canonical detail pages. No auth, 1 req/s. Strong general coverage. |
| **Deezer** | ✓ | — | Public `api.deezer.com/track` endpoint. No auth, ~4 req/s. `bpm: 0` = "known but not analysed" — counted as a miss. |
| **ReccoBeats** | ✓ | ✓ | Drop-in replacement for the deprecated Spotify audio-features endpoint. Returns `key` (pitch class 0–11) and `mode` (0=minor / 1=major). Requires `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` for the name→ID step (no user OAuth). Coverage = "everything on Spotify" — limited for pre-Spotify vinyl-only releases. The Spotify track ID matched here is also persisted onto `tracks.spotify_track_id` so the web UI's listen icon can deep-link straight into Spotify. |
| **Beatport v4** | ✓ | ✓ | Editorial BPM + key supplied by the labels themselves. Uses the public Swagger client_id; tokens stored in `kv['beatport_tokens']` (see next section). |
| **AcousticBrainz** | ✓ | ✓ | Open dataset reached via MusicBrainz recording IDs. Frozen since 2022 but excellent for older electronic releases. |

**Validation:** every hit is fuzzy-matched against artist + title (rapidfuzz, min score 70). This stops a same-titled but unrelated track from being accepted by mistake. Plausibility window: 50 ≤ BPM ≤ 250.

**Retry behaviour:** HTTP calls use a tight `tenacity` retry (`wait_exponential(1, 5)` × 2 attempts). A flaky source costs at most ~5 s — the cascade then falls through to the other four, so one slow API can't stall the run.

**Consensus** (per track, in this order):
- **Strict cluster** — values within ±1 BPM. ≥3 sources agreeing wins outright (`high` confidence, ● dot on the sticker).
- **Octave-aware cluster** — fold each value into a canonical [80–160] range (halving/doubling as needed), cluster with ±2 tolerance, pick the cluster with the most distinct sources. Beats a strict pair of 2 when 3+ sources agree once half/double-time is normalised. Catches the dominant failure mode of algorithmic BPM detection. Confidence = `octave`.
- **Strict pair** — fall back to the ≥2-source strict cluster if octave didn't beat it. Confidence = `high`.
- **Shared-URL flag** — if all winning sources have a URL shared by ≥3 distinct cache_keys (the "remix collapse" pattern, where the matcher returned the base track for every remix variant), the BPM value is kept but confidence is downgraded to `shared` (yellow in the UI) — verify before trusting.
- **Priority fallback** — none of the above produced a cluster: pick the highest-priority source (`songbpm > reccobeats > deezer > acousticbrainz > beatport`, ordered by empirical outlier rate against the rest of the cascade). Confidence = `disputed`.
- **Single** — only one source returned anything. Confidence = `single`.
- **Key** — exact-Camelot-string majority across sources. All keys are normalised to Camelot before comparison (Beatport's `camelot_number`/`camelot_letter` fields are used directly; Spotify-style pitch class + mode is mapped; AcousticBrainz `tonal.key_key`/`tonal.key_scale` is parsed; songbpm.com is best-effort).
- **Manual override** — rows in the `overrides` table always win over the consensus (see next section).

**Cache schema:** `bpm_cache` holds one row per (artist, title) hash with `sources_tried` (JSON array); `bpm_source_hits` holds one row per (cache_key, source) with the raw `bpm`, `key_camelot`, `score`, `url`. Inspecting any of them is one `sleeve-notes query` away:

```bash
sleeve-notes query --sql "
  SELECT c.artist, c.title, GROUP_CONCAT(h.source || '=' || h.bpm) AS hits
  FROM bpm_cache c JOIN bpm_source_hits h ON h.cache_key = c.cache_key
  WHERE h.bpm IS NOT NULL
  GROUP BY c.cache_key HAVING COUNT(*) > 1 LIMIT 10"
```

**Rate limits enforced:**
| Source | Limit |
|---|---|
| songbpm.com | ~1 req/s + Mozilla User-Agent |
| Deezer | ~4 req/s |
| Spotify | ~5 req/s |
| ReccoBeats | ~7 req/s |
| Beatport | 2 req/s |
| MusicBrainz | 1 req/s (strict — `User-Agent` must include contact info; configured already) |
| Discogs | 60/min authenticated, 25/min unauthenticated |

---

## Manual overrides

When a source returns the wrong BPM (e.g. picked up a remix, picked up a same-titled but unrelated track), or you have a needle-dropped value you trust over any online source, add it via the `overrides` subcommand (or the `/tracks` web UI — same data, point-and-click). Overrides win over the cache and the entire cascade — they're checked **before** anything else for a given track.

```bash
# Precise: one specific track on one release
sleeve-notes overrides add --release-id 123456 --position A1 --bpm 128 --key 8A

# Broader: every track with this artist+title in the collection
sleeve-notes overrides add --artist "Daft Punk" --title "Around the World" --bpm 121 --key Am

sleeve-notes overrides list
sleeve-notes overrides remove 3
```

Two ways to address a track:

- **`--release-id` + `--position`** — most precise. Both are printed on the sticker itself, so an override is a one-command edit after a needle-drop.
- **`--artist` + `--title`** — broader. Matches every track in the collection whose normalized artist+title hash to the same key. Useful when the same track appears on multiple releases.
- `release_id` + `position` takes priority when an override of each shape matches the same track.

Per-entry fields:

| Flag | Meaning |
|---|---|
| `--bpm <int>` (50–250) | Override the BPM. Sticker shows the digits with a **●** dot (manual values are treated as fully trusted). |
| `--key <str>` | Override the Camelot key. Accepts Camelot (`8A`, `12B`), musical (`Am`, `C#m`, `F# major`), or slash notation (`F♯/G♭ Major`); all are normalised to Camelot. |
| `--note <str>` | Free-text reminder for yourself. Ignored by the renderer. |

Re-run `sleeve-notes bpm` (or `sleeve-notes render` if the cache is already populated) after editing — overrides are applied on the fly. They never poison the cache, so removing an override later restores the previously-cached value (or sends the track back through the cascade if it was never cached).

**Validation:** parsing errors, out-of-range BPMs, and entries that match no track in your collection are reported as warnings — never fatal. The unmatched-entry warning is the one to watch for: a typo'd `--release-id` produces no override and would otherwise be silent.

**Bulk imports:** if you have a list of needle-drops, the cleanest path is a small shell loop over `sleeve-notes overrides add …`. For direct SQL surgery on an existing set, drop into `cd webapp && python manage.py dbshell` and edit the `records_override` table directly — read-only `sleeve-notes query records.Override` still works either way.

---

## Beatport: one-time auth

> **Currently disabled in the Django port.** The original Beatport flow
> stored tokens in a SQLite kv table that no longer exists, and the
> Selenium-driven auth helper has been removed (`sleeve-notes
> auth-beatport` no longer exists). The cascade silently skips Beatport
> and runs with four sources. Re-enabling it requires porting the
> token storage to a Django model and re-implementing the auth flow —
> tracked as future work.

Once Beatport is re-enabled, the original on-boarding will be: add
`BEATPORT_USERNAME` + `BEATPORT_PASSWORD` to `.env`, run a one-time
auth command to mint tokens, and `sleeve-notes bpm` picks them up
on the next cascade. Underlying flow is the same authorization_code
dance as `beets-beatport4` against the public Swagger client_id
(`0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd`).

---

## Printing

1. Open `.tmp/stickers.pdf` in any PDF reader.
2. Print at **A4, 100% scale**, **no** "fit to page" / "shrink to fit". This is critical: sticker sizes are calibrated to exact millimetres and any scaling will throw them off.
3. After printing one test page, **measure a sticker with a ruler**. If it's not the expected mm size to within a millimetre, the printer is scaling — fix the print settings and try again.
4. Print onto **A4 self-adhesive paper** (any matte sticker paper works).

**Cutting depends on the mode you chose in Step 4:**

- **Default mode** (with gutters and crop marks): cut along the crop marks at each sticker's corners. Two cuts per side of each sticker.
- **Tile mode (`--tile`)**: stickers are edge-to-edge so you only need straight cuts that span the whole sheet — for the 2 × 5 default that's **1 vertical + 4 horizontal cuts (5 total per page)**. Print **borderless** if your printer supports it; otherwise the outer ~3 mm of the outermost stickers will be clipped by the printer's unprintable margin. The sticker borders that get drawn along every cut line act as ruler guides — line up a metal ruler with the printed border and slice in one pass.

Tracks for which no BPM could be found get an empty rectangle on the sticker itself in the right-hand column — write the BPM in by hand once you've needle-dropped it.

---

## Edge cases

| Situation | Behaviour |
|---|---|
| Various Artists / compilation | Release header reads `V/A — <title>`; per-track artists come from the tracklist. |
| Position `A` / `B` with no subnumber | Treated as one track on that side, position preserved literally. |
| Non-ASCII characters (Björk, é, ø, …) | Matching uses NFKD normalisation; the PDF preserves the originals. |
| Multiple SongBPM remix hits | The variant whose title matches the mix suffix wins; otherwise the shortest title (the original mix). |
| Missing tracklist on Discogs | Release row gets stored without any `tracks` entries — it's still in your collection but won't appear on the sticker sheet (renderer skips releases with no tracks). |

---

## Failure modes & troubleshooting

| Symptom | What to do |
|---|---|
| `DISCOGS_USER_ID must be set` from a CLI subcommand | Set `DISCOGS_USER_ID=<your numeric Discogs id>` in `.env`. Find it via `sleeve-notes query users.User` or the Django admin once you've signed in via the web UI. |
| `No Discogs OAuth token found for this user` | You've never signed in via the browser (allauth hasn't created a SocialToken row), or you signed in before `SOCIALACCOUNT_STORE_TOKENS` was enabled. Sign out and back in via the web UI. |
| `ERROR: set DISCOGS_CONSUMER_KEY and DISCOGS_CONSUMER_SECRET in .env` | Your Discogs **app** isn't registered or its consumer credentials aren't in `.env`. Register an application at https://www.discogs.com/settings/developers, set its Callback URL to `<host>/accounts/discogs/login/callback/`, and paste the consumer key/secret. |
| `DJANGO_SECRET_KEY must be set` | Generate one with `python -c "import secrets; print(secrets.token_urlsafe(64))"` and put it in `.env`. |
| `Folder 'X' not found` | Pass an existing folder name (case-insensitive). In CSV mode the tool prints the folders present in the CSV; in API mode it prints the folders on your Discogs account. |
| `401 Unauthorized` from Discogs | Your OAuth access token was revoked on the Discogs side. Sign out and back in to refresh. |
| `401 Unauthorized` from Spotify | Spotify app credentials wrong or expired. Re-issue and update `.env`. |
| `401 Unauthorized` from Beatport during BPM lookup | Beatport is currently disabled in the Django port (see [Beatport: one-time auth](#beatport-one-time-auth)). If you somehow get this error anyway, it means the auth flow needs re-implementation. |
| HTTP 429 (rate limited) | `tenacity` retries with exponential backoff. Persistent 429s usually mean a misconfigured rate-limit — wait a minute, or lower concurrency with `sleeve-notes bpm --workers 4` and try again. |
| Release shows in `/collection` but is skipped by the renderer | Discogs returned no tracklist for it. List the affected releases with: `sleeve-notes query releases --cols "id,artist,title,type,format" --where "NOT EXISTS (SELECT 1 FROM tracks t WHERE t.release_id = releases.id)"`. Usually a Discogs submission gap — adding the tracklist on discogs.com and re-running `sleeve-notes fetch` for that release fixes it. |
| SongBPM parser returns nothing (HTML changed) | The tool dumps the offending HTML into `.tmp/debug/<slug>.html`. Open it, find the new BPM markup, adjust the selectors in `sleeve_notes/fetch_bpm.py`, drop the affected cache rows (`cd webapp && python manage.py dbshell <<< "DELETE FROM records_bpmcache WHERE source_hits ? 'songbpm';"`), and re-run `sleeve-notes bpm`. |
| Sticker text overflows / looks wrong on a release | Fonts auto-shrink to fit, but for releases with very long titles or many tracks per side, results can still get tight. Inspect via `sleeve-notes query tracks --where "release_id = X"` and confirm the data is correct first; the PDF is a faithful render of that data. If you chose very small stickers via `--sticker-w/--sticker-h`, try a larger size — vertical space is the limiting factor for >6-track sides. |
| `Sticker WxH mm doesn't fit on A4` | The requested `--sticker-w` / `--sticker-h` leaves no room for the 4 mm page edge on A4. Pick a smaller size, or stick to the default 96 × 50.8 mm. |

---

## Project layout

```
.
├── README.md                              ← you are here
├── CLAUDE.md                              ← codebase conventions for AI agents working on the repo
├── RUNBOOK.md                             ← incident response + pause/resume Render staging
├── migration_plan.md                      ← the Django port plan (mostly historical now)
├── pyproject.toml                         ← packaging + `sleeve-notes` entry point
├── docker-compose.yml                     ← local Postgres
├── render.yaml                            ← Render Blueprint (web + worker + db)
├── .env                                   ← your secrets (gitignored)
├── sleeve_notes/                          ← pure engine package (no DB code, no Django imports)
│   ├── __init__.py                        ← package marker + project_root() helper
│   ├── cli.py                             ← `sleeve-notes` subcommand dispatcher
│   ├── django_setup.py                    ← Django bootstrap shim for the delegators
│   ├── fetch_discogs_collection.py        ← delegator → manage.py sync_discogs
│   ├── fetch_bpm.py                       ← Step 2 pure cascade (HTTP + parsing + consensus)
│   ├── generate_sticker_pdf.py            ← Step 3 pure ReportLab adapter (PdfDrawer + delegator)
│   ├── preview.py                         ← SvgDrawer (pure SVG adapter) for the detail drawer + previews
│   ├── sticker_layout.py                  ← geometry, fonts, draw_sticker — the Drawer protocol
│   ├── ingest.py                          ← per-release normalize: extract fields + tracks
│   ├── classify.py                        ← derive `type` and `format` from Discogs metadata
│   ├── query.py                           ← delegator → manage.py query
│   └── overrides.py                       ← delegator → manage.py overrides
├── webapp/                                ← Django project
│   ├── manage.py
│   ├── sleevenotes_app/                   ← project settings + root urls
│   ├── core/                              ← UserJobLock + shared abstract models
│   ├── users/                             ← AbstractUser subclass (custom AUTH_USER_MODEL)
│   ├── records/                           ← Release, Track, BpmCache, Override
│   │   ├── models.py
│   │   ├── services/                      ← discogs_sync, bpm_cascade — ORM-bound wrappers around sleeve_notes/
│   │   ├── management/commands/           ← sync_discogs, lookup_bpm, query, overrides — CLI entry points
│   │   ├── jobs.py                        ← enqueue_* + _run_* worker functions for Django-Q2
│   │   ├── action_views.py                ← /actions/<name> POST endpoints (sidebar modals)
│   │   └── views.py                       ← /collection, /collection/<uuid>, /tracks, /tracks/save, /tracks/sync
│   ├── print_runs/                        ← PrintRun + render service + PDF download
│   │   ├── models.py
│   │   ├── services/render.py             ← releases_by_ids, build_bpm_lookup, render_pdf
│   │   ├── management/commands/           ← render_print_run
│   │   └── views.py                       ← /print-runs/, /print-runs/<uuid>, /print-runs/<uuid>/pdf, /print-runs/new
│   ├── audit/                             ← AuditEvent (append-only history)
│   ├── discogs_provider/                  ← custom allauth OAuth1 provider for Discogs
│   ├── templates/                         ← Django templates (base.html + per-page + partials)
│   └── middleware/staging_gate.py         ← HTTP Basic Auth for ENVIRONMENT=staging
└── .tmp/                                  ← disposable cache + final PDF (gitignored)
    ├── stickers.pdf                       ← the only file you actually need to print
    ├── uploads/                           ← browser-uploaded CSV exports, cleaned by the worker after import
    └── debug/                             ← (only on songbpm parser failure) raw HTML
```

---

## License & attribution

Personal project. The Beatport client_id used here is the public one from Beatport's own Swagger UI; the Discogs, Spotify, ReccoBeats, Deezer, MusicBrainz/AcousticBrainz and songbpm endpoints are queried through their documented public interfaces. Be a good citizen and respect their rate limits — the tools enforce them, do not weaken them.
