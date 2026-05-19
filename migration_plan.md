# Migratieplan: Sleeve Notes → Django

Dit document beschrijft hoe Sleeve Notes wordt overgezet van de huidige FastAPI + HTMX + SQLite stack naar Django + Postgres, gehost op Render. Het doel is een ingezette applicatie waar deployen `git push` is, maintenance dashboards-werk is, en de codebase grotendeels uit bewezen frameworks bestaat in plaats van custom code.

> **Lees dit eerst:** [Voortgang](#voortgang) documenteert wat er werkelijk is gebeurd t.o.v. het plan. De fase-secties hieronder zijn het oorspronkelijke ontwerp; de Voortgang corrigeert daar waar realiteit het plan inhaalde.

## Inhoudsopgave

1. [Voortgang](#voortgang)
2. [Doelstelling](#doelstelling)
3. [Eindstack](#eindstack)
4. [Aannames](#aannames)
5. [Vroege architectuur-beslissingen](#vroege-architectuur-beslissingen)
6. [Fase 0 — Voorbereiding](#fase-0--voorbereiding)
7. [Fase 1 — Skelet & deploy-pipeline](#fase-1--skelet--deploy-pipeline)
8. [Fase 2 — Auth](#fase-2--auth)
9. [Fase 3 — Models & migrations](#fase-3--models--migrations)
10. [Fase 4 — Engine integratie](#fase-4--engine-integratie)
11. [Fase 5 — Web UI port](#fase-5--web-ui-port)
12. [Fase 6 — Productie cut-over](#fase-6--productie-cut-over)
13. [Fase 7 — Operationeel](#fase-7--operationeel)
14. [Per-user credentials architectuur](#per-user-credentials-architectuur)
15. [Operationele zorgvuldigheid v1](#operationele-zorgvuldigheid-v1)
16. [Buiten scope v1](#buiten-scope-v1)
17. [Maandelijkse maintenance](#maandelijkse-maintenance)
18. [Tijdsinschatting](#tijdsinschatting)
19. [Open vragen](#open-vragen)

---

## Voortgang

Stand van zaken voor wie net dit document opent in een nieuw context window.

### Branch & live state

- **Branch:** `django-port` (op origin, auto-deploy naar Render staging)
- **Laatste commit:** `f529b92` (zie `git log --oneline` voor actuele lijst)
- **Staging URL:** `https://sleeve-notes-web-staging.onrender.com` — Basic Auth gate actief; achter de gate werkt Discogs OAuth login + logout
- **Lokaal:** `docker compose up -d db` voor Postgres; `cd webapp && ../.venv/bin/python manage.py runserver` voor Django; legacy FastAPI app draait nog via `sleeve-notes web` (zie waarschuwing onder *Transitional state*)

### Fase-status

| Fase | Status | Notitie |
|---|---|---|
| 0. Voorbereiding | ✓ klaar | Render + Sentry + Discogs staging-OAuth-app aangemaakt; secrets in Bitwarden |
| 1. Skelet & deploy | ✓ klaar | Django 5.2 + Postgres + Sentry + Basic Auth gate + healthz + RUNBOOK; `git push` deployt |
| 2. Auth | ✓ klaar | django-allauth + custom Discogs OAuth1 provider; sign-in werkt end-to-end op staging |
| 3. Models | ✓ klaar | Records / PrintRun / AuditEvent / BpmCache + admin; UUID PKs op user-facing, TimestampedModel base |
| 4. Engine integratie | gedeeltelijk | 4A.1–4A.4 klaar (alle DB-rakende modules geport). 4B (Django-Q2) en 4C (PDF render command) staan nog open. |
| 5. Web UI port | ✗ niet begonnen | Volledige FastAPI → Django UI; komt na 4 |
| 6. Productie cut-over | ✗ niet begonnen | Pas wanneer 5 klaar is + staging een week stabiel |
| 7. Operationeel | n.v.t. | Doorlopend |

### Sub-blok status binnen fase 4

| Sub-blok | Status | Commit |
|---|---|---|
| 4A.1 Django bootstrap in engine | ✓ | `3b05e48` |
| 4A.2 Port `overrides.py` | ✓ | `3b05e48` |
| 4A.3 Port `query.py` + `fetch_discogs_collection.py` | ✓ | `3024322` |
| 4A.4 Port `fetch_bpm.py` | ✓ | `f529b92` |
| 4B Django-Q2 voor async tasks | TODO (~1u) | — |
| 4C PDF render management command | TODO (~30m) | — |

### Architectuur-beslissingen die tijdens de port zijn gewijzigd

Het oorspronkelijke plan was correct in opzet maar miste detail. Wijzigingen die de plan-aannames overrulen:

- **Aanname #5 omgekeerd.** Het plan zei "geen dedicated bpm_cache als aparte tabel, wordt veld op Track". Verkeerd — de cache is per *song* (artist+title hash), niet per *track*. Dezelfde song op een single én een LP deelt één cache-row. `records.BpmCache` is dus een eigen model. `Track.bpm` is volledig verwijderd; rendering leest door de cache.
- **Schema is iteratief verfijnd.** De model-sketches in fase 3 hieronder zijn historisch. Canonieke schema-staat: `webapp/records/models.py` / `webapp/print_runs/models.py` / `webapp/audit/models.py`. Het oorspronkelijke `Release.artists` (JSONField) is `artist` (CharField) geworden om met de Discogs-engine te matchen. `Override` is herontworpen om zowel precise (release+position) als broad (artist+title) match te ondersteunen — anders zou de "zelfde song op single + LP" feature verloren gaan.
- **Schaal-doel + tijdsinvestering bevestigd.** v1 mikt op 10–100 users, geen deadline, variabel/burst-werk. Render instances staan op de kleinste paid tier (~$21/mnd voor staging-stack).

### Engine-port pattern (hou dit aan voor 4B / 4C en later)

De gevolgde conventie tijdens fase 4:

```
sleeve_notes/<module>.py                              ← legacy SQLite versie
  → main() vervangen door dunne delegator
  → pure helper-functies (HTTP, parsing, layout, etc.) blijven 1:1 staan
    en worden door de Django-kant geïmporteerd

webapp/<app>/services/<module>.py                     ← ORM-bound logica
  → wraps de pure helpers uit sleeve_notes/
  → schrijft via Django ORM
  → call-pattern: run_*(user, *, log=print, ...)

webapp/<app>/management/commands/<command>.py         ← entry point
  → thin wrapper rond services/
  → resolve user via DISCOGS_USER_ID env var
  → mapped door sleeve_notes/<module>.py delegator
```

Concrete voorbeelden: `webapp/records/services/discogs_sync.py` (sync) en `webapp/records/services/bpm_cascade.py` (BPM cascade). Eerst port-pattern volgen, daarna naar Django-Q2 tillen in fase 4B.

### Transitional state — gotchas voor de volgende sessie

- **`sleeve-notes web` start nog de oude FastAPI app.** Dat is fine zolang we de Django web UI niet hebben gebouwd (fase 5). Maar: gebruikers die de oude FastAPI gebruiken zien een SQLite-wereld; de CLI subcommands (`fetch`, `bpm`, `query`, `overrides`) schrijven nu naar Postgres. **De twee datasets zijn ontkoppeld.** Dit is OK voor nu (alleen de developer test) maar moet worden opgelost vóór fase 6.
- **Beatport in de BPM cascade leest nog uit het SQLite kv-tabel** via `sleeve_notes/db.py`. Wanneer het SQLite-bestand niet bestaat (bv. op Render) faalt `_load_beatport_tokens` netjes; cascade slaat Beatport over. Wanneer we Beatport echt willen gebruiken na de port: app-wide kv mechanism nodig (kv-model in `core/` of `audit/` app, of env-var fallback).
- **`sleeve_notes/db.py` bestaat nog.** Andere engine modules (`generate_sticker_pdf.py`, `beatport_auth.py`, dood gemaakte legacy code in `fetch_bpm.py`) importeren het. Verwijderen kan zodra die laatste callers ook geport zijn (fase 4C en daarna).
- **`sleeve_notes/ingest.normalize_release` is dode code** sinds 4A.3. De pure helpers eromheen (`join_artists`, `transform_tracks`, `extract_rpms`) worden wél gebruikt door `webapp/records/services/discogs_sync.py`.
- **CSRF_TRUSTED_ORIGINS moet per environment gezet** zijn voor HTTPS POSTs (sign-out, forms). Staging heeft het al; productie moet later toegevoegd worden.
- **Custom domein nog niet ingezet.** Staging draait op `*.onrender.com`. Bij custom domein: Cloudflare Access voor staging i.p.v. HTTP Basic Auth + DNS records voor Resend (DKIM/SPF/DMARC).

### Volgende stappen — concreet starten met 4B

Voor wie 4B oppakt (Django-Q2):

1. Add `django-q2` to `pyproject.toml` dependencies; `pip install -e .`.
2. Configure in `webapp/sleevenotes_app/settings.py`: `Q_CLUSTER = {"orm": "default", ...}` (Postgres als broker, geen Redis).
3. Add `django_q` to `INSTALLED_APPS`; `makemigrations` + `migrate` (creates Q2 broker tables).
4. Create `webapp/records/jobs.py` (or similar) with `@async_task`-wrapped versions of `run_bpm_cascade` and `sync_via_csv`/`sync_via_api`.
5. Update `render.yaml`: add a `worker` service of type `worker` that runs `python manage.py qcluster`. Same Docker image, different command.
6. Per-user lock in jobs (DB-row in AuditEvent or a dedicated UserJobLock model) so one user can't start two parallel syncs.
7. Test: trigger a sync via a temp management command that just calls `async_task("...", user.id)`.

Voor 4C (PDF render management command): lees `sleeve_notes/generate_sticker_pdf.py`, port `main()` naar een Django management command `render_print_run`, gebruik het bestaande `PrintRun` model voor input + `pdf_hash` veld voor output-hash.

---

## Doelstelling

Een ingezette Django-app op Render waar je:

- **deployen doet door `git push` te doen** (verder geen knoppen)
- **maintenance doet door dashboards te bekijken** (Render, Sentry) — geen SSH, geen servers, geen DB-tunen
- **veilig kunt experimenteren in staging** zonder productie te raken
- **lokaal kunt ontwikkelen met identieke stack als productie**

Bestaande gebruikersbeslissingen:

- **Repo strategie:** nieuwe branch `django-port` in bestaande repo
- **Data:** schone start, geen migratie vanuit `data/sleeve_notes.db`
- **CLI:** blijft werken naast Django, deelt engine-code
- **Domein:** Render-subdomein voor v1, custom domein later
- **Schaal-doelstelling v1:** 10–100 users (niche, vrienden/gemeenschap), zonder launchdeadline. Render-instance sizes en monitoring afgestemd op dit doel; opschalen is later een dashboard-aanpassing.
- **Tijdsinvestering:** variabel, in bursts. Elke fase split in 1–3 uur sub-taken die zelfstandig commitbaar zijn.

## Eindstack

| Laag | Keuze | Waarom |
|---|---|---|
| Framework | Django 5.x | LTS-cyclus, batteries-included |
| Database | Postgres 16 (lokaal + Render) | Identiek lokaal/prod, geen "works on my machine" |
| Hosting | Render | PaaS, git-push deploys, alles in één dashboard |
| Web server | Gunicorn + WhiteNoise | Conventie; WhiteNoise serveert static files zonder CDN-config |
| Auth | django-allauth + Discogs provider | OAuth 1.0a flow uitbesteed aan beheerde library |
| Background jobs | Django-Q2 (Postgres als broker) | Geen aparte Redis nodig → minder services, minder maintenance, minder kosten |
| Config | django-environ | 12-factor; één `settings.py`, env vars sturen gedrag |
| Errors / perf | Sentry (gratis tier) | Errors gemaild, slow queries zichtbaar |
| Lokale DB | Postgres in Docker | Eén `docker compose up`, klaar |
| Staging-toegang | HTTP Basic Auth middleware | Werkt overal, geen IP-gedoe; upgraden naar Cloudflare Access zodra eigen domein |
| Repo | Branch `django-port` in huidige repo | Engine code blijft toegankelijk; later mergen of branch behouden |
| CLI | Blijft werken, deelt engine met Django | Engine gebruikt Django ORM; CLI configureert SQLite, web configureert Postgres |

## Aannames

Deze keuzes zijn niet-omkeerbaar zonder werk. Bevestigen vóór fase 1.

1. **Engine wordt Django-ORM-gebaseerd, niet meer raw `sqlite3`.** Eén code-pad voor CLI en web. CLI start met `DATABASE_URL=sqlite:///data/sleeve_notes.db`, web met Postgres. Beide draaien dezelfde engine-code. Schema-changes via Django migrations.
2. **Job queue is Django-Q2, niet Celery.** Django-Q2 gebruikt Postgres als broker → één service minder op Render, geen Redis te beheren. Voor de huidige schaal ruim voldoende. Kan later naar Celery als nodig.
3. **PDF's worden niet persistent opgeslagen.** Render's disk is ephemeral. PDF wordt on-demand gerendered vanuit het `PrintRun`-record (precies zoals nu via `sleeve-notes render --print-run-id N`). Geen S3/R2 nodig.
4. **Eén Discogs OAuth-app per environment.** Aparte registratie voor staging en productie. Voorkomt dat een staging-bug productie-OAuth omgooit.
5. **Geen dedicated `bpm_cache` als aparte tabel.** Wordt een veld op `Track` (`bpm`, `bpm_sources_tried`, `bpm_resolved_at`). Eenvoudiger schema, geen verlies aan functionaliteit.
6. **Sticky single-job-slot per user is goed genoeg voor v1.** Django-Q2 default = N parallel workers globaal. Per-user lock (DB-row) voorkomt dat één user drie syncs tegelijk start. App-wide queue mag meerdere users tegelijk verwerken.
7. **Brand-naam staat in één centrale `APP_NAME` setting, codebase-identifiers blijven `sleeve_notes`.** Alle user-zichtbare strings (wordmark, page titles, mail-templates, marketing copy in templates) lezen `APP_NAME` uit env var. Rebranden later = één env var in het Render dashboard wijzigen, geen code-change. Python-package, CLI command (`sleeve-notes`), DB-tabelnamen en interne imports blijven voor altijd `sleeve_notes` ongeacht latere rebrand — interne namen mogen geschiedenis dragen. Praktische uitvoering: `APP_NAME = env("APP_NAME", default="Sleeve Notes")` in `settings.py`, beschikbaar in alle templates via context processor, nooit hardcoded in een `.html` of `.py` file.

## Vroege architectuur-beslissingen

Beslissingen die *nu* worden vastgelegd omdat retrofitten later duur of pijnlijk is. Deze leven op fase-0 niveau: het skelet wordt erop gebouwd, niet andersom.

### A. Data & schema

**Tijdzones.** `USE_TZ = True` in settings. Alle datetimes in DB als UTC. Display in browser locale via JS `Intl.DateTimeFormat`. Geen per-user timezone-setting in v1.

**Primary keys.** UUID v4 op alle user-facing modellen: `Release`, `Track`, `Override`, `PrintRun`. `User` blijft INT (allauth + admin compatibility). Voorkomt enumeration en maakt URLs deelbaar.

**Audit-velden op elk model.** Abstract `TimestampedModel` met `created_at` (auto_now_add) en `updated_at` (auto_now). Elk model erft. Geen uitzonderingen.

**Hard delete + cascade v1.** Alle ForeignKeys naar User: `on_delete=CASCADE`. User-row weg = alle data van die user weg. GDPR-vriendelijk, simpel. Soft delete pas wanneer een specifieke use-case zich aandient (bv. "undo print run delete" knop) — dan per-model, niet app-wide.

**`email` veld op User.** `AbstractUser` heeft 'm al. Bij eerste Discogs-login optioneel uitvragen. Geeft optionality voor toekomstige mail-flows zonder schema-change.

### B. Environment hygiene

**Drie environments, één env var.** `ENVIRONMENT=local|staging|production` overal in code, Sentry tags, logs, footer banner.

**Secrets bron-van-waarheid.** Render dashboard is runtime-bron. Backup-kopie in 1Password van álle env vars per environment. Vooral `FIELD_ENCRYPTION_KEY` — kwijt = nooit meer credentials decrypten.

**Env var naming.** `ALL_CAPS_SNAKE_CASE`, geen afkortingen, prefix op provider (`DISCOGS_*`, `SPOTIFY_*`, `SENTRY_*`, `DJANGO_*`). Connection strings als URLs (`DATABASE_URL`) niet als losse parts.

**Footer banner per environment.** Op staging: rode strook bovenaan met "STAGING — niet productie". Voorkomt dat je per ongeluk in de verkeerde omgeving werkt. Op productie: niets.

### C. Operationele scaffolding

**Email provider: Resend.** Account aanmaken bij fase 1, `DEFAULT_FROM_EMAIL = "noreply@..."` als placeholder. DNS-records (SPF/DKIM/DMARC) klaarzetten zodra custom domein bekend is — propagatie duurt 24u, dat wil je niet doen op het moment dat je de eerste mail wil sturen. Geen actieve verzendcode in v1, wel infrastructuur klaar.

**CI pipeline vanaf dag 1.** GitHub Actions workflow:
- `python -m pytest`
- `python manage.py check`
- `python manage.py makemigrations --check --dry-run`

Render auto-deploy gekoppeld aan "deploy alleen bij groene CI". Workflow file in repo van begin af aan, zelfs als er één lege test in zit.

**Test framework: pytest-django + factory-boy.** Scaffold staat klaar (`conftest.py`, `factories.py`, één voorbeeld-test per app). Testen schrijven we wanneer we ze nodig hebben (bugfix, nieuwe feature). Later toevoegen = bestaande code refactoren om testbaar te zijn.

**Sentry user context + environment tags.** `sentry_sdk.set_user({"id": request.user.id, "username": request.user.username})` per request, `sentry_sdk.set_tag("environment", settings.ENVIRONMENT)` globaal. Errors in Sentry tonen direct welke user op welke omgeving, in plaats van anonieme stack traces.

### D. URL & toekomstige optionality

**URL-structuur ontworpen voor groei:**

```
/                       landing (geen login)
/dashboard              ingelogde homepage
/collection             records
/tracks                 tracks
/print-runs             index
/print-runs/<uuid>      detail (UUID, deelbaar)
/settings/account       toekomstig
/settings/bpm-sources   toekomstig (per-source koppelingen, zie roadmap.md)
/api/v1/...             gereserveerd, nog geen routes
```

`/settings/` namespace en `/api/v1/` prefix nu plannen voorkomt rommelige top-level routes later.

**App-wide settings via singleton model (gereserveerd, niet ingevuld).** Plek in schema voor toekomstige veranderlijke app-wide instellingen (announcement banner, maintenance flag, default BPM cascade volgorde voor nieuwe users). Geen invulling in v1, wel naam afgesproken: `SiteSettings` als single-row model.

### E. Architectuurkeuzes die we afslaan

| Concept | Reden |
|---|---|
| i18n / vertalingen | Single-language is prima; `_()`-overal-toevoegen is duur, latere overstap is mogelijk |
| Redis / caching laag | Postgres trekt het ruim; toevoegen wanneer Sentry slow queries laat zien |
| Feature flag systeem | env vars + één env per environment voldoet voor v1 |
| Multi-region DB | Single region in Europa is goed tot ~100k users |
| GraphQL / REST API endpoints | Geen externe consumers, geen API nodig |
| Microservices / split deploys | Alles in één Django app, geen premature splitsing |
| Cookie consent banner | Pas nodig bij third-party trackers — die hebben we niet |

## Fase 0 — Voorbereiding

**Tijd:** 30 minuten, geen code.

1. Branch `django-port` aanmaken vanaf `main`.
2. Render-account aanmaken (gratis), GitHub-koppeling autoriseren.
3. Sentry-account aanmaken (gratis), één project `sleeve-notes`.
4. Tweede Discogs OAuth-app registreren bij Discogs developer settings — naam: `Sleeve Notes (staging)`, callback URL tijdelijk leeg (vullen we in fase 2).

**Klaar als:** Render dashboard open, Sentry DSN ergens opgeschreven, twee Discogs OAuth apps in Discogs settings (productie blijft de bestaande, staging is nieuw).

## Fase 1 — Skelet & deploy-pipeline

**Tijd:** 1 dag.

Doel: vanaf nul kunnen pushen en op een Render-URL een "hello world" zien, end-to-end.

1. `django-admin startproject sleevenotes_app` in `webapp/` directory (naast bestaande `sleeve_notes_web/`).
2. **Custom User model meteen aanmaken** (`users` app, `User(AbstractUser)`) — Django's #1 traditionele val: dit later toevoegen is zeer pijnlijk, op dag 1 triviaal.
3. `settings.py` rebuild met `django-environ` patroon.
4. `docker-compose.yml` aan repo toevoegen voor lokale Postgres.
5. `Dockerfile` voor Render.
6. `render.yaml` met:
   - `web` service (gunicorn)
   - `worker` service (Django-Q2 cluster)
   - `database` (Postgres)
   - Environment group voor gedeelde env vars
   - Auto-deploy van `django-port` branch
7. HTTP Basic Auth middleware in `webapp/middleware/staging_gate.py` (activeert bij `ENVIRONMENT=staging`).
8. Sentry SDK installeren, DSN en `environment` tag uit env var.
9. Health-check endpoint `/healthz` voor Render's monitoring.

**Klaar als:** `git push origin django-port` → 2 minuten later staat er een Django-app op `sleeve-notes-staging.onrender.com`, vraagt om basic-auth wachtwoord, daarna zie je "Sleeve Notes (staging) — Django works".

## Fase 2 — Auth

**Tijd:** 1 dag.

1. `django-allauth` installeren.
2. Discogs OAuth1 provider configureren (allauth ondersteunt het of we voegen ~30 regels custom-provider toe).
3. Discogs staging-OAuth-app's callback URL invullen: `https://sleeve-notes-staging.onrender.com/accounts/discogs/login/callback/`.
4. Login / logout / "ingelogd als" flows.
5. Sidebar van huidige UI overnemen (Tailwind blijft, layout 1-op-1).
6. `SocialToken` van allauth slaat Discogs access token + secret op (encrypted at rest mogelijk via `django-cryptography`).
7. **`request.user` als single source of truth** voor multi-tenancy — `user_id`-parameter-threading verdwijnt overal.

**Klaar als:** inloggen met Discogs op staging-URL, gebruikersnaam verschijnt in sidebar, uitloggen werkt.

## Fase 3 — Models & migrations

**Tijd:** 1 dag.

Django models definiëren, conform de [architectuur-beslissingen](#vroege-architectuur-beslissingen) (UUID PKs op user-facing modellen, `TimestampedModel` als basis, hard delete + cascade):

```python
import uuid

class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True

class User(AbstractUser):
    # email, date_joined, last_login geërfd van AbstractUser
    discogs_user_id = models.IntegerField(unique=True, null=True)
    last_seen_at = models.DateTimeField(auto_now=True)

class Release(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    discogs_release_id = models.IntegerField()
    title = models.CharField(max_length=500)
    artists = models.JSONField()
    raw_tracklist = models.JSONField(null=True)
    # ... rest van velden
    class Meta:
        unique_together = [("user", "discogs_release_id")]
        indexes = [models.Index(fields=["user", "discogs_release_id"])]

class Track(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    release = models.ForeignKey(Release, on_delete=models.CASCADE)
    position = models.CharField(max_length=20)
    title = models.CharField(max_length=500)
    bpm = models.FloatField(null=True)
    bpm_sources_tried = models.JSONField(default=list)
    bpm_resolved_at = models.DateTimeField(null=True)

class PrintRun(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    settings = models.JSONField()
    release_ids = models.JSONField()
    pdf_hash = models.CharField(max_length=64, blank=True)  # SHA256 hex van laatste render

class Override(TimestampedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    track = models.ForeignKey(Track, on_delete=models.CASCADE)
    field = models.CharField(max_length=50)
    value = models.JSONField()

class AuditEvent(TimestampedModel):
    """Append-only log van user-acties. Basis voor latere OrderEvent / dispute logs."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    action = models.CharField(max_length=100)        # bv. "discogs.sync", "bpm.lookup", "print_run.create"
    target_type = models.CharField(max_length=50, blank=True)  # "release" | "print_run" | ...
    target_id = models.CharField(max_length=64, blank=True)    # UUID of INT als string
    payload = models.JSONField(default=dict)
    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"])]
```

`python manage.py makemigrations && python manage.py migrate` — lokaal en op Render.

**Alle models registreren in Django admin.** Gratis CRUD-UI op `/admin/` voor jezelf — zoeken, banken, deactiveren, data inspecteren.

### Cheap-insurance voor toekomstige print fulfillment

Twee velden/modellen hierboven zijn niet strikt nodig voor v1 maar zijn vooruitlopend op de [print fulfillment feature in roadmap.md](./roadmap.md#print-fulfillment--monetisatie). Geen functionele zichtbaarheid voor users; bespaart refactoring later:

- **`PrintRun.pdf_hash`** — content-hash van de gerenderde PDF, gevuld op elke render. Anker voor latere "is deze PDF reproduceerbaar?"-checks en dispute-verificatie wanneer paid orders binnenkomen.
- **`AuditEvent`** — append-only log van user-acties. In v1 gevuld bij Discogs sync, BPM lookup, print run create. Wordt later de basis voor `OrderEvent` met dezelfde shape maar `on_delete=SET_NULL` (financiële records moeten user-deletion overleven). Twee separate modellen straks, één foundation nu.

Wanneer print fulfillment landt: `AuditEvent` blijft voor niet-financiële events (hard-delete-cascade consistent met v1), `OrderEvent` ernaast voor financiële events met retention-policy.

## Fase 4 — Engine integratie

**Tijd:** 2–3 dagen.

Bestaande engine-functies (`sleeve_notes/discogs.py`, `bpm.py`, `render.py`) blijven werken, maar lezen/schrijven via Django ORM in plaats van raw `sqlite3`.

1. **`sleeve_notes/db.py` weggooien.** Vervangen door Django ORM. Engine importeert `from webapp.records.models import Release, Track, ...`.
2. **`derive_track_result`, `build_bpm_lookup`, `load_overrides`, etc. herschrijven** om `User` object te accepteren in plaats van `user_id`-int + `conn`. Korter, type-safe, één bron van waarheid.
3. **CLI dispatcher** (`sleeve_notes/cli.py`) wordt een dunne wrapper rond Django management commands. `sleeve-notes sync` ≡ `python manage.py sync`. De `sleeve-notes` entry point blijft maar setupt Django settings en delegeert.
4. **Discogs sync wordt een Django-Q2 task.** `runner.start(kind="sync", user=user)` → `async_task("webapp.jobs.tasks.run_discogs_sync", user.id)`. Geen subprocess meer, geen env-var-injection meer. Job state in Postgres.
5. **BPM cascade idem.** Plus: per-user lock zodat dezelfde user niet twee syncs tegelijk kan starten.
6. **PDF render blijft synchroon** voor download-endpoint (snel genoeg) of async voor grote runs — keuze in code.

Een Django-Q2 task die crasht wordt automatisch geretried met exponential backoff, status zichtbaar in admin, logs in Render.

## Fase 5 — Web UI port

**Tijd:** 3–5 dagen.

Mechanisch werk; weinig ontwerpkeuzes.

1. **Templates** — Jinja → Django templates. Syntactisch ~95% identiek. Eén grep-vervangoperatie voor `url_for` → `{% url %}`.
2. **Routes** — FastAPI routes → Django views (functie-based, geen class-based; eenvoudiger).
3. **HTMX** — `hx-*` attributen veranderen niet. `django-htmx` library installeren voor helpers.
4. **Job toast** — wijst nu naar Django-Q2 task status endpoint i.p.v. job runner endpoint. Polling werkt hetzelfde.
5. **Static files** — Tailwind CSS bundle blijft. WhiteNoise serveert ze met cache headers. Bump-counter (`?v=N`) niet meer nodig — WhiteNoise hashed filenames.
6. **Per-user data filtering** — overal `Release.objects.filter(user=request.user)`. Geen `Depends(current_user)` meer.

Sectie-volgorde: Records → Tracks → Print runs → Discogs sync → Import CSV → BPM lookup. Een voor een gemigreerd, elke afzonderlijke pagina is een PR/commit.

## Fase 6 — Productie cut-over

**Tijd:** 1 dag, één keer.

Voorwaarde: staging draait stabiel, minimaal een week zelf in gebruik.

1. Productie-Discogs-OAuth-app aanmaken (de bestaande, met productie-callback URL).
2. In Render: nieuwe environment "production":
   - `web` service op starter ($7/mnd) — voldoende voor 10–100 users, upgraden bij groei is een slider in het dashboard
   - `worker` op starter ($7/mnd)
   - Postgres op kleinste betaalde tier ($7/mnd) — voor backups
   - Auto-deploy gekoppeld aan branch `main` (we mergen `django-port` → `main` op cut-over moment)
3. Sentry: tweede project of zelfde project met `environment=production` tag → aparte alert-regels.
4. Smoke test op `sleeve-notes.onrender.com`: inloggen, sync, BPM, print, PDF.
5. HTTP Basic Auth middleware uitschakelen voor production (`ENVIRONMENT=production`).
6. Aankondigen aan de eerste 1–10 testers.

## Fase 7 — Operationeel

**Tijd:** doorlopend, doet zichzelf grotendeels.

| Zorg | Hoe geregeld |
|---|---|
| Backups | Render Postgres: automatisch dagelijks, 7 dagen retentie op gratis tier, 30+ dagen op betaald |
| Security patches Django | Dependabot maakt PR's, jij merget, Render redeployt |
| SSL | Render automatisch via Let's Encrypt |
| Errors | Sentry → mail |
| Uptime | Render's eigen ping + Better Stack indien gewenst (gratis) |
| Cost monitoring | Render mailt bij usage spikes |
| Discogs rate limits | Per-user 60 req/min — Django-Q2 throttle in task |

## Per-user credentials architectuur

Sleeve Notes ondersteunt verschillende externe diensten (Discogs voor auth, Spotify / Beatport / YouTube / publieke bronnen voor BPM). Per dienst leggen we vast hoe credentials worden opgeslagen, met als rode draad: **geen platte wachtwoorden in de DB, OAuth waar mogelijk, encryption-at-rest waar het moet, opt-in per provider, cascade-delete bij account-verwijdering**.

### Patroon per provider

| Provider | Opslag-patroon | Wanneer geïmplementeerd |
|---|---|---|
| **Discogs** | `SocialToken` via django-allauth, encrypted-at-rest | Fase 2 (auth) — vervangt huidige session-cookie token storage |
| **Spotify** | OAuth 2.0 → encrypted `refresh_token` in `SpotifyConnection` model | Bij implementatie van per-source koppeling (zie `roadmap.md`) — niet in migration v1 |
| **Beatport** | Encrypted `username` + `password` in `BeatportConnection` model | Bij implementatie van per-source koppeling — niet in migration v1 |
| **YouTube** | App-wide API key in env var (geen user-data) | Blijft zoals nu — geen migratie nodig |
| **Publieke bronnen** (SongBPM e.d.) | Geen credentials | N.v.t. |

### Technische keuzes

- **Encryption library:** `django-cryptography` (Fernet-based). Sleutel in env var `FIELD_ENCRYPTION_KEY`, gegenereerd bij eerste deploy, nooit gecommit.
- **Sleutel-rotatie:** standaard `django-cryptography` patroon — meerdere sleutels in env var, oudste wordt uitgefaseerd. Niet automatisch; handmatige operatie wanneer nodig.
- **Cascade-delete:** alle `*Connection` models hebben `ForeignKey(User, on_delete=models.CASCADE)`. Eén DELETE op `User`-row verwijdert alle externe koppelingen automatisch.
- **Disconnect-flow:** per provider een settings-pagina-knop die de `*Connection` row verwijdert. User kan altijd los zonder admin contact.
- **Background job toegang:** workers lezen credentials direct uit de DB (gedecrypteerd door Django ORM). Géén cookie-based opslag, want Django-Q2 workers hebben geen HTTP-context.

### Wat dit betekent voor de migration v1

- **Discogs auth** wordt direct beter: van zelf-onderhouden session-cookie naar allauth's `SocialToken` met optionele field-encryption.
- **BPM bronnen (Spotify/Beatport/YouTube)** blijven in v1 app-wide (huidige gedrag). De architectuur hierboven is het pad voor wanneer per-source user-koppelingen worden gebouwd; zie `roadmap.md` voor die feature.
- **Schema-impact:** Phase 3 voegt geen `*Connection` modellen toe; die komen later bij de feature.

### Wat dit niet doet

- Geen protectie tegen volledig server-compromise (attacker met env var + DB heeft alles).
- Geen end-to-end encryption (wij kunnen decrypten; dat is nodig om jobs te kunnen draaien).
- Geen per-user encryption keys (zou async background jobs onmogelijk maken).

## Operationele zorgvuldigheid v1

Items die niet binnen één fase passen omdat ze cross-cutting zijn, en die we expliciet niet over het hoofd zien.

### `FIELD_ENCRYPTION_KEY` lifecycle

**Genereren:** vóór fase 1, één keer, lokaal:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Opslaan:** in 1Password vaults "Sleeve Notes / staging-secrets" en "/ production-secrets" — aparte sleutels per environment. Daarna in Render dashboard als env var.

**Rotatie:** zet meerdere sleutels in env var als comma-separated, oudste laatst. `django-cryptography` ondersteunt dit; oude sleutel blijft beschikbaar voor decrypt-only tijdens een migratie-window.

**Bij verlies:** alle encrypted credentials (toekomstig: Spotify refresh_tokens, Beatport credentials) zijn onherstelbaar verloren. Users moeten opnieuw koppelen. Geen impact op Django data zelf — alleen op encrypted velden.

### Postgres connection pooling

Render Postgres geeft je twee connection strings:

- `DATABASE_URL` — directe verbinding, max ~10 connections op kleinste tier
- `DATABASE_POOL_URL` — pgbouncer transaction-mode, gedeelde pool

**Configuratie:** web service gebruikt direct, workers (Django-Q2) gebruiken de pool. Voorkomt "too many connections" bij meerdere workers + threads. Zet ook `CONN_MAX_AGE = 60` in settings voor persistent connections binnen requests.

### Backup restore-test (eenmalig vóór productie)

Toevoegen aan fase 6: maak een snapshot van staging-Postgres, restore die in een lege test-instance, verifieer dat data leesbaar is en migrations werken. Eén keer doen vóór productie cut-over, daarna jaarlijks herhalen.

Een backup die je nooit hebt getest is folklore. Render's dailies zijn echt, maar je systeem-knowhow om ze daadwerkelijk te herstellen niet — tenzij je het oefent.

### Account deletion + GDPR data export

V1 mag spartaans zijn, moet bestaan:

- **Verwijderen** — Django admin "Delete selected users" knop is voldoende voor v1. Cascade-delete via ForeignKeys ruimt alle data op. Toekomstig: settings-pagina-knop voor user-initiated delete.
- **Export** — `python manage.py export_user_data <user_id>` management command schrijft JSON-dump met alle records voor die user. Voldoet aan GDPR data-portabiliteit. Toekomstig: settings-pagina download.

Samen ~2 uur werk. Niet doen = juridisch risico zodra je eerste EU-user inlogt, ook al heb je geen formele privacy policy.

### Login anti-abuse

Twee maatregelen, low effort:

- **`django-axes`** — 5 mislukte logins → account/IP lockout, configureerbare duur. Twee regels in `INSTALLED_APPS` en `MIDDLEWARE`.
- **User-creation rate limit** — `django-ratelimit` op de OAuth callback view, max N nieuwe users per IP per uur. Voorkomt massa-account-creatie die jouw app-wide BPM-API-quota's opbranden.

### RUNBOOK.md

Begin leeg in fase 1. Vul incrementeel tijdens elke fase als je iets tegenkomt dat "die zou ik me later willen herinneren" voelt. Vóór productie cut-over moet er minimaal in staan:

- Render dashboard URLs en welke org-account
- Wat te doen bij "DB connection limit reached"
- Wat te doen bij "Discogs API 503 / rate-limited"
- Wat te doen bij "OAuth redirect loop"
- Hoe een deploy-rollback uit te voeren
- Hoe een specifieke user-row in Django admin te vinden

Doel: in een paniekmoment pak je deze file, niet je terminal.

### Python + Django version policy

- **Python 3.12** in fase 1 vastpinnen via `pyproject.toml` en Render's `PYTHON_VERSION` env var. 3.13 werkt ook; 3.12 is conservatiever (langer in productie bij meer projecten).
- **Django 5.2 LTS** (huidige LTS-release). Volgende LTS-cyclus = 6.2 (~april 2027). Tussentijdse minor upgrades optioneel.
- **Dependency policy:** Dependabot auto-merge bij groene CI voor patch/minor releases, handmatig voor major. Security updates altijd direct mergen.

## Buiten scope v1

- Custom domein + Cloudflare Access (zodra je een domein wilt)
- Email-flows (welcome mail, etc.) — Discogs login is genoeg voor v1
- Privacy / GDPR pagina's, account-verwijderingsknop — nodig voor publieke launch, niet voor eerste testers
- Stripe of betalingen
- Read replicas, sharding, caching tiers — pas relevant bij echte schaal
- Per-user Spotify/Beatport OAuth — zie [Open vragen](#open-vragen)
- Celery (Django-Q2 voldoet)
- Cloudflare CDN/WAF/cache (Render levert SSL, edge-cache is niet nodig op deze schaal)

## Maandelijkse maintenance

- `git push` naar `django-port` of `main` om nieuwe features uit te rollen
- Dependabot PRs mergen (~5 minuten/maand)
- Sentry inbox checken als er een mail binnenkomt
- Render dashboard als je een env var wilt veranderen
- Discogs developer settings als je iets aan je OAuth app wilt veranderen

Verder niets. Geen servers, geen DB-tooling, geen SSL-vernieuwing, geen log rotation.

## Tijdsinschatting

Bij ~4–6 uur per dag, met begeleiding:

| Fase | Tijd |
|---|---|
| 0. Voorbereiding | 0,5 dag |
| 1. Skelet & deploy | 1 dag |
| 2. Auth | 1 dag |
| 3. Models | 1 dag |
| 4. Engine integratie | 2–3 dagen |
| 5. Web UI port | 3–5 dagen |
| 6. Productie cut-over | 1 dag |
| **Totaal** | **9–12 effectieve dagen** |

Bij steady cadence (~4–6 uur/dag): praktisch verspreid over 2–4 weken kalenderzijds.

Bij variabel/burst-werk (paar uur tegelijk, met pauzes ertussen): plan in halve-dag- of avondblokken. Fase 5 splitst trivial in per-pagina commits van 2–3 uur elk. Fase 4 is de moeilijkste om te onderbreken — plan een aaneengesloten halve dag specifiek voor de engine-naar-ORM port, anders verlies je context tussen sessies.

## Open vragen

Punten die nog niet vastgelegd zijn, maar voor v1 of v1.1 een keuze nodig hebben:

### Custom domein

Wanneer wisselen van `*.onrender.com` naar bv. `sleevenotes.app` of vergelijkbaar. Geen blokker voor v1, wel een trigger om Cloudflare Access in te zetten voor staging.

### Publieke launch criteria

Wat moet er minimaal staan voordat staging → productie (privacy policy, account-deletion, terms)? Buiten scope van technische migratie, maar relevant voor planning.
