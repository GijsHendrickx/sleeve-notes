# Workflow: Discogs DJ Sticker PDF

## Objective
Genereer een print-bare A4-PDF met stickers (één per release) voor de DJ-bruikbare elektronische 12"-platen uit een Discogs collectie. Elke sticker toont per kant (A/B) per track: positie, artiest, titel, lengte en BPM. Output: `.tmp/stickers.pdf`.

## Prerequisites
- Python 3.10+
- `pip install -r requirements.txt`
- `.env` ingevuld met:
  - `DISCOGS_TOKEN` — Personal Access Token van https://www.discogs.com/settings/developers
  - `DISCOGS_USERNAME` — je publieke Discogs username
  - **Optioneel (voor extra BPM-bronnen):**
    - `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET` — gratis app via
      https://developer.spotify.com/dashboard. Nodig voor ReccoBeats-lookup
      (Spotify Search vertaalt artist+title → Spotify-ID; ReccoBeats vervangt
      het gedeprecate Spotify audio-features endpoint).
- **Optioneel (Beatport fallback):**
  - Voeg toe aan `.env`: `BEATPORT_USERNAME` + `BEATPORT_PASSWORD` (de email +
    password waarmee je op beatport.com inlogt).
  - Run éénmalig `python tools/beatport_auth.py`. Geen browser, geen popup —
    het script doet een password grant (Resource Owner Password Credentials)
    tegen de publieke Swagger client en schrijft `.tmp/beatport_tokens.json`.
  - `fetch_bpm.py` ververst access_tokens automatisch via refresh_token; als
    de refresh_token vervalt of wordt ingetrokken valt het automatisch terug
    op een nieuwe password grant met de creds in `.env`. Geen handwerk meer.

## Inputs
- Optioneel `--folder <naam-of-id>` om je collectie te beperken tot één Discogs folder. Zonder argument: hele collectie (folder 0 = All).
- Optioneel `--limit N` voor debugging.

## Steps

1. **Haal collectie op**
   ```
   python tools/fetch_discogs_collection.py                 # hele collectie via API
   python tools/fetch_discogs_collection.py --folder "DJ"   # alleen folder "DJ"
   python tools/fetch_discogs_collection.py --csv path/to/discogs-export.csv
   python tools/fetch_discogs_collection.py --csv path/to/discogs-export.csv --folder "DJ"
   ```
   - `--folder` accepteert een folder-naam (case-insensitive) of een folder-id. Bij onbekende naam toont de tool de beschikbare folders.
   - `--csv PATH`: gebruik een Discogs CSV-export (Collection → Export) als bron i.p.v. de collection-listing API. Folder-filter werkt dan tegen de `CollectionFolder`-kolom (alleen op naam, niet op id). Per-release tracklists worden nog wel via `/releases/{id}` opgehaald (cache-aware via `.tmp/release_cache/`). In CSV-mode is **geen Discogs-account nodig**: zonder `DISCOGS_TOKEN` werkt het public endpoint nog steeds, alleen op 25 req/min (i.p.v. 60 met token) — gap wordt automatisch 2.5s i.p.v. 1.1s. `DISCOGS_USERNAME` is nooit nodig in CSV-mode.
   - Output: `.tmp/collection.json` + per-release cache in `.tmp/release_cache/{id}.json`.
   - Runtime: ~1.1s per release (Discogs auth-limiet 60 req/min). 500 releases ≈ 10 min.
   - Hervatbaar: bestaande cache-bestanden worden overgeslagen.

2. **Filter DJ-platen**
   ```
   python tools/filter_dj_releases.py
   ```
   - Pure transformatie, geen netwerk.
   - Output: `.tmp/dj_releases.json` (releases die door het filter komen) + `.tmp/skipped.json` (met reden per geweigerde release).
   - Filter: format-description bevat `'12"'` of `'LP'`. Alle 12" en LP vinyl komen door (Single, Maxi, EP, Album). 7"/10"/CD/cassette/digital vallen buiten. Geen genre-filter.

