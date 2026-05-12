# Workflow: Discogs DJ Sticker PDF

## Objective
Generate a printable A4 PDF with stickers (one per release) for the DJ-usable 12"/LP records in a Discogs collection. Each sticker shows, per side (A/B), per track: position, artist, title, duration and BPM. Output: `.tmp/stickers.pdf`.

## State store
All persistent state lives in **a single SQLite file** at `data/bpm_stickers.db` in the project root. Tables: `releases`, `tracks`, `bpm_cache`, `bpm_source_hits`, `overrides`, `print_runs` / `print_run_releases`, `kv`. The old `.tmp/*.json` files are gone — on first DB init they're auto-imported and renamed to `*.bak`. Inspect anything with `bpm-stickers query [<table>|--sql "…"|--schema]`.

## Prerequisites
- Python 3.10+
- `pip install -r requirements.txt`
- `.env` filled in with:
  - `DISCOGS_TOKEN` — Personal Access Token from https://www.discogs.com/settings/developers
  - `DISCOGS_USERNAME` — your public Discogs username
  - **Optional (for extra BPM sources):**
    - `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` — free app at
      https://developer.spotify.com/dashboard. Needed for the ReccoBeats
      lookup (Spotify Search translates artist+title → Spotify ID;
      ReccoBeats replaces the deprecated Spotify audio-features endpoint).
- **Optional (Beatport fallback):**
  - Add to `.env`: `BEATPORT_USERNAME` + `BEATPORT_PASSWORD` (the email +
    password you log in to beatport.com with).
  - Run once: `python tools/beatport_auth.py`. No browser, no popup —
    the script does a scripted authorization_code flow against the public
    Swagger client and writes `.tmp/beatport_tokens.json`.
  - `fetch_bpm.py` auto-refreshes access tokens via the refresh_token;
    if the refresh_token expires or is revoked it transparently falls
    back to a fresh login with the creds in `.env`. Fully hands-off.

## Inputs
- Optional `--folder <name-or-id>` to limit the collection to a single Discogs folder. Without the argument: whole collection (folder 0 = All).
- Optional `--csv <path>` to use a Discogs CSV export instead of the collection-listing API.
- Optional `--limit N` for debugging.

## Steps

1. **Fetch the collection**
   ```
   bpm-stickers fetch                          # whole collection via API
   bpm-stickers fetch --folder "DJ"            # only folder "DJ"
   bpm-stickers fetch --csv path/to/export.csv
   bpm-stickers fetch --csv path/to/export.csv --folder "DJ"
   ```
   - `--folder` accepts a folder name (case-insensitive) or a folder id. On an unknown name the tool prints the available folders.
   - `--csv PATH`: use a Discogs CSV export (Collection → Export) as the source instead of the collection-listing API. The folder filter then matches against the `CollectionFolder` column (names only, not ids). Per-release tracklists are still fetched via `/releases/{id}` (cache-aware: rows in `releases` with `raw_tracklist IS NOT NULL` are skipped). In CSV mode **no Discogs account is required**: without `DISCOGS_TOKEN` the public endpoint still works, just at 25 req/min (vs. 60 with a token).
   - Output: rows in `releases` (with `basic_information`, `raw_tracklist`, `notes` JSON columns). Inspect via `bpm-stickers query releases`.
   - Runtime: ~1.1s per release (Discogs authenticated limit = 60 req/min). 500 releases ≈ 10 min.
   - Resumable: cached rows are skipped.

2. **Filter DJ records**
   ```
   bpm-stickers filter
   ```
   - Pure transformation, no network.
   - Output: updates `releases` rows in place — sets `is_dj_release` to 1 (kept) or 0 (skipped, with `skip_reasons` JSON populated). For keepers, inserts normalized rows into `tracks`.
   - Inspect skipped: `bpm-stickers query releases --where "is_dj_release = 0" --cols "id,artist,title,skip_reasons"`.
   - Filter: a format description contains `'12"'` or `'LP'`. All 12" and LP vinyl pass (Single, Maxi, EP, Album). 7"/10"/CD/cassette/digital are dropped. No genre filter.

3. **Look up BPMs (5-source cascade)**
   ```
   bpm-stickers bpm
   ```
   - Per track, fires all 5 sources in parallel and reconciles by consensus:
     1. **songbpm.com** — direct HTML scrape of canonical detail pages.
     2. **Deezer** — public JSON API (no auth).
     3. **ReccoBeats** — drop-in replacement for Spotify's deprecated audio-features.
        Requires `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`.
     4. **Beatport v4** — editorial BPM from labels themselves. OAuth via the
        public Swagger client_id; tokens stored in `kv['beatport_tokens']`.
     5. **AcousticBrainz** — open dataset, looked up via MusicBrainz recording IDs.
        Frozen since 2022, but strong for older electronic releases.
   - Output: one row per (artist, title) hash in `bpm_cache` with `sources_tried`, plus one row per source-that-returned-something in `bpm_source_hits`. Consensus is recomputed on the fly by the renderer.
   - Every `bpm_cache` row tracks `sources_tried`: re-runs only call sources that have not yet been queried for that track. When you extend the cascade with a new source later, existing entries are automatically re-cascaded — only for that new source.
   - Sources without configured credentials raise `SourceUnavailable` and are skipped without being marked as `tried`, so you can add credentials later and only that source will be tried on the next run.
   - Validation: rapidfuzz on artist+title (>= 70 score) against every source's result — prevents a random hit on a same-titled track from being accepted.
   - Rate limits: songbpm 1 req/s, Deezer ~4 req/s, Spotify ~5 req/s, ReccoBeats ~7 req/s, Beatport 2 req/s, MusicBrainz strict 1 req/s.
   - The Beatport client_id (`0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd`) is scraped from the public Swagger UI JS bundle at `api.beatport.com/v4/docs/`. Stable since 2023; if it ever rotates, re-scrape from `/static/btprt/*.js` (grep for `API_CLIENT_ID`).

