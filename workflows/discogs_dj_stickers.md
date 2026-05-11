# Workflow: Discogs DJ Sticker PDF

## Objective
Genereer een print-bare A4-PDF met stickers (één per release) voor de DJ-bruikbare elektronische 12"-platen uit een Discogs collectie. Elke sticker toont per kant (A/B) per track: positie, artiest, titel, lengte en BPM. Output: `.tmp/stickers.pdf`.

## Prerequisites
- Python 3.10+
- `pip install -r requirements.txt`
- `.env` ingevuld met:
  - `DISCOGS_TOKEN` — Personal Access Token van https://www.discogs.com/settings/developers
  - `DISCOGS_USERNAME` — je publieke Discogs username

## Inputs
- Optioneel `--folder <naam-of-id>` om je collectie te beperken tot één Discogs folder. Zonder argument: hele collectie (folder 0 = All).
- Optioneel `--limit N` voor debugging.

## Steps

1. **Haal collectie op**
   ```
   python tools/fetch_discogs_collection.py                 # hele collectie
   python tools/fetch_discogs_collection.py --folder "DJ"   # alleen folder "DJ"
   ```
   - `--folder` accepteert een folder-naam (case-insensitive) of een folder-id. Bij onbekende naam toont de tool de beschikbare folders.
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

3. **Zoek BPM's op (3-source cascade)**
   ```
   python tools/fetch_bpm.py
   ```
   - Per track, in volgorde tot er een hit is:
     1. **songbpm.com** — directe HTML-scrape van canonical detail-pagina's.
     2. **Deezer** — publieke JSON API (geen auth).
     3. **AcousticBrainz** — open dataset, lookup via MusicBrainz recording-IDs.
        Bevroren sinds 2022, maar sterk voor oudere electronic releases.
   - Output: `.tmp/bpm_results.json`. Persistent cache: `.tmp/bpm_cache.json`.
   - Elke cache-entry tracked `sources_tried`: re-runs raken alleen nog-niet-geprobeerde bronnen. Een entry met alle drie geprobeerd krijgt `exhausted: true` en wordt nooit opnieuw gequeried.
   - Validatie: rapidfuzz op artist+title (>= 70 score) tegen het resultaat van élke bron — voorkomt dat een random hit op gelijknamige tracks wordt geaccepteerd.
   - Rate-limits: songbpm 1 req/s, Deezer ~4 req/s, MusicBrainz strikt 1 req/s (User-Agent met identificatie verplicht).
   - Geen credentials nodig voor enige bron — script is volledig portable.

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
- **Deezer**: geen auth, ruim rate-budget; let op `bpm: 0` op /track endpoint = track wel bekend maar niet geanalyseerd (telt als miss → valt door naar AcousticBrainz).
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
- **401 Unauthorized**: token verlopen of fout → check `.env`.
- **HTTP 429 / 503**: tenacity probeert opnieuw met backoff. Bij aanhoudende 429: verlaag concurrency of wacht.
- **SongBPM HTML wijzigt** (parser geeft niets terug): kijk in `.tmp/debug/{slug}.html`, pas selectors aan in `fetch_bpm.py`, verwijder de aangetaste keys uit `.tmp/bpm_cache.json` en draai stap 3 opnieuw.
- **Discogs API down**: stap 1 is hervatbaar, herstart en de cache vult zich verder.

## Self-improvement
Update dit document wanneer je rate-limits, parser-quirks of edge cases tegenkomt. De WAT-loop: identificeer, fix in de tool, verifieer, document hier, ga verder.
