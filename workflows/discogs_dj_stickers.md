# Workflow: Discogs DJ Sticker PDF

## Objective
Generate a printable A4 PDF with stickers (one per release) for the DJ-usable 12"/LP records in a Discogs collection. Each sticker shows, per side (A/B), per track: position, artist, title, duration and BPM. Output: `.tmp/stickers.pdf`.

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
   python tools/fetch_discogs_collection.py                 # whole collection via API
   python tools/fetch_discogs_collection.py --folder "DJ"   # only folder "DJ"
   python tools/fetch_discogs_collection.py --csv path/to/discogs-export.csv
   python tools/fetch_discogs_collection.py --csv path/to/discogs-export.csv --folder "DJ"
   ```
   - `--folder` accepts a folder name (case-insensitive) or a folder id. On an unknown name the tool prints the available folders.
   - `--csv PATH`: use a Discogs CSV export (Collection → Export) as the source instead of the collection-listing API. The folder filter then matches against the `CollectionFolder` column (names only, not ids). Per-release tracklists are still fetched via `/releases/{id}` (cache-aware via `.tmp/release_cache/`). In CSV mode **no Discogs account is required**: without `DISCOGS_TOKEN` the public endpoint still works, just at 25 req/min (vs. 60 with a token) — gap automatically becomes 2.5s instead of 1.1s. `DISCOGS_USERNAME` is never needed in CSV mode.
   - Output: `.tmp/collection.json` + per-release cache in `.tmp/release_cache/{id}.json`.
   - Runtime: ~1.1s per release (Discogs authenticated limit = 60 req/min). 500 releases ≈ 10 min.
   - Resumable: existing cache files are skipped.

2. **Filter DJ records**
   ```
   python tools/filter_dj_releases.py
   ```
   - Pure transformation, no network.
   - Output: `.tmp/dj_releases.json` (releases that pass the filter) + `.tmp/skipped.json` (with the rejection reason per skipped release).
   - Filter: a format description contains `'12"'` or `'LP'`. All 12" and LP vinyl pass (Single, Maxi, EP, Album). 7"/10"/CD/cassette/digital are dropped. No genre filter.

3. **Look up BPMs (5-source cascade)**
   ```
   python tools/fetch_bpm.py
   ```
   - Per track, in order, until there is a hit:
     1. **songbpm.com** — direct HTML scrape of canonical detail pages.
     2. **Deezer** — public JSON API (no auth).
     3. **ReccoBeats** — drop-in replacement for Spotify's deprecated audio-features.
        Spotify Search translates artist+title → Spotify ID; ReccoBeats `/v1/track?ids=`
        translates that into its own UUID; then `/v1/track/{uuid}/audio-features`
        for `tempo`. Requires `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`.
     4. **Beatport v4** — editorial BPM supplied by labels themselves, so very
        accurate for house/techno/DnB/disco/electro. OAuth via the public
        Swagger client_id; tokens in `.tmp/beatport_tokens.json`.
     5. **AcousticBrainz** — open dataset, looked up via MusicBrainz recording IDs.
        Frozen since 2022, but strong for older electronic releases.
   - Output: `.tmp/bpm_results.json`. Persistent cache: `.tmp/bpm_cache.json`.
   - Every cache entry tracks `sources_tried`: re-runs only hit sources that have not yet been tried. An entry with all five tried gets `exhausted: true` and is never re-queried. When you extend the cascade with a new source later, existing `exhausted` entries are automatically re-cascaded — only for that new source.
   - Sources without configured credentials raise `SourceUnavailable` and are skipped without being marked as `tried`, so you can add credentials later and only that source will be tried on the next run.
   - Validation: rapidfuzz on artist+title (>= 70 score) against every source's result — prevents a random hit on a same-titled track from being accepted.
   - Rate limits: songbpm 1 req/s, Deezer ~4 req/s, Spotify ~5 req/s, ReccoBeats ~7 req/s, Beatport 2 req/s, MusicBrainz strict 1 req/s.
   - The Beatport client_id (`0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd`) is scraped from the public Swagger UI JS bundle at `api.beatport.com/v4/docs/`. Stable since 2023; if it ever rotates, re-scrape from `/static/btprt/*.js` (grep for `API_CLIENT_ID`).

4. **Generate PDF**
   ```
   python tools/generate_sticker_pdf.py
   ```
   - Output: `.tmp/stickers.pdf`.
   - Stickers are 96×50.8 mm by default (configurable via `--sticker-w` / `--sticker-h` in mm), two per row on A4, with crop marks and a faint border. Tracks without a found BPM get a small empty rectangle in the BPM column on the sticker so the user can pen the value in by hand after printing.
   - **Tile mode**: pass `--tile` to lay stickers edge-to-edge (no gutters, no page margin) so the print can be sliced with `(cols−1)+(rows−1)` straight ruler cuts. Default `--tile-cols 2 --tile-rows 5` gives exactly 10 stickers per A4 at 105×59.4 mm; the print needs 5 cuts (1 vertical + 4 horizontal). Use borderless printing or expect ~3 mm clipping on outer stickers.

## Outputs
- `.tmp/stickers.pdf` — print on A4 at **100% scale** (no "fit to page"), preferably on self-adhesive paper. Sticker size is 96×50.8 mm; verify with a ruler after the first print.
- `.tmp/skipped.json` — review to confirm no release was accidentally filtered out.

## Operational notes
- **Discogs**: authenticated limit 60 req/min, unauthenticated 25 req/min. Tools sleep 1.1s (auth) or 2.5s (unauth) between calls + tenacity backoff on 429.
- **SongBPM**: 1 req/sec + User-Agent header. On parser failure the raw HTML is dumped into `.tmp/debug/` for quick selector repair.
- **Deezer**: no auth, generous rate budget; note that `bpm: 0` on the `/track` endpoint means "track known but not analysed" (counts as a miss → falls through to the next source).
- **ReccoBeats**: young project (2024+), free and no auth for ReccoBeats itself. Coverage = "everything on Spotify" — limited for pre-Spotify vinyl-only releases. Spotify Client Credentials tokens live 1 hour and are cached in-memory.
- **Beatport**: client_id is public (scraped from the Swagger UI JS bundle). Auth uses an `authorization_code` flow against `api.beatport.com/v4/auth/` — no browser needed, just `BEATPORT_USERNAME` + `BEATPORT_PASSWORD` in `.env`. Access tokens live ~1 hour and are auto-refreshed; on an invalid refresh_token `fetch_bpm.py` performs a fresh login itself.
- **MusicBrainz**: strict 1 req/s. User-Agent must include contact info (see `API_USER_AGENT` in `fetch_bpm.py`).
- **Resumability**: every step is idempotent. Killing it mid-run and restarting loses no data thanks to the cache files in `.tmp/`.

## Edge cases
- **V/A / compilations** → release header shows "V/A — {title}"; per-track artist comes from the tracklist.
- **Positions `A` / `B` without a subnumber** → treated as one track per side, position kept literally.
- **Continuous mixes** (single-letter position with duration > 12:00) → BPM lookup skipped, sticker shows `(mix)`.
- **No tracklist or no genre** → release ends up in `.tmp/skipped.json` with a reason.
- **Non-ASCII (Björk, é, ø)** → NFKD normalisation for matching, originals on the PDF.
- **Remixes** → with multiple SongBPM hits, the variant whose mix suffix matches wins; otherwise the shortest title (the original).

## Failure modes & recovery
- **401 Unauthorized**: Discogs/Spotify token expired or wrong → check `.env`. For Beatport: re-run `python tools/beatport_auth.py`.
- **HTTP 429 / 503**: tenacity retries with backoff. On persistent 429s: lower concurrency or wait.
- **SongBPM HTML changes** (parser returns nothing): inspect `.tmp/debug/{slug}.html`, adjust the selectors in `fetch_bpm.py`, remove the affected keys from `.tmp/bpm_cache.json` and re-run step 3.
- **Discogs API down**: step 1 is resumable, restart and the cache keeps filling up.

## Self-improvement
Update this document when you encounter rate limits, parser quirks or edge cases. The WAT loop: identify, fix the tool, verify, document here, move on.