3. **Zoek BPM's op (5-source cascade)**
   ```
   python tools/fetch_bpm.py
   ```
   - Per track, in volgorde tot er een hit is:
     1. **songbpm.com** — directe HTML-scrape van canonical detail-pagina's.
     2. **Deezer** — publieke JSON API (geen auth).
     3. **ReccoBeats** — drop-in replacement voor Spotify's gedeprecate audio-features.
        Spotify Search vertaalt artist+title → Spotify-ID; ReccoBeats `/v1/track?ids=`
        translatet die naar een eigen UUID; daarna `/v1/track/{uuid}/audio-features`
        voor `tempo`. Vereist `SPOTIFY_CLIENT_ID` + `SPOTIFY_CLIENT_SECRET`.
     4. **Beatport v4** — editorial BPM door labels zelf aangeleverd, dus zeer
        accuraat voor house/techno/DnB/disco/electro. OAuth via de publieke
        Swagger client_id; tokens in `.tmp/beatport_tokens.json`.
     5. **AcousticBrainz** — open dataset, lookup via MusicBrainz recording-IDs.
        Bevroren sinds 2022, maar sterk voor oudere electronic releases.
   - Output: `.tmp/bpm_results.json`. Persistent cache: `.tmp/bpm_cache.json`.
   - Elke cache-entry tracked `sources_tried`: re-runs raken alleen nog-niet-geprobeerde bronnen. Een entry met alle vijf geprobeerd krijgt `exhausted: true` en wordt nooit opnieuw gequeried. Wanneer je de cascade later uitbreidt met een nieuwe bron, worden oude `exhausted`-entries automatisch opnieuw gecascadeerd voor alleen die nieuwe bron.
   - Bronnen zonder geconfigureerde credentials raisen `SourceUnavailable` en worden geskipt zonder als `tried` gemarkeerd te worden — je kunt dus later credentials toevoegen en alleen die bron wordt geprobeerd op de volgende run.
   - Validatie: rapidfuzz op artist+title (>= 70 score) tegen het resultaat van élke bron — voorkomt dat een random hit op gelijknamige tracks wordt geaccepteerd.
   - Rate-limits: songbpm 1 req/s, Deezer ~4 req/s, Spotify ~5 req/s, ReccoBeats ~7 req/s, Beatport 2 req/s, MusicBrainz strikt 1 req/s.
   - Beatport client_id (`0GIvkCltVIuPkkwSJHp6NDb3s0potTjLBQr388Dd`) is gescraped uit de publieke Swagger-UI JS bundle van `api.beatport.com/v4/docs/`. Stabiel sinds 2023; bij rotatie opnieuw scrapen uit `/static/btprt/*.js` (grep voor `API_CLIENT_ID`).

4. **Genereer PDF**
   ```
   python tools/generate_sticker_pdf.py
   ```
   - Output: `.tmp/stickers.pdf`.
   - 6 stickers (70×100mm) per A4 + invul-pagina(s) achterin voor missende BPM's.

## Outputs
- `.tmp/stickers.pdf` — print op A4, **100% schaal** (geen "fit to page"), bij voorkeur op zelfklevend papier. Sticker is 70×100mm; controleer met liniaal na print.
- `.tmp/skipped.json` — review om te zien of een release per ongeluk weggefilterd is.

## Operational notes
- **Discogs**: auth-limiet 60 req/min. Tools sleep 1.1s tussen calls + tenacity-backoff op 429.
- **SongBPM**: 1 req/sec + User-Agent header. Bij parser-falen wordt raw HTML gedumpt in `.tmp/debug/` voor snelle reparatie van de selectors.
- **Deezer**: geen auth, ruim rate-budget; let op `bpm: 0` op /track endpoint = track wel bekend maar niet geanalyseerd (telt als miss → valt door naar de volgende bron).
- **ReccoBeats**: jong project (2024+), gratis en geen auth voor ReccoBeats zelf. Coverage = "alles op Spotify" — beperkt voor pre-Spotify vinyl-only releases. Spotify Client Credentials tokens leven 1 uur en worden in-memory gecached.
- **Beatport**: client_id is publiek (gescraped uit de Swagger UI JS bundle). Auth gebruikt `password` grant tegen `account.beatport.com/o/token/` — geen browser nodig, alleen `BEATPORT_USERNAME` + `BEATPORT_PASSWORD` in `.env`. Access tokens leven ~1 uur en worden automatisch ververst; bij ongeldige refresh_token doet `fetch_bpm.py` zelf een nieuwe password grant.
- **MusicBrainz**: strikte 1 req/s. User-Agent moet contact-info bevatten (zie `API_USER_AGENT` in `fetch_bpm.py`).
- **Hervatbaarheid**: alle stappen zijn idempotent. Halverwege killen en herstarten verliest geen data dankzij cache-bestanden in `.tmp/`.

## Edge cases
- **V/A / compilations** → release-header toont "V/A — {title}"; per-track artiest komt uit de tracklist.
- **Posities `A` / `B` zonder subnummer** → behandeld als één track per kant, positie letterlijk behouden.
- **Continuous mixes** (positie zonder subnummer met duration > 12:00) → BPM-lookup geskipt, sticker toont `(mix)`.
- **Geen tracklist of geen genre** → release belandt in `.tmp/skipped.json` met reden.
- **Niet-ASCII (Björk, é, ø)** → NFKD-normalisatie voor matching, originelen op de PDF.
- **Remixes** → bij meerdere SongBPM-hits wint de variant waarvan de mix-suffix matcht; anders kortste titel (origineel).

## Failure modes & recovery
- **401 Unauthorized**: Discogs/Spotify token verlopen of fout → check `.env`. Voor Beatport: `python tools/beatport_auth.py` opnieuw draaien.
- **HTTP 429 / 503**: tenacity probeert opnieuw met backoff. Bij aanhoudende 429: verlaag concurrency of wacht.
- **SongBPM HTML wijzigt** (parser geeft niets terug): kijk in `.tmp/debug/{slug}.html`, pas selectors aan in `fetch_bpm.py`, verwijder de aangetaste keys uit `.tmp/bpm_cache.json` en draai stap 3 opnieuw.
- **Discogs API down**: stap 1 is hervatbaar, herstart en de cache vult zich verder.

## Self-improvement
Update dit document wanneer je rate-limits, parser-quirks of edge cases tegenkomt. De WAT-loop: identificeer, fix in de tool, verifieer, document hier, ga verder.
