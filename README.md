# Vinyl BPM Stickers

Generate a printable A4 PDF with one sticker per record from your Discogs collection. Each sticker lists every track on the release, grouped by side (A / B / …), with **position, artist, title, duration and BPM**. Stickers are 96 × 50.8 mm (two per row on A4), perfect for the sleeve of a 12" so you can read the BPM at a glance while DJing.

Filter is conservative: **only 12"/LP vinyl** is kept (7"/10"/CD/cassette/digital are skipped). Genre is **not** filtered — every 12"/LP in the collection produces a sticker.

BPMs are looked up through a 5-source cascade (songbpm → Deezer → ReccoBeats/Spotify → Beatport → AcousticBrainz). For tracks where no BPM is found, the sticker shows an **empty box** where the BPM digits would have gone, so you can pen the value in by hand after a needle-drop.

---

## Table of contents
- [How it works (high level)](#how-it-works-high-level)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Credentials & `.env`](#credentials--env)
- [Running the workflow](#running-the-workflow)
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
             │   tools/fetch_discogs_collection.py
             ▼
   .tmp/collection.json       (+ per-release cache in .tmp/release_cache/)
             │
             │   tools/filter_dj_releases.py
             ▼
   .tmp/dj_releases.json      (.tmp/skipped.json for rejects)
             │
             │   tools/fetch_bpm.py   (5-source cascade)
             ▼
   .tmp/bpm_results.json      (+ persistent .tmp/bpm_cache.json)
             │
             │   tools/generate_sticker_pdf.py
             ▼
   .tmp/stickers.pdf          ← print this on A4, 100% scale
```

Each step writes intermediates into `.tmp/` and **can be re-run independently**: caches mean re-runs only do new work.

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

```bash
# 1. Clone & enter
git clone <this-repo> vinyl-bpm-stickers
cd vinyl-bpm-stickers

# 2. Create a virtualenv (strongly recommended)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Create your .env (see next section)
cp .env.example .env   # or create it from scratch — see below
```

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
# Used once by tools/beatport_auth.py to mint tokens in .tmp/beatport_tokens.json.
BEATPORT_USERNAME=...
BEATPORT_PASSWORD=...
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

Activate your virtualenv first (`source .venv/bin/activate`), then run the four steps in order. Every step is idempotent — re-running picks up where the last left off.

### Step 1 — fetch your collection

```bash
# Whole collection, via the Discogs collection API
python tools/fetch_discogs_collection.py

# Only one folder (case-insensitive match by name, or folder id)
python tools/fetch_discogs_collection.py --folder "DJ"

# Use a Discogs CSV export instead of the collection API (recommended for first runs)
python tools/fetch_discogs_collection.py --csv path/to/your-discogs-export.csv

# CSV + folder filter (matches against the CSV's CollectionFolder column)
python tools/fetch_discogs_collection.py --csv path/to/your-discogs-export.csv --folder "DJ"

# Debug: stop after N releases
python tools/fetch_discogs_collection.py --limit 10
```

- **Runtime:** roughly **1.1 s per release** (Discogs allows 60 authenticated req/min). 500 releases ≈ 10 minutes. Unauthenticated CSV mode runs at 2.5 s per release (25 req/min).
- **Output:** `.tmp/collection.json` plus a per-release cache at `.tmp/release_cache/<release_id>.json`.
- **Resumable:** cached releases are served from disk, so re-runs after an interruption finish quickly.

See [The CSV-export shortcut](#the-csv-export-shortcut) for why `--csv` is often the easier path.

### Step 2 — filter to DJ-usable vinyl

```bash
python tools/filter_dj_releases.py
```

- Pure local transformation, no network.
- Output: `.tmp/dj_releases.json` (the keepers) and `.tmp/skipped.json` (with a reason per skipped release).
- Filter rule: a release is kept iff at least one of its format **descriptions** contains `12"` or `LP`. Discogs assigns these per release, so 12" Maxi/Single/EP and LP albums all come through; 7"/10"/CD/cassette/digital drop out.
- No genre filter — every 12"/LP in the collection survives this step.

Review `.tmp/skipped.json` if a record you expected is missing.

### Step 3 — look up BPMs

```bash
python tools/fetch_bpm.py
```

- Iterates over every track in `dj_releases.json`, asking 5 sources in order until one returns a validated hit. See [BPM cascade details](#bpm-cascade-details).
- Output: `.tmp/bpm_results.json` (the lookup results used by the PDF), plus a persistent `.tmp/bpm_cache.json` so the next run is fast.
- Beatport is opt-in (see [Beatport: one-time auth](#beatport-one-time-auth)).

Typical runtimes (rough, depending on cache state and how many sources are configured):
- First run, **no** Spotify/Beatport creds: ~0.5–1 s per track
- First run, **all** sources configured: ~1–2 s per track in the worst case (a few sources need to be queried per miss)
- Cached re-run: a few seconds for the whole collection

### Step 4 — generate the PDF

```bash
python tools/generate_sticker_pdf.py                           # default 96 x 50.8 mm with gutters + crop marks
python tools/generate_sticker_pdf.py --sticker-w 70 --sticker-h 40   # smaller stickers, more per page
python tools/generate_sticker_pdf.py --sticker-w 140 --sticker-h 80  # bigger stickers, fewer per page
python tools/generate_sticker_pdf.py --tile                    # edge-to-edge: exactly 10 stickers per A4, slice with 5 ruler cuts
python tools/generate_sticker_pdf.py --tile --tile-cols 3 --tile-rows 4   # 12-per-A4 tile (70 x 74.2 mm)
```

- Output: `.tmp/stickers.pdf`.
- **Default mode** ships with crop marks and a 4 mm gutter between stickers, sized via `--sticker-w` / `--sticker-h` (mm). Columns and rows per page are auto-derived from the size so as many stickers as possible fit. Sizes that don't fit on A4 are rejected.
- **Tile mode (`--tile`)** lays stickers edge-to-edge with zero gutters and zero page margin so the print can be sliced with just a few straight ruler cuts (`(cols − 1) + (rows − 1)` total). Sticker size is derived from `--tile-cols` × `--tile-rows` (default 2×5 = 10 per A4 → 105 × 59.4 mm). `--sticker-w` / `--sticker-h` are ignored when `--tile` is set. **Print borderless** or expect ~3 mm clipping on the outer stickers (most home printers have a small unprintable margin).
- Fonts auto-shrink to keep all text inside the sticker margins. The BPM number is rendered ~50 % larger than the track text and scales together with it, so a sticker that needs to fit 7–8 tracks shrinks the BPM proportionally — never overlapping the line above.
- Tracks without a BPM hit get a small **empty rectangle** drawn in the BPM column on the sticker itself — pen the value in by hand after a needle-drop. No separate fill-in pages are added.
- The console output reports the sticker count, pages used, the chosen mm size and the auto-derived grid (e.g. `2x5 grid edge-to-edge (tile mode)`), plus the exact number of straight cuts you need to make per page when in tile mode.

---

## Outputs

After a complete run, `.tmp/` contains:

| File | Purpose |
|------|---------|
| `collection.json` | Full release list with merged tracklists |
| `release_cache/<id>.json` | One file per fetched release — persistent cache |
| `dj_releases.json` | Releases that survived the 12"/LP filter |
| `skipped.json` | Releases that were filtered out, with reason |
| `bpm_results.json` | BPM lookup result per track (used by the PDF) |
| `bpm_cache.json` | Persistent BPM cache, tracks which sources were tried |
| `beatport_tokens.json` | Beatport access + refresh tokens (auto-refreshed) |
| **`stickers.pdf`** | **The final printable PDF** |

`.tmp/` is entirely **disposable** — delete it and re-run the steps and you will get the same result (slower the first time, because the caches need to repopulate).

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

Every cache is designed so you can interrupt and resume safely.

- **`.tmp/release_cache/<id>.json`** — the raw `/releases/{id}` JSON. Existing files are served from disk and never re-fetched.
- **`.tmp/bpm_cache.json`** — one entry per track key. Each entry tracks `sources_tried`, so re-runs only call sources that have not yet been queried for that track. When all five have been tried, the entry is marked `exhausted: true` and is no longer re-queried.
- **Adding a source later:** if you add `SPOTIFY_CLIENT_ID` after a previous run already exhausted some entries with the other four sources, those entries are automatically re-cascaded **only for the newly-available source** on the next run. The same applies when you authenticate Beatport for the first time.
- **Re-running steps:** Step 2 always rebuilds `dj_releases.json` from `collection.json` (cheap). Steps 3 and 4 are cache-aware.

To start clean, delete the relevant cache file (e.g. `rm .tmp/bpm_cache.json` for a fresh BPM lookup) and re-run the step.

---

## BPM cascade details

`tools/fetch_bpm.py` tries five sources in order, accepting the first validated hit per track:

1. **songbpm.com** — HTML scrape of canonical detail pages. No auth, 1 req/s. Strong general coverage.
2. **Deezer** — public `api.deezer.com/track` endpoint. No auth, ~4 req/s. Note: a track may exist on Deezer with `bpm: 0`, which means "known but not analysed" — counted as a miss and falls through to the next source.
3. **ReccoBeats** — drop-in replacement for the deprecated Spotify audio-features endpoint. Requires `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` for the name→Spotify-ID translation step (no user OAuth). Spotify Client Credentials tokens last 1 hour and are cached in-memory.
4. **Beatport v4** — editorial BPM provided by the labels themselves; very accurate for house/techno/DnB/disco/electro. Uses the public Swagger client_id; tokens stored in `.tmp/beatport_tokens.json` (see next section).
5. **AcousticBrainz** — open dataset reached via MusicBrainz recording IDs. Frozen since 2022 but excellent for older electronic releases.

**Validation:** every hit is fuzzy-matched against artist + title (rapidfuzz, min score 70). This stops a same-titled but unrelated track from being accepted by mistake. Plausibility window: 50 ≤ BPM ≤ 250.

**Continuous mixes** — a track with a "side-only" position (e.g. `A`, with no `A1`/`A2` subnumbers) longer than 12 minutes is treated as a continuous mix: no BPM lookup is attempted and the sticker shows `(mix)`.

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

When a source returns the wrong BPM (e.g. picked up a remix, picked up a same-titled but unrelated track), or you have a needle-dropped value you trust over any online source, drop it in `overrides.json` at the repo root. Overrides win over the cache, the continuous-mix detector, and the entire cascade — they are checked **before** anything else for a given track.

Copy `overrides.example.json` to `overrides.json` and edit. The file is a flat JSON list; each entry uses one of two keying strategies:

```json
[
  { "release_id": 123456, "position": "A1", "bpm": 128 },
  { "release_id": 123456, "position": "B2", "bpm": null,
    "note": "force empty box even though songbpm returned 174" },
  { "release_id": 789012, "position": "A", "continuous_mix": true },
  { "artist": "Daft Punk", "title": "Around the World", "bpm": 121 }
]
```

- **`release_id` + `position`** — most precise. Both are printed on the sticker itself, so an override is a one-line edit after a needle-drop.
- **`artist` + `title`** — broader. Matches every track in the collection whose normalized artist+title hash to the same key. Useful when the same track appears on multiple releases.
- `release_id`+`position` takes priority when an entry of each shape matches the same track.

Per-entry fields:

| Field | Meaning |
|---|---|
| `bpm: <int>` (50–250) | Override the BPM. Sticker shows the digits. |
| `bpm: null` | Force "no BPM" — sticker shows the empty fill-in box. Use to suppress a wrong source hit. |
| `continuous_mix: true` | Mark the track as a continuous DJ mix — sticker shows `(mix)` instead of a number. |
| `note` | Free-text reminder for yourself. Ignored by the script. |

Re-run `python tools/fetch_bpm.py` after editing — overrides are applied on the fly, the cache is not poisoned, so removing an override later restores the previously-cached value (or sends the track back through the cascade if it was never cached).

**Validation:** parsing errors, out-of-range BPMs, and entries that match no track in your collection are reported as warnings — never fatal. The unmatched-entry warning is the one to watch for: a typo'd `release_id` produces no override and would otherwise be silent.

`overrides.json` is **not** gitignored by default. Decide for yourself whether to commit it: handy for sharing your hard-earned needle-drops with the repo, fine to leave local.

---

## Beatport: one-time auth

Beatport coverage is opt-in. To enable it:

```bash
# 1. Add to .env:
#    BEATPORT_USERNAME=your.beatport.email@example.com
#    BEATPORT_PASSWORD=your-password

# 2. Mint the initial tokens (no browser, no popup):
python tools/beatport_auth.py
```

This writes `.tmp/beatport_tokens.json`. From then on, `tools/fetch_bpm.py` refreshes access tokens automatically via the refresh token. If the refresh token is ever revoked, `fetch_bpm.py` silently falls back to a fresh login using the credentials in `.env` — you never have to touch this again.

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
| Continuous mix (single-letter position + duration > 12:00) | BPM lookup skipped; sticker shows `(mix)`. |
| Non-ASCII characters (Björk, é, ø, …) | Matching uses NFKD normalisation; the PDF preserves the originals. |
| Multiple SongBPM remix hits | The variant whose title matches the mix suffix wins; otherwise the shortest title (the original mix). |
| Missing tracklist on Discogs | Release lands in `.tmp/skipped.json` with reason `no_tracklist`. |
| Missing genre | No effect — there is no genre filter. |

---

## Failure modes & troubleshooting

| Symptom | What to do |
|---|---|
| `ERROR: set DISCOGS_TOKEN and DISCOGS_USERNAME in .env` | You're in API mode without creds. Add them to `.env`, or use `--csv path/to/export.csv` to bypass the listing API. |
| `ERROR: <N> releases not cached and DISCOGS_TOKEN missing` (CSV mode) | You don't have a token AND the CSV references releases you've never fetched. Either add `DISCOGS_TOKEN` to `.env`, or be patient — without a token the script falls back to the public endpoint at 25 req/min (2.5 s per release). |
| `Folder 'X' not found` | Pass an existing folder name (case-insensitive). In CSV mode the tool prints the folders present in the CSV; in API mode it prints the folders on your Discogs account. |
| `401 Unauthorized` from Discogs/Spotify | Token expired or wrong. Re-issue it and update `.env`. |
| `401 Unauthorized` from Beatport during BPM lookup | Refresh token expired or revoked. `fetch_bpm.py` retries automatically with a fresh password grant; if that fails, re-run `python tools/beatport_auth.py`. |
| HTTP 429 (rate limited) | `tenacity` retries with exponential backoff. Persistent 429s usually mean a misconfigured rate-limit — wait a minute and try again. |
| SongBPM parser returns nothing (HTML changed) | The tool dumps the offending HTML into `.tmp/debug/<slug>.html`. Open it, find the new BPM markup, adjust the selectors in `tools/fetch_bpm.py`, delete the affected keys from `.tmp/bpm_cache.json`, and re-run Step 3. |
| Sticker text overflows / looks wrong on a release | Fonts auto-shrink to fit, but for releases with very long titles or many tracks per side, results can still get tight. Open `dj_releases.json` and confirm the data is correct first; the PDF is a faithful render of that data. If you chose very small stickers via `--sticker-w/--sticker-h`, try a larger size — vertical space is the limiting factor for >6-track sides. |
| `Sticker WxH mm doesn't fit on A4` | The requested `--sticker-w` / `--sticker-h` leaves no room for the 4 mm page edge on A4. Pick a smaller size, or stick to the default 96 × 50.8 mm. |

---

## Project layout

```
.
├── README.md                              ← you are here
├── CLAUDE.md                              ← agent instructions (WAT framework)
├── requirements.txt                       ← Python deps
├── .env                                   ← your secrets (gitignored)
├── overrides.example.json                 ← template for manual BPM overrides
├── overrides.json                         ← (optional) your manual BPM overrides
├── tools/
│   ├── fetch_discogs_collection.py        ← Step 1: collect releases (API or CSV)
│   ├── filter_dj_releases.py              ← Step 2: keep only 12"/LP vinyl
│   ├── fetch_bpm.py                       ← Step 3: 5-source BPM cascade
│   ├── generate_sticker_pdf.py            ← Step 4: render the PDF
│   └── beatport_auth.py                   ← one-time Beatport OAuth bootstrap
├── workflows/
│   └── discogs_dj_stickers.md             ← internal SOP (Dutch); this README mirrors and extends it
└── .tmp/                                  ← all intermediates + final PDF (gitignored)
    ├── collection.json
    ├── release_cache/
    ├── dj_releases.json
    ├── skipped.json
    ├── bpm_cache.json
    ├── bpm_results.json
    ├── beatport_tokens.json
    └── stickers.pdf
```

---

## License & attribution

Personal project. The Beatport client_id used here is the public one from Beatport's own Swagger UI; the Discogs, Spotify, ReccoBeats, Deezer, MusicBrainz/AcousticBrainz and songbpm endpoints are queried through their documented public interfaces. Be a good citizen and respect their rate limits — the tools enforce them, do not weaken them.
