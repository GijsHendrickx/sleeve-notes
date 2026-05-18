# Ideas to improve the project

## On distribution
- **Publish to PyPI** so `pipx install sleeve-notes` works without a clone. The package is already structured for it; just needs an account + `python -m build && twine upload`.

## On the sticker itself
- **Per-side stickers** as an option, for 12" singles where the sleeve has two visible faces.

## On correctness
- **Snapshot test on the PDF**: render one fixed input through `sleeve-notes render`, diff against a known-good PNG. The scraper-driven sources also deserve unit tests on their HTML parser (songbpm especially — it's the one most likely to silently break).

## Rendering engine
- Different designs
- Configurable font and font size
- Transparent background (for transparent stickers)

## Per-source BPM koppelingen (user-configureerbaar)

Vandaag zijn alle BPM-bronnen app-wide geconfigureerd via een enkele set creds in `.env`. Idee: ze openzetten als per-user opt-in koppelingen, zodat elke user zijn eigen accounts gebruikt en zelf kan kiezen welke bronnen actief zijn én in welke volgorde de cascade ze probeert.

### Functionele scope
- **Settings-pagina "BPM bronnen"** met één rij per provider, elk met een **Connect** / **Disconnect** knop en een status-indicator (gekoppeld / niet gekoppeld / token verlopen).
- **Cascade volgorde** als drag-and-drop lijst: user sleept Spotify boven Beatport boven Tidal boven YouTube. De BPM lookup probeert ze in deze volgorde tot er een hit is.
- **Per-bron toggle** (aan/uit) los van de volgorde, zodat user een bron kan deactiveren zonder hem te ontkoppelen.
- **Opt-in**: zonder gekoppelde bronnen → geen BPM cascade (of fallback naar publieke bronnen als SongBPM die geen creds nodig hebben). Beslissing nemen bij implementatie.

### Providers in scope
- **Spotify** — OAuth 2.0 (Authorization Code + PKCE), refresh_token opslag volgens [credentials architectuur in MIGRATION_PLAN.md](./MIGRATION_PLAN.md#per-user-credentials-architectuur).
- **Beatport** — username + password, encrypted-at-rest (geen publieke OAuth beschikbaar). Heldere consent-tekst bij koppeling.
- **Tidal** — TBD, zie open vragen hieronder.
- **YouTube** — TBD, zie open vragen hieronder. Twijfelachtig of dit een per-user "Connect"-flow rechtvaardigt; YouTube Data API werkt met API key, niet OAuth voor zoeken.
- **Publieke bronnen** (SongBPM, etc.) — geen koppeling nodig, blijven altijd beschikbaar als fallback.
- **Discogs** — bewust uitgesloten; Discogs levert geen BPM-data.

### Open vragen vóór implementatie
- **Tidal BPM-data**: levert Tidal's API daadwerkelijk BPM per track? Hun audio-analyse-endpoint bestaat maar dekking is onduidelijk. Vereist research voordat we Tidal als bron toevoegen — geen punt om een mooie connect-flow te bouwen als de data er niet is.
- **YouTube in dit model**: YouTube zoeken/scrapen gebruikt een API key, geen user-OAuth. Drie opties:
  1. YouTube blijft app-wide, verschijnt niet in de per-user koppelingen-pagina.
  2. YouTube in lijst, maar zonder Connect-knop — alleen toggle aan/uit en positie in cascade.
  3. Per-user "Bring Your Own API Key" (user maakt een Google Cloud project, voert key in). Veel UX-friction, alleen voor power users.
- **Fallback gedrag**: als user géén bronnen heeft gekoppeld — leeg laten, of altijd publieke bronnen + app-wide YouTube als safety net?
- **Cascade granulariteit**: één globale volgorde per user, of per-track? Globaal is veel simpeler en waarschijnlijk genoeg.

### Afhankelijkheid
Bouwt op de credentials-architectuur die in de Django-migratie wordt vastgelegd (`django-cryptography`, `*Connection` models met cascade-delete). Niet eerder beginnen dan na fase 4 van de migratie.

## Print fulfillment & monetisatie

Toekomstige feature: user klikt "Bestel deze stickers", betaalt, PDF gaat naar een print-partner-API, partner drukt op stickerpapier en levert af bij de klant. Dit is het commerciële verdienmodel.

**Fulfilmentmodel: drop-ship via print-API-partner (vastgelegd).** Geen eigen voorraad, geen self-fulfilment, geen SendCloud. Print-partner ontvangt PDF + klantadres en verzendt rechtstreeks aan de klant. Reden: solo-founder, geen fysiek voorraadbeheer gewenst.

### Functionele scope
- Checkout-flow vanaf bestaande Print run: shipping address, aantal sets, prijsberekening, betaling via Stripe Checkout.
- Order-status pagina ("Betaald", "Bij drukker", "Verzonden", "Bezorgd") met tracking-link.
- Transactional emails: orderbevestiging, verzendnotificatie, refund-bevestiging.
- Admin-interface voor refunds, manuele interventie, klantcontact.

### Architectuurimplicaties op de Django-migratie

Twee v1-beslissingen in [MIGRATION_PLAN.md](./MIGRATION_PLAN.md) krijgen een uitzondering wanneer deze feature komt — geen rework van v1 nodig, wel awareness:

- **PDF-storage** wordt voor paid orders persistent (Cloudflare R2 / S3) als onveranderlijke `PrintArtifact`. Reden: de klant betaalde voor *die* PDF; later regenereren kan andere output geven en is onverdedigbaar bij dispute. Niet-betaalde renders blijven ephemeral.
- **Hard delete + cascade** krijgt uitzondering voor `Order`-model. Financiële records moeten 7 jaar bewaard (NL BTW-eis). User-verwijdering → PII-velden op order anonimiseren, order-row blijft. Vereist `on_delete=SET_NULL` of `PROTECT` op `Order.user` FK.

### Nieuwe infrastructuur die hierbij komt
- **Object storage** (Cloudflare R2 aanrader) voor PDF-artifacts
- **Stripe Checkout** + webhook endpoint + idempotency-tabel (`WebhookEvent(event_id unique)`)
- **Print partner integratie** — partner TBD; bepaalt API design
- **`Order`, `OrderItem`, `PrintArtifact`, `Address` modellen** in een nieuwe `orders` app
- **`AuditEvent` / `OrderEvent` model** voor state-changes en dispute-tracking
- **Transactional email templates** in Resend (infrastructuur al gepland)
- **VAT/BTW** via Stripe Tax (~0.5% fee, neemt EU-MOSS administratie over)

### Cheap-insurance toevoegingen voor v1 (optioneel, ~2 uur werk samen)
- **`PrintRun.pdf_hash` veld** — content-hash van gerenderde PDF. Anker voor latere "is deze reproduceerbaar?"-checks.
- **`AuditEvent` model skelet** — `(user_id, action, target_type, target_id, payload_json, created_at)`. In v1 alleen voor "user X imported Discogs collectie". Wordt later de basis voor `OrderEvent`.

### Open vragen vóór implementatie
- **Print partner — voorkeur: Drukwerkdeal.nl.** Nederlandse partij, BTW-factuur klopt direct, A4 kiss-cut stickers is regulier product, Order API ondersteunt drop-ship met blind packaging, EU-shipping uitstekend.
  - **Backup-kandidaten:** Helloprint Connect (zelfde groep, andere positionering), Pixartprinting (Italiaans, sterke API), Sticker Mule (premium kwaliteit maar mogelijk US/UK shipping → customs + langzamer).
  - **Waarschijnlijk afvallers:** Printful en Gelato (POD-model met vaste sticker-templates, geen vrije CutContour cut-paden per order), StickerYou (Canadees, EU-shipping niet optimaal), Vistaprint (API niet flexibel genoeg voor SaaS drop-ship).
  - **Validatie vóór API-implementatie:** één avond pre-research met 2 kandidaten parallel — bestel via UI een A4 testprint met CutContour-paden in verschillende vormen (cirkel, vierkant, vrije vorm), blind packaging naar jezelf (~€15 per test). Vergelijk papierkwaliteit, print, snijkwaliteit, snelheid. Vraag API-docs + sandbox toegang op. Pas daarna code schrijven. Voorkomt een week refactor als de gekozen vendor achteraf niet voldoet.
- **Pricing model** — flat per sticker-set, of variabel op aantal/formaat? Heeft impact op `Order` / `OrderItem` schema.
- **Shipping scope v1** — alleen NL, EU, of wereldwijd? Beperkt complexity (BTW, douane, vertaling van mails).
- **Merchant of record overwegen** — Stripe + Stripe Tax (~3.4% all-in, jij doet tax-aangifte) versus Lemon Squeezy / Paddle (~5%, zij regelen alle wereldwijde tax compliance). Lemon Squeezy / Paddle zijn primair op digitale producten gericht; verifieer fysieke-goods-flow voor sticker shipping voordat je kiest.
- **Legal preconditions** — KvK-inschrijving, BTW-nummer, algemene voorwaarden, privacy policy, DPA's met Stripe + printer. Geen technische blokker maar wel implementatieblokker.

### Afhankelijkheid
Bouwt op de v1-migratie (Django + Postgres + Render + UUID-PKs + Resend). Niet eerder beginnen dan na fase 7 van de migratie. Eerder is mogelijk maar niet aan te raden — eerst stabiliseren, dan monetariseren.