4. **Generate PDF**
   ```
   bpm-stickers render
   ```
   - Output: `.tmp/stickers.pdf`.
   - Stickers are 96×50.8 mm by default (configurable via `--sticker-w` / `--sticker-h` in mm), two per row on A4, with crop marks and a faint border. Tracks without a found BPM get a small empty rectangle in the BPM column on the sticker so the user can pen the value in by hand after printing.
   - **Tile mode**: pass `--tile` to lay stickers edge-to-edge (no gutters, no page margin) so the print can be sliced with `(cols−1)+(rows−1)` straight ruler cuts. Default `--tile-cols 2 --tile-rows 5` gives exactly 10 stickers per A4 at 105×59.4 mm; the print needs 5 cuts (1 vertical + 4 horizontal). Use borderless printing or expect ~3 mm clipping on outer stickers.
   - **Incremental** (`--new-only`/`--mark-printed`): print history lives in `print_runs` + `print_run_releases`. Inspect via `bpm-stickers query print_runs` and `bpm-stickers query print_run_releases`.

## Outputs
- `.tmp/stickers.pdf` — print on A4 at **100% scale** (no "fit to page"), preferably on self-adhesive paper. Sticker size is 96×50.8 mm; verify with a ruler after the first print.
- Skipped releases — `bpm-stickers query releases --where "is_dj_release = 0" --cols "id,artist,title,skip_reasons"`. Review to confirm no release was accidentally filtered out.

## Operational notes
- **Discogs**: authenticated limit 60 req/min, unauthenticated 25 req/min. Tools sleep 1.1s (auth) or 2.5s (unauth) between calls + tenacity backoff on 429.
- **SongBPM**: 1 req/sec + User-Agent header. On parser failure the raw HTML is dumped into `.tmp/debug/` for quick selector repair.
- **Deezer**: no auth, generous rate budget; note that `bpm: 0` on the `/track` endpoint means "track known but not analysed" (counts as a miss → falls through to the next source).
- **ReccoBeats**: young project (2024+), free and no auth for ReccoBeats itself. Coverage = "everything on Spotify" — limited for pre-Spotify vinyl-only releases. Spotify Client Credentials tokens live 1 hour and are cached in-memory.
- **Beatport**: client_id is public (scraped from the Swagger UI JS bundle). Auth uses an `authorization_code` flow against `api.beatport.com/v4/auth/` — no browser needed, just `BEATPORT_USERNAME` + `BEATPORT_PASSWORD` in `.env`. Access tokens live ~1 hour and are auto-refreshed; on an invalid refresh_token `fetch_bpm.py` performs a fresh login itself.
- **MusicBrainz**: strict 1 req/s. User-Agent must include contact info (see `API_USER_AGENT` in `fetch_bpm.py`).
- **Resumability**: every step is idempotent. Killing it mid-run and restarting loses no data thanks to the DB caches (fetch_bpm commits cache rows every 5 cascade runs; fetch commits every 25 releases).

## Edge cases
- **V/A / compilations** → release header shows "V/A — {title}"; per-track artist comes from the tracklist.
- **Positions `A` / `B` without a subnumber** → treated as one track per side, position kept literally.
- **Continuous mixes** (single-letter position with duration > 12:00) → BPM lookup skipped, sticker shows `(mix)`.
- **No tracklist or no genre** → release row gets `is_dj_release=0` with `skip_reasons` populated.
- **Non-ASCII (Björk, é, ø)** → NFKD normalisation for matching, originals on the PDF.
- **Remixes** → with multiple SongBPM hits, the variant whose mix suffix matches wins; otherwise the shortest title (the original).

## Failure modes & recovery
- **401 Unauthorized**: Discogs/Spotify token expired or wrong → check `.env`. For Beatport: re-run `bpm-stickers auth-beatport`.
- **HTTP 429 / 503**: tenacity retries with backoff. On persistent 429s: lower concurrency or wait.
- **SongBPM HTML changes** (parser returns nothing): inspect `.tmp/debug/{slug}.html`, adjust the selectors in `fetch_bpm.py`, drop the affected rows with `sqlite3 data/bpm_stickers.db "DELETE FROM bpm_source_hits WHERE source='songbpm'"`, and re-run step 3.
- **Discogs API down**: step 1 is resumable, restart and the DB-backed cache keeps filling up.

## Self-improvement
Update this document when you encounter rate limits, parser quirks or edge cases. The WAT loop: identify, fix the tool, verify, document here, move on.
