# Sleeve Notes

Generate a printable A4 PDF with one sticker per record from your Discogs collection. Each sticker lists every track on the release, grouped by side (A / B / …), with **position, artist, title, duration, musical key (Camelot) and BPM**, plus a **QR code** linking to the Discogs release page and the **playback RPM** (33⅓ / 45) when Discogs lists it. Stickers are 96 × 50.8 mm (two per row on A4), perfect for the sleeve of a 12" so you can read everything at a glance while DJing.

Every release in your Discogs collection gets a sticker. The web UI lets you filter the collection by `type` (Album / EP / Single / Compilation / Other) and `format` (12" / 10" / 7" / Other) — both are auto-classified from Discogs metadata at fetch time.

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
             │  sleeve-notes fetch   →  releases.basic_information + raw_tracklist
             ▼
        ┌──────────────────────────┐
        │   data/sleeve_notes.db   │   ← single SQLite file under project root
        │   ─────────────────      │     replaces every JSON intermediate
        │   releases / tracks      │
        │   bpm_cache / source_hits│
        │   overrides              │
        │   print_runs / kv        │
        └────────────┬─────────────┘
             │  sleeve-notes fetch   →  ingests releases + tracks in one pass
             │  sleeve-notes bpm     →  cascade fills bpm_cache + bpm_source_hits
             │  sleeve-notes render  →  derives per-track BPM/key on the fly
             ▼
   .tmp/stickers.pdf          ← print this on A4, 100% scale
```

All persistent state — the Discogs collection, the BPM cache, your manual
overrides, the print history — lives in a single SQLite file
(`data/sleeve_notes.db`) under the project root. `.tmp/` still holds the final
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

# 3. Create your .env (see next section)
cp .env.example .env   # or create it from scratch — see below
```

After install, the CLI is available as `sleeve-notes`. All commands resolve state files (`data/sleeve_notes.db`, `.tmp/`, `.env`) against the current working directory by default — so always run `sleeve-notes` from the directory you want those files to live in. Override with `SLEEVE_NOTES_ROOT=/path/to/project sleeve-notes …` if needed.

**First run** auto-creates `data/sleeve_notes.db` and, if it finds any
legacy JSON files (`collection.json`, `bpm_cache.json`, `release_cache/`,
`overrides.json`, etc.), imports them in a single transaction and renames
the originals to `*.bak`. If you previously had `sleeve_notes.db` at the
project root (from before the `data/` move), it's relocated automatically
on first run. Nothing manual to do — just run the pipeline.

If a `.env.example` is not present, just create `.env` from scratch using the template in the next section.

---

## Credentials & `.env`

All secrets live in `.env` in the repo root. **Never commit this file** — it is already gitignored.

The minimal-but-functional `.env`:

```dotenv
# Discogs — required UNLESS you use --csv with everything in cache
DISCOGS_TOKEN=your_personal_access_token
DISCOGS_USERNAME=your_public_discogs_username
```

The full `.env`, unlocking every BPM source:

```dotenv
# --- Discogs ----------------------------------------------------------
# Required for API mode. In --csv mode: only used for cache misses
# (per-release tracklist fetches). DISCOGS_USERNAME is NEVER needed in --csv mode.
# Token: https://www.discogs.com/settings/developers → "Generate new token"
DISCOGS_TOKEN=...
DISCOGS_USERNAME=...

# --- Spotify (for ReccoBeats source) ----------------------------------
# Free app: https://developer.spotify.com/dashboard → "Create app".
# Redirect URI can be anything (we use Client Credentials, no user OAuth).
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...

# --- Beatport (for Beatport v4 source) --------------------------------
# Same email + password you use to log in at beatport.com.
# Used once by sleeve_notes/beatport_auth.py to mint tokens in .tmp/beatport_tokens.json.
BEATPORT_USERNAME=...
BEATPORT_PASSWORD=...

# --- YouTube (for the per-track "listen" icon, YouTube fallback) ------
# Powers the per-track listen icon on /tracks and the collection drawer
# when we don't have a Spotify track ID. Free key, ~100 first-clicks per
# day before quota runs out. Skip if you don't care about the YouTube
# fallback — the icon then opens youtube.com/results instead of a video.
# https://console.cloud.google.com/apis/credentials — enable "YouTube Data API v3".
YOUTUBE_API_KEY=...
```

**What happens if a source is missing creds?** The tool prints a notice and skips that source for affected tracks. The cascade simply tries the next source. You can add creds later and re-run — only the previously-skipped source will be retried (see [Resumability](#resumability-caches-and-re-runs)).

### Where to get the Discogs token

1. Log in at https://www.discogs.com
2. Visit **Settings → Developers**
3. Click **Generate new token**
4. Paste the token into `DISCOGS_TOKEN`. Set `DISCOGS_USERNAME` to your public Discogs username (the one in the URL of your profile).

### Where to get Spotify creds

1. https://developer.spotify.com/dashboard → log in
2. **Create app** — name and description are arbitrary; redirect URI is **not** used by this project (we use the server-to-server Client Credentials flow), but Spotify still requires one to be set — `http://localhost/` is fine
3. Copy the **Client ID** and **Client Secret** into `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`

### Where to get Beatport creds

Just the email + password you normally use to log in at beatport.com. No developer account or app registration needed — this project uses Beatport's public Swagger client_id and a fully-scripted OAuth flow (no browser). See [Beatport: one-time auth](#beatport-one-time-auth).

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

- **Runtime:** roughly **1.1 s per release** (Discogs allows 60 authenticated req/min). 500 releases ≈ 10 minutes. Unauthenticated CSV mode runs at 2.5 s per release (25 req/min).
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
sleeve-notes render --new-only --mark-printed    # render new + record the print in history
sleeve-notes render -o ~/Desktop/crate-2026-05.pdf   # custom output path
sleeve-notes render -o out/                       # custom dir; filename defaults to stickers.pdf
```

- Output: `.tmp/stickers.pdf` by default. Override with `-o/--output <path>` — either a full PDF path, or a directory (filename falls back to `stickers.pdf`). Parent directories are created as needed; a `.pdf` suffix is appended if missing.
- **Default mode** ships with crop marks and a 4 mm gutter between stickers, sized via `--sticker-w` / `--sticker-h` (mm). Columns and rows per page are auto-derived from the size so as many stickers as possible fit. Sizes that don't fit on A4 are rejected.
- **Tile mode (`--tile`)** lays stickers edge-to-edge with zero gutters and zero page margin so the print can be sliced with just a few straight ruler cuts (`(cols − 1) + (rows − 1)` total). Sticker size is derived from `--tile-cols` × `--tile-rows` (default 2×5 = 10 per A4 → 105 × 59.4 mm). `--sticker-w` / `--sticker-h` are ignored when `--tile` is set. **Print borderless** or expect ~3 mm clipping on the outer stickers (most home printers have a small unprintable margin).
- Fonts auto-shrink to keep all text inside the sticker margins. The BPM number is rendered ~50 % larger than the track text and scales together with it, so a sticker that needs to fit 7–8 tracks shrinks the BPM proportionally — never overlapping the line above.
- **Per-row columns** (left → right): position, artist + title, duration, Camelot key, BPM. The BPM column prepends a small **●** dot when two or more sources agreed (or the value came from a manual override); single-source / disputed hits show just the digits. Tracks without any BPM hit show an **empty rectangle** for a hand-written value.
- **Header**: the artist line shows the playback **RPM** (`33⅓` / `45`) in grey when Discogs lists it. Releases where the Discogs `formats[*].descriptions` array doesn't include an RPM string simply have no badge — about 30–40 % of community-submitted releases.
- **QR code** sits 1 mm from the top-right corner of each sticker (14 × 14 mm, ~0.5 mm modules) and links to `https://www.discogs.com/release/<id>`. Scan it from the sleeve to jump straight to the Discogs page.
- The console output reports the sticker count, pages used, the chosen mm size and the auto-derived grid (e.g. `2x5 grid edge-to-edge (tile mode)`), plus the exact number of straight cuts you need to make per page when in tile mode.

**Incremental printing (`--new-only` / `--mark-printed`):** you won't reprint a 500-record crate every month — you'll print the 8 records you bought last week. The tool keeps a per-print history in the `print_runs` + `print_run_releases` tables (each row: timestamp + the release IDs that were on it). `--new-only` filters the render to releases that don't appear in any prior print run; `--mark-printed` inserts a new `print_runs` row after a successful render. Typical workflow:

```bash
# First time: print everything, mark them all as printed
sleeve-notes render --mark-printed

# Later, after adding new records to Discogs and re-fetching:
sleeve-notes render --new-only                  # preview new arrivals
sleeve-notes render --new-only --mark-printed   # ready to print → commit to history
```

The two flags compose cleanly: alone, `--new-only` just filters; alone, `--mark-printed` marks the entire current collection (handy for marking pre-existing prints as already done). Releases that drop out of your Discogs collection later are ignored — `--new-only` only adds, never removes.

Inspect history with `sleeve-notes query print_runs` and
`sleeve-notes query print_run_releases`.

---

## Web UI

If you'd rather not live in the terminal, every step above is also exposed
through a small localhost web app. Launch it with:

```bash
sleeve-notes web                 # http://127.0.0.1:8765, auto-opens your browser
sleeve-notes web --port 9000     # change port
sleeve-notes web --no-browser    # bind only, don't pop a tab
```

The app is a thin FastAPI + HTMX layer over the same engine — long jobs
shell out to the `sleeve-notes` CLI subcommands you already know, so the
behaviour, caches, overrides and print history are identical whether you
drive things from the terminal or the browser.

The sidebar splits into two sections — **Collection** (data views) and **Actions** (one-shot jobs that shell out to the CLI). The Dashboard is the homepage at `/`, reached by clicking the "Sleeve Notes" wordmark in the top-left.

| Surface | What it's for |
|---|---|
| **Dashboard** (`/`) | At-a-glance BPM coverage (high / single / disputed / missing), count of releases new since the last print, quick-action buttons. |
| **Records** (`/collection`) | Searchable, filterable table of every release (artist, title, year, type, format, BPM coverage). Click a row to inspect tracks, BPM sources and key in a side drawer that also renders an inline SVG sticker preview at the actual print size. |
| **Tracks** (`/tracks`) | Per-track table with text search (artist/title), filter chips (All / Without BPM / Has override), and inline editing of manual BPM / key / note overrides. Each row has a **▶** listen icon (Spotify-green when we have a Spotify ID, YouTube-red otherwise) that opens the track in a new tab, plus a **↻** sync button that re-fetches BPM/key for just that track from all 5 sources; the BPM and Key cells briefly flash blue when the request returns, so the user gets confirmation even when the value didn't change. The same listen icon also appears in the per-release drawer's tracklist on `/collection`. |
| **Generate stickers** (`/preview`) | Live SVG preview of the sticker for any release at the requested mm size. Step through releases with `←` / `→`, then generate the PDF with the same tile / new-only / mark-printed flags as the CLI. |
| **Discogs sync** action | Modal-driven `sleeve-notes fetch` via the Discogs API. Optional folder name + result-limit. |
| **Import Discogs csv** action | Modal-driven `sleeve-notes fetch --csv …` against a CSV export uploaded from your machine. Optional folder filter. |
| **BPM lookup** action | Modal-driven `sleeve-notes bpm`. One option: **Force re-fetch all** — wipes the BPM cache before running, so every track is re-queried. |

**Job feedback.** Sidebar actions kick a CLI subprocess in the background; status appears as a small fixed toast in the bottom-right with a real progress bar (parsed from the CLI's `[done/total]` counters) and a friendly summary line. The toast persists across page navigation, can be cancelled (SIGTERM → SIGKILL escalation after a brief grace period), and on success it triggers an in-place refresh of the current page's table so new releases / updated BPMs show up without a full reload.

State still lives where it always did — `data/sleeve_notes.db` and `.tmp/`. The web app is purely an alternative front-end; you can mix and match it with CLI invocations freely.

**CLI-only commands.** A few subcommands are not surfaced in the web UI and have to be run from the terminal:

- `sleeve-notes auth-beatport` — one-time Beatport OAuth (see [Beatport: one-time auth](#beatport-one-time-auth)).
- `sleeve-notes query …` — ad-hoc SQL / table inspection (see [Inspecting the data](#inspecting-the-data-query--overrides)).
- `sleeve-notes overrides clear --yes` — batch-clear every manual override. Single-row add/remove/edit lives in `/tracks`.

---

## Inspecting the data: `query` + `overrides`

Every JSON file from the old layout is now a table in `data/sleeve_notes.db`.
Two subcommands give you read/write access from the shell:

```bash
# List every table with its row count
sleeve-notes query

# Dump a table (with --where / --order-by / --cols / --limit / --json)
sleeve-notes query releases --limit 5 --cols "id,artist,title,year"
sleeve-notes query tracks --where "release_id = 12345"
sleeve-notes query bpm_source_hits --where "source = 'beatport'" --order-by "bpm DESC"

# Show the CREATE statements for a table (or all of them)
sleeve-notes query --schema releases
sleeve-notes query schema

# Arbitrary read-only SQL (rejected if it's not SELECT / WITH / PRAGMA / EXPLAIN)
sleeve-notes query --sql "SELECT source, COUNT(*) AS hits FROM bpm_source_hits GROUP BY source"
sleeve-notes query --sql "SELECT r.artist, r.title, COUNT(t.position) FROM releases r
                          JOIN tracks t ON t.release_id = r.id GROUP BY r.id ORDER BY 3 DESC LIMIT 5"

# JSON output for piping into jq or another script
sleeve-notes query releases --limit 0 --json | jq '.[].title'
```

Manual overrides — the only table you typically need to edit by hand —
get a dedicated subcommand so you don't have to write SQL:

```bash
# Precise: a single track on a specific release
sleeve-notes overrides add --release-id 123 --position A1 --bpm 128 --key 8A

# Broad: every track with this artist+title (matches the BPM cascade's hash)
sleeve-notes overrides add --artist "Daft Punk" --title "Around the World" --bpm 121 --key Am

sleeve-notes overrides list
sleeve-notes overrides remove 3
sleeve-notes overrides clear --yes
```

See [Manual overrides](#manual-overrides) for the full schema.

---

## Outputs

After a complete run, the project layout looks like:

| Location | Purpose |
|----------|---------|
| **`data/sleeve_notes.db`** | **The single SQLite file containing all state** — your Discogs collection, the BPM cache, your overrides, your print history. See `sleeve-notes query` for inspection. |
| `.tmp/stickers.pdf` | The default printable PDF location (override with `sleeve-notes render -o <path>`). |
| `.tmp/debug/*.html` | (Only on songbpm parser failures.) Raw HTML dumped for selector repair. |

`.tmp/` is entirely **disposable** — wipe it freely; only `stickers.pdf`
ever lives there and that gets rebuilt by `sleeve-notes render`.
`data/sleeve_notes.db` is **not** disposable: it contains your overrides and
print history. Back it up like any other source of truth.

The DB tables in summary:

| Table | What's in it |
|-------|--------------|
| `releases` | One row per Discogs release. `basic_information` + `raw_tracklist` are JSON blobs (and double as the per-release cache); `artist`, `title`, `rpm`, `type`, `format` are the normalized columns populated by `fetch` via `sleeve_notes.ingest.normalize_release`. |
| `tracks` | Normalized track rows. Populated by `fetch` alongside the parent release. |
| `bpm_cache` | One row per (artist, title) hash. Tracks which sources have been queried. |
| `bpm_source_hits` | One row per (cache_key, source) — the raw BPM/key/url returned by each source. |
| `overrides` | Manual BPM/key overrides. Managed via `sleeve-notes overrides`. |
| `print_runs` + `print_run_releases` | Print history for `--new-only` / `--mark-printed`. |
| `kv` | Tiny key/value table — Beatport tokens, schema version. |

---

## The CSV-export shortcut

Discogs lets you export your collection as a CSV: **Collection → Export → "CSV" (without recommendations)**. The file looks like `yourname-collection-YYYYMMDD-HHMM.csv` and contains one row per release with columns including `release_id` and `CollectionFolder`.

**Why use `--csv` over the API?**
- Works **without** `DISCOGS_USERNAME`.
- Works **without `DISCOGS_TOKEN`** if every release is already in your local cache, or if you accept the slower public rate-limit (25 req/min instead of 60). The tool auto-detects which mode you're in and prints the chosen gap and ETA.
- Deterministic snapshot of your collection at export time (handy when running on a machine that does not have your Discogs creds).
- Lets you hand the export and the repo to a friend so they can print their own stickers without sharing tokens.

**Why the per-release API is still hit:** the CSV does not contain tracklists, and tracklists are essential for the sticker rows. So Step 1 always fetches `/releases/{id}` per release (unless cached) — only the *collection listing* is bypassed.

Folder filtering in `--csv` mode matches `--folder "NAME"` case-insensitively against the CSV's `CollectionFolder` column. Folder **ids** (`--folder 0`) are rejected in CSV mode because the CSV has no ids. If you pass an unknown folder name, the tool prints the available folders from the CSV.

---

## Resumability, caches and re-runs

Every cache lives in `data/sleeve_notes.db` so you can interrupt and resume safely.

- **`releases.raw_tracklist`** — the per-release Discogs detail. Rows where this is non-NULL are served from the DB and never re-fetched. This is the equivalent of the old `release_cache/<id>.json` files.
- **`bpm_cache` + `bpm_source_hits`** — one `bpm_cache` row per (artist, title) hash, with `sources_tried` listing which sources have been queried. Each source that returned something gets one `bpm_source_hits` row with the raw `bpm`/`key_camelot`/`url`. Re-runs only call sources that have not yet been queried for that track; consensus is recomputed on the fly so you never re-fetch.
- **Legacy JSON migration:** on first DB creation, any old `.tmp/*.json` files (incl. `release_cache/`) and the root `overrides.json` are imported into the DB in a single transaction; originals are renamed to `*.bak`. The pre-v2 `bpm_cache.json` shape (flat `cache_key → single_source_hit`) is mapped onto v2 by inferring the source from the `source_url`; entries with an unrecognised URL or no BPM/key are dropped (they'll be re-queried on the next `bpm` run).
- **Adding a source later:** if you add `SPOTIFY_CLIENT_ID` after a previous run already queried the other four sources, those entries are automatically re-cascaded **only for the newly-available source** on the next run. Same applies when you authenticate Beatport for the first time.
- **Re-running steps:** `fetch` is cache-aware (rows with a stored `raw_tracklist` skip the network call but still re-normalize). `bpm` and `render` are cache-aware too.
- **Commit granularity:** `fetch` commits to the DB every 25 releases; `bpm` commits cache rows every 5 cascade runs. So `Ctrl-C` mid-run loses at most a handful of seconds' worth of work.

To start a step clean, drop the relevant table — e.g. for a fresh BPM
lookup:

```bash
sqlite3 data/sleeve_notes.db "DELETE FROM bpm_source_hits; DELETE FROM bpm_cache;"
sleeve-notes bpm
```

To start completely fresh: `rm -rf data/` and re-run the pipeline.
Your overrides go too — back up with `sleeve-notes query overrides --limit 0 --json` first if needed.

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

**Consensus** (per track):
- **BPM** — cluster all returned values within ±1 BPM. The largest cluster with **≥2 members** wins, value = median, confidence = `high` (gets the ● dot on the sticker). Only one source returned → confidence = `single` (digits only). Multiple sources, none agreeing → priority-pick (`beatport > songbpm > reccobeats > deezer > acousticbrainz`), confidence = `disputed` (digits only).
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

**Bulk imports:** if you have a list of needle-drops, the cleanest path is a small shell loop over `sleeve-notes overrides add …`. For direct SQL surgery on an existing set, drop into `sqlite3 data/sleeve_notes.db` and edit the `overrides` table directly — read-only `sleeve-notes query overrides` still works either way.

---

## Beatport: one-time auth

Beatport coverage is opt-in. To enable it:

```bash
# 1. Add to .env:
#    BEATPORT_USERNAME=your.beatport.email@example.com
#    BEATPORT_PASSWORD=your-password

# 2. Mint the initial tokens (no browser, no popup):
sleeve-notes auth-beatport
```

This writes the access + refresh tokens to `kv['beatport_tokens']` in the DB. From then on, `sleeve-notes bpm` refreshes access tokens automatically via the refresh token. If the refresh token is ever revoked, the cascade silently falls back to a fresh login using the credentials in `.env` — you never have to touch this again.

Under the hood, the script uses the same fully-scripted authorization_code flow as the `beets-beatport4` plugin: POSTs your creds to `/auth/login/` for a session cookie, GETs `/auth/o/authorize/` to extract the code from the redirect's `Location` header, then exchanges the code at `/auth/o/token/`. The client_id is the public Swagger one (`0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd`), scraped from Beatport's own Swagger UI JS bundle. If Beatport ever rotates it, re-extract from `api.beatport.com/v4/docs/` → `/static/btprt/*.js` (grep for `API_CLIENT_ID`).

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
| `ERROR: set DISCOGS_TOKEN and DISCOGS_USERNAME in .env` | You're in API mode without creds. Add them to `.env`, or use `--csv path/to/export.csv` to bypass the listing API. |
| `ERROR: <N> releases not cached and DISCOGS_TOKEN missing` (CSV mode) | You don't have a token AND the CSV references releases you've never fetched. Either add `DISCOGS_TOKEN` to `.env`, or be patient — without a token the script falls back to the public endpoint at 25 req/min (2.5 s per release). |
| `Folder 'X' not found` | Pass an existing folder name (case-insensitive). In CSV mode the tool prints the folders present in the CSV; in API mode it prints the folders on your Discogs account. |
| `401 Unauthorized` from Discogs/Spotify | Token expired or wrong. Re-issue it and update `.env`. |
| `401 Unauthorized` from Beatport during BPM lookup | Refresh token expired or revoked. `sleeve-notes bpm` retries automatically with a fresh password grant; if that fails, re-run `sleeve-notes auth-beatport`. |
| HTTP 429 (rate limited) | `tenacity` retries with exponential backoff. Persistent 429s usually mean a misconfigured rate-limit — wait a minute, or lower concurrency with `sleeve-notes bpm --workers 4` and try again. |
| Release shows in `/collection` but is skipped by the renderer | Discogs returned no tracklist for it. List the affected releases with: `sleeve-notes query releases --cols "id,artist,title,type,format" --where "NOT EXISTS (SELECT 1 FROM tracks t WHERE t.release_id = releases.id)"`. Usually a Discogs submission gap — adding the tracklist on discogs.com and re-running `sleeve-notes fetch` for that release fixes it. |
| SongBPM parser returns nothing (HTML changed) | The tool dumps the offending HTML into `.tmp/debug/<slug>.html`. Open it, find the new BPM markup, adjust the selectors in `sleeve_notes/fetch_bpm.py`, drop the affected rows with `sqlite3 data/sleeve_notes.db "DELETE FROM bpm_source_hits WHERE source='songbpm'; DELETE FROM bpm_cache WHERE cache_key NOT IN (SELECT cache_key FROM bpm_source_hits);"` (or just wipe both tables), and re-run `sleeve-notes bpm`. |
| Sticker text overflows / looks wrong on a release | Fonts auto-shrink to fit, but for releases with very long titles or many tracks per side, results can still get tight. Inspect via `sleeve-notes query tracks --where "release_id = X"` and confirm the data is correct first; the PDF is a faithful render of that data. If you chose very small stickers via `--sticker-w/--sticker-h`, try a larger size — vertical space is the limiting factor for >6-track sides. |
| `Sticker WxH mm doesn't fit on A4` | The requested `--sticker-w` / `--sticker-h` leaves no room for the 4 mm page edge on A4. Pick a smaller size, or stick to the default 96 × 50.8 mm. |

---

## Project layout

```
.
├── README.md                              ← you are here
├── CLAUDE.md                              ← codebase conventions for AI agents working on the repo
├── pyproject.toml                         ← packaging + `sleeve-notes` entry point
├── requirements.txt                       ← Python deps (for `pip install -r` mode)
├── .env                                   ← your secrets (gitignored)
├── data/                                  ← all persistent state (gitignored)
│   └── sleeve_notes.db                    ← single SQLite file: collection, cache, overrides, print history
├── overrides.example.json                 ← legacy JSON shape for reference only
├── sleeve_notes/
│   ├── __init__.py                        ← package marker + project_root() helper
│   ├── cli.py                             ← `sleeve-notes` subcommand dispatcher
│   ├── db.py                              ← SQLite schema + connection + JSON migration
│   ├── fetch_discogs_collection.py        ← Step 1: collect releases (API or CSV)
│   ├── ingest.py                          ← per-release normalize: extract fields + tracks
│   ├── classify.py                        ← derive `type` and `format` from Discogs metadata
│   ├── fetch_bpm.py                       ← Step 2: 5-source BPM cascade
│   ├── generate_sticker_pdf.py            ← Step 3: render the PDF
│   ├── query.py                           ← `sleeve-notes query` (browse the DB)
│   ├── overrides.py                       ← `sleeve-notes overrides` (manage overrides)
│   └── beatport_auth.py                   ← one-time Beatport OAuth bootstrap
├── sleeve_notes_web/                      ← localhost web UI (FastAPI + HTMX)
│   ├── app.py                             ← `sleeve-notes web` entry point
│   ├── routes/                            ← collection / tracks / preview / run / dashboard / actions handlers
│   ├── services/                          ← thin wrappers that call into sleeve_notes/* engine code
│   ├── templates/                         ← Jinja templates (base.html + per-page partials)
│   └── static/                            ← app.js + assets served at /static
└── .tmp/                                  ← disposable cache + final PDF (gitignored)
    ├── stickers.pdf                       ← the only file you actually need to print
    └── debug/                             ← (only on songbpm parser failure) raw HTML
```

---

## License & attribution

Personal project. The Beatport client_id used here is the public one from Beatport's own Swagger UI; the Discogs, Spotify, ReccoBeats, Deezer, MusicBrainz/AcousticBrainz and songbpm endpoints are queried through their documented public interfaces. Be a good citizen and respect their rate limits — the tools enforce them, do not weaken them.
