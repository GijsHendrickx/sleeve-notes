# Ideas to improve the project

## Uitgangspunten

Randvoorwaarden die boven de losse ideeën hieronder staan. Elke feature die hiermee botst moet zich verantwoorden, niet omgekeerd.

- **Zo weinig mogelijk klantdata opslaan.** Expliciete wens, met wetgeving (AVG) als reden. Concreet: geen gegevens bewaren die we niet nodig hebben voor het maken van een sticker, en wat we wél bewaren versleutelen of kunnen weggooien. Dit is de reden achter *Discogs-tokens versleutelen* in "Hardening & onderhoudslast" en het is de belangrijkste tegenkracht bij *Print fulfillment* (adressen, orderhistorie) en bij een gedeelde BPM-cache. Bij elke nieuwe tabel eerst de vraag: kan dit veld weg, of korter bewaard worden?
- **Elke sticker is gegarandeerd leesbaar.** Als aanbieder moeten we kunnen beloven dat elke sticker er netjes uitziet, ook voor de plaat met de langste tracklist en in een donkere booth. Daarom kiezen gebruikers uit designs die wij hebben getest, in plaats van zelf een layout te bouwen. Een instelling die dat kan breken (maat, kleur, lettertype) is niet te kiezen; alleen waarschuwen is niet genoeg. Zie "Stickerontwerp".
- **De stack is vastgelegd**: Django LTS + Postgres + één worker, server-rendered met HTMX. Zie "Hardening & onderhoudslast" voor de onderbouwing. Ideeën die een tweede runtime of een SPA vereisen krijgen die last expliciet op hun rekening.

## Volgorde (wat eerst)

Voorstel op basis van afhankelijkheden, okt 2026. Kernidee: **eerst de app veilig genoeg maken om andermans data te bewaren, dan pas testers binnenlaten.** Elke tester zet zijn Discogs-token in onze database.

1. ~~**Hardening vóór testers**~~ — **gedaan (21 okt 2026)**: Discogs-tokens versleuteld, Sentry zonder PII, tenant-isolatietest. Wat overblijft (backups + restore-oefening, workercheck, Dependabot) staat onder "Hardening & onderhoudslast → Uitvoeringsstatus" en hoort bij het herstellen van staging in stap 2.
2. **Staging herstellen + feedbackformulier.** Code-kant **gedaan (21 okt 2026)**: `render.yaml` deployt van `main`, de Docker-build is gerepareerd, en het feedbackformulier staat er. Wat overblijft is dashboardwerk: zie "Feedback van testers → Uitvoeringsstatus".
3. **BPM-kwaliteit**: de genummerde lijst onder "BPM-bronnen onderzoek → Uitvoeringsstatus". Items 1–3 gedaan (okt 2026). Item 4 (×1,5) komt zelden voor: 1 van 94 audio-metingen stond in een 3:2-verhouding tot de metadata, 2 in ×2. Item 5 (Rekordbox-import) hangt aan testers, niet aan code. **Volgorde na deze meting:** item 4 nu (klein, maar laag in waarde); item 5 en stap 4 (gedeelde BPM-cache) wachten op testers, want beide hebben pas waarde met meer dan één gebruiker. Stap 6 (agent-fallback) is gemeten en heeft weinig waarde: na de audioketen blijven 16 van 445 tracks (3,6%) zonder enige audiobron, vooral acapella's (die geen BPM hebben) en remixen.
4. **Gedeelde BPM-cache** (item 7 in diezelfde lijst). Pas zinvol zodra er meer dan één gebruiker is, en pas na stap 1.
5. **Huisstijl.** Na de eerste testerfeedback, zodat die feedback over de flow gaat en niet over kleur.
6. **Agent-fallback voor BPM** (item 6). Als laatste, en pas nadat gemeten is hoeveel tracks er na 3 en 4 nog openstaan.
7. **Jobs-backend** heroverwegen, gekoppeld aan de upgrade naar de volgende Django LTS (6.2, april 2027).

*Print fulfillment & monetisatie* staat bewust niet in deze lijst; die heeft een eigen afhankelijkheid (zie die sectie).

## On distribution
- **Publish to PyPI** so `pipx install sleeve-notes` works without a clone. The package is already structured for it; just needs an account + `python -m build && twine upload`.

## On the sticker itself
- **Per-side stickers** as an option, for 12" singles where the sleeve has two visible faces.

## On correctness
- **Snapshot test on the PDF**: render one fixed input through `sleeve-notes render`, diff against a known-good PNG. The scraper-driven sources also deserve unit tests on their HTML parser (songbpm especially — it's the one most likely to silently break).

## Rendering engine
- Different designs → uitgewerkt in "Stickerontwerp" hieronder
- Configurable font and font size
- Transparent background (for transparent stickers)

## Stickerontwerp: de sticker op de hoes, niet het vel (okt 2026)

Doel: wie stickers afneemt moet de sticker kunnen krijgen zoals hij hem wil, zonder dat de eerste keer ontwerpen ontmoedigt.

**Uitgangspunt: wij garanderen dat elke sticker leesbaar is.** Als aanbieder moeten we kunnen beloven dat elke sticker die de printer verlaat er netjes uitziet, ook voor de plaat met de langste tracklist en ook in een donkere booth. Daaruit volgt de vorm van dit hele blok: de gebruiker kiest uit **designs die wij hebben gemaakt en getest**, en past die alleen aan binnen grenzen waarvan we weten dat ze goed gaan. Vrijheid zit in de keuze uit designs, niet in het zelf bouwen van een layout. Een instelling die de garantie zou breken kan niet worden gekozen; een waarschuwing alleen is niet genoeg.

Wat er nu al is: drie inhoudspresets (Minimal / DJ / Everything, `PRESETS` in `print_runs/flow.py`), vier papiersoorten (`STOCK_OPTIONS`), een eigen breedte × hoogte in mm (gevalideerd in `sticker_layout.derive_layout`, alleen staand A4), en in stap 3 een preview met de keuze *Sheet* / *One label*.

### Afgevoerd: elementen vrij slepen op de sticker
Het idee was dat de gebruiker artiest, titel, BPM enzovoort zelf naar een plek op de sticker kon slepen. **Geschrapt (okt 2026), niet opnieuw voorstellen.** De reden: één ontwerp wordt op tientallen platen toegepast, met artiestnamen van 6 tot 60 tekens en tracklists van 2 tot 14 nummers. Een layout die de gebruiker zelf heeft gebouwd loopt dan over of overlapt, en dat kunnen we niet testen. Dit is precies de plek waar stil een BPM van de sticker valt. Dat botst met de leesbaarheidsgarantie hierboven. Daarnaast vroeg het om client-side JavaScript die buiten de vastgelegde stack valt.

### 1. Preview toont de sticker op de hoes, niet het A4-vel
Gebruikers geven niet om hoe het vel eruitziet maar om hoe de sticker op hun hoes staat. De hoofdpreview wordt de sticker **op ware verhouding op een hoes**: een 12"-hoes is 31,5 cm, een 7" 18 cm. `Release.format` zegt welke maat, en `Release.thumb_url` geeft de echte hoes als achtergrond. Elk design bepaalt zelf waar op de hoes het komt (de krat-strook bovenaan, een gewoon label in de hoek).
- *Challenge:* het vel heeft één echte taak, namelijk laten zien hoeveel stickers er per A4 passen (= hoeveel vellen je koopt of betaalt). Dat kan als één regel tekst ("10 per vel, 3 vellen voor deze set") in plaats van een afbeelding. De vel-weergave verhuist naar de proefprint (zie "Meer ideeën"), of verdwijnt.
- Vervangt het voorstel *Sheet preview in step 3* onder "UX proposals".

### 2. Een catalogus van designs, als hart van stap 3
Stap 3 begint met **een keuze uit designs**, elk als een herkenbare tegel met de eigen plaat van de gebruiker erop. Kiezen en doorgaan moet in één klik kunnen. Een design is meer dan de huidige inhoudspreset: het legt layout, lettergroottes, wat er gebeurt als de inhoud niet past, en welke maten en kleuren mogen vast. Eerste set:
- **Label**: het huidige label in de hoek, in de varianten Minimal / DJ / Everything (de bestaande `PRESETS`).
- **Krat-strook**: zie punt 3.
- Later meer, op basis van wat testers vragen. Elk nieuw design is werk van ons, niet van de gebruiker.

Wat de gebruiker binnen een design kan aanpassen:
- **Inhoud**: de bestaande checkboxes, maar alleen die het design ondersteunt (een krat-strook heeft geen QR-code).
- **Kleurthema**: zie punt 5.
- **Afmetingen**: alleen binnen het bereik waarvoor het design getest is (zie punt 4).

Regels die bij elk design horen, omdat de garantie anders niet waar is:
- **Wat er gebeurt als het niet past, is per design vastgelegd** en dus voorspelbaar: eerst kleiner lettertype tot de ondergrens (voorstel 6 pt, 7 pt voor lichte tekst op donker), daarna een vaste terugval van het design zelf (bijvoorbeeld een BPM-bereik in plaats van elke BPM). Nooit stil afkappen.
- **Testen op de lastigste plaat.** De preview springt standaard naar de plaat in de set die het minst goed past (langste tracklist, langste artiestnaam), niet naar de eerste.
- **Stap 3 meldt platen die op de terugval draaien** ("3 platen tonen een BPM-bereik — bekijk"), zodat het zichtbaar is in plaats van verrassend.
- *Gevolg voor de engine:* `sticker_layout.py` kent nu één layout die van boven naar beneden stapelt (`cursor_y`). Elk design wordt een eigen layoutfunctie in de engine achter het bestaande `Drawer`-protocol, zodat PDF en SVG hetzelfde blijven (zie "Engine reuse" in CLAUDE.md). `PrintRun.settings` krijgt een `design`-sleutel. Zelfde regel als nu: de losse instellingen zijn de bron van waarheid, en `active_preset()` leidt af welke chip oplicht.

### 3. Design: krat-strook
Een lange smalle sticker langs de bovenrand van de voorkant van de hoes. Als je door een krat bladert zie je precies dat stuk, dus je ziet meteen wat je nodig hebt zonder de plaat eruit te halen: artiest, titel en vooral **BPM en toonsoort, groot**.
- *Maat:* een 12"-hoes is 31,5 cm breed, A4 liggend 297 mm. De strook wordt dus hooguit ~285 mm (paginamarge eraf) en ligt liggend op het vel (zie punt 4). Hoogte rond 20–25 mm, dus 8 à 10 stroken per vel. Voor een 7" een kortere variant.
- *Challenge — wat past er op?* Een strook van 2 cm hoog heeft ruimte voor één regel grote tekst. Bij een EP met 4 nummers past "A1 124 · A2 126 · B1 122 · B2 128". Bij een LP met 12 nummers past dat niet leesbaar. Vastleggen als de terugval van dit design: **boven een bepaald aantal nummers toont de strook het BPM-bereik** ("118–126 BPM, 12 tracks"), en de volledige lijst hoort dan op een tweede sticker (het Label-design). Beslissen: is de grens een aantal nummers, of rekent de engine uit wat er past?
- *Challenge — artwork:* een strook bovenaan bedekt een deel van de hoes, en verzamelaars vinden dat erg. Daarom ook hier: verwijderbare lijm of op de buitenhoes plakken (zie "Materiaaladvies").
- Kan samen met een Label-sticker van dezelfde plaat: strook voor in de krat, label met de volledige tracklist voor als de plaat eruit is. Open vraag: één set met twee designs, of twee sets?

### 4. Afmetingen binnen A4, liggend als de sticker breder is dan staand A4
Eigen breedte × hoogte bestaat al; wat ontbreekt is draaien. Is de sticker breder dan staand A4 (210 mm min de paginamarge) maar past hij liggend (297 mm), dan wordt **de hele pagina liggend** opgemaakt. Niet per sticker draaien en staand/liggend mengen op één vel: dat maakt snijden met de hand of door een printpartner foutgevoelig. De krat-strook is de eerste die dit nodig heeft.
- *Challenge — garantie:* vrije maten en een gegarandeerd leesbare sticker gaan niet samen. Elk design geeft daarom een **minimum- en maximummaat** op waarvoor het getest is; daarbinnen is de maat vrij, daarbuiten niet te kiezen. Een sticker van 30 × 15 mm met een tracklist van twaalf nummers is met geen enkel design leesbaar.
- *Challenge — voorgesneden vellen:* wie thuis print op voorgestanste etiketvellen (Avery, Herma) heeft niets aan vrije maten: de print valt naast de stansing. Vrije maten zijn alleen zinvol op ongesneden vellen of bij een printpartner (kiss-cut, zie "Print fulfillment"). De stock picker blijft daarom het startpunt, en "Eigen maat" staat erachter met die waarschuwing erbij.

### 5. Kleuren en achtergronden, als thema's
Voorgrond, accent en achtergrond als **thema's** die je bij elk design kunt kiezen (zwart op wit, wit op zwart, …). Een design komt met een standaardthema, zodat één klik genoeg blijft.
- *Thema's los van designs:* drie designs × vijf thema's zijn al vijftien combinaties, te veel als losse chips. Daarom kies je eerst een design en dan, als je wilt, een thema.
- *Leesbaarheid in het donker:* dit is voor DJ's in clubs. Elk thema, en ook een eigen kleur als die er ooit komt, haalt een **contrastcheck op luminantie** (WCAG-verhouding; voorstel minimaal 7:1 voor kleine tekst). Een combinatie die faalt kun je niet kiezen; een waarschuwing alleen is niet genoeg (zie het uitgangspunt bovenaan). Booths hebben vaak rood of gekleurd licht. Onder rood licht verdwijnt rode tekst, ongeacht hoe goed die er overdag uitziet. Luminantiecontrast overleeft gekleurd licht, kleurcontrast niet.
- *Eigen kleuren?* Advies: in v1 alleen onze thema's. Een kleurkiezer met contrastcheck kan later, maar elke vrije kleur is weer een combinatie die we niet zelf hebben bekeken.
- *Printen:* een gekleurde achtergrond op een tegelvel zonder marges geeft op een thuisprinter witte randen (printers drukken niet tot de rand) en kost veel inkt. Lichte tekst op een donkere achtergrond loopt dicht bij kleine letters, vandaar de hogere ondergrens in punt 2. Het item *Transparent background* hierboven hoort bij dit punt.

### 6. Kleuren uit de hoes van de plaat (droomwens)
Een thema dat per plaat twee kleuren uit de hoes haalt (`thumb_url` van Discogs), mits die genoeg contrasteren, ook in het donker.
- *Is dit te ver?* Minder dan het lijkt. Pillow komt al mee met ReportLab, en kleuren uit een thumbnail van 150 px halen (quantize) is een paar regels. Opslaan: twee hex-waarden per release, geen afbeelding. Dat past bij "zo weinig mogelijk data" (een kleur is geen klantdata). **Afhankelijk van punt 5:** zonder thema's in de engine heeft dit niets om in te vullen.
- *Challenge — terugval:* veel techno- en house-12"s zitten in een generieke of witte hoes, en dan is het palet grijs. Ga niet weigeren maar **corrigeren**: houd de tint, schuif de lichtheid tot de contrastgrens gehaald wordt, en lukt dat niet, val dan terug op zwart/wit. Zo blijft de garantie staan. Per plaat is in de preview zichtbaar welke kleur gekozen is.
- *Challenge — rode hoezen:* zie punt 5. Een rode hoes geeft een rode sticker, en die is onder rood boothlicht onleesbaar. De contrastcheck moet daarom op luminantie, niet op tint.

### Meer ideeën voor hetzelfde doel
- **Booth-preview.** Een schakelaar in stap 3 die de preview toont zoals in een donkere booth: gedimd, en met rood licht. Goedkoop (een CSS-filter over de SVG), en het maakt de leesbaarheidsgarantie zichtbaar voor de gebruiker.
- **BPM als blikvanger.** In het DJ-design het BPM-getal het grootst. Het is wat een DJ als eerste zoekt. Raakt aan de open vraag over Camelot-gekleurde keys onder "UX proposals".
- **Eigen keuze bewaren.** Instellingen staan nu per set (`PrintRun.settings`). Het laatst gekozen design + thema + maat als standaard voor de volgende set. Het is geen persoonsgegeven, dus dat botst niet met de Uitgangspunten.
- **Proefprint-pagina.** Eén A4 op gewoon papier met de stickeromtrekken en een liniaal van 10 cm om de 100%-schaal te controleren. Houd hem tegen de hoes vóór je op duur stickerpapier print. Hier hoort de vel-weergave uit punt 1 thuis.
- **Materiaaladvies (Print fulfillment).** Mat in plaats van glanzend, want glans spiegelt boothlicht. En verwijderbare lijm, want verzamelaars willen geen lijmresten op een hoes die geld waard is. Ook de keuze: sticker op de hoes of op de buitenhoes (shrink/plastic).
- **Afgeronde hoeken.** Die laten minder snel los bij veel in- en uitschuiven uit de krat. Voor een printpartner is dat gratis (snijlijn), voor thuisprint alleen een stippellijn.

### Gevonden in een echte render (okt 2026)
Twee gevallen waar de huidige layout de leesbaarheidsgarantie nu al breekt. Ze horen bij de terugvalregels van punt 2:
- ~~**Lange vinylposities overlappen de titel.**~~ — **gedaan (24 okt 2026).** Bij Angelic "It's My Turn" stond `AAA1` over "It's" heen: de positiekolom had een vaste breedte ("AA1"). Nu is hij minstens zo breed als "AA1" (gewone stickers blijven precies gelijk) en groeit hij mee met de breedste positie op de sticker. De titel krijgt de rest; past die dan niet meer, dan gelden de regels hieronder.
- ~~**Lange titels krimpen tot onleesbaar en worden afgekapt.**~~ — **gedaan (24 okt 2026).** Romanthony "Do You Think You Can Love Me" (vier remixen met lange namen) eindigde op "…". Correctie op de eerste waarneming: het lettertype zakte niet *onder* 6 pt. 6 pt was al de kleinste maat, maar daarna werd stil afgekapt. Nu geldt per sticker een vaste volgorde (`fit_tracks` in `sticker_layout.py`):
  1. De grootste maat (10 → 6 pt) waarop elke titel op één regel past.
  2. Past het op 6 pt nog niet, dan **loopt de titel door op een tweede regel**, op 6 pt, en alleen zolang er verticaal ruimte is (in tracklijstvolgorde).
  3. Pas wat daarna nog niet past, eindigt op "…". Dat wordt **altijd gemeld**: `draw_sticker` geeft een `StickerFit` terug (`wrapped`, `truncated`, `header_truncated`, `overflows`), `render_pdf` zet dat in `RenderResult.fallback_release_ids` / `lossy_release_ids` (afgekapt of over de rand), en `manage.py render_print_run` noemt de Discogs-ids. **De webflow toont het nog niet**: daar blijft een afgekapte titel voorlopig onopgemerkt. Stap 3 kan "platen die op de terugval draaien" tonen zonder de layout opnieuw uit te rekenen; dat is het volgende stuk werk.
  - *Waarom terugloop en niet iets anders:* een tweede regel verliest niets en is voorspelbaar (de BPM, toonsoort en duur blijven op de eerste regel staan, de titel loopt eronder door). Terugloop gebeurt bewust *op* de ondergrens en niet op een grotere maat: zo krijgt een langere titel nooit grotere letters dan een kortere, en geldt op elke sticker dezelfde regel. Afgevallen: (a) een andere kolom smaller maken, maar duur, toonsoort en BPM zijn al zo smal als hun inhoud, en de BPM-kolom is ook het schrijfvak voor een handgeschreven BPM; (b) de titel afkorten (bijv. "(Original Mix)" weglaten), wat raden is en stil informatie weghaalt.
  - *Prijs:* een sticker met terugloop heeft een onregelmatig ritme (één nummer neemt twee regels), en soms staat er één woord op de tweede regel (bij Romanthony "#40)"). Wie dat lelijk vindt: dat is precies het signaal dat de sticker te klein is voor deze tracklist.
  - *Gemeten op de eigen collectie (116 platen, 132 stickers):* op de standaardmaat 96 × 50,8 mm lopen 2 platen door op een tweede regel en wordt geen tracktitel meer afgekapt. Zie hieronder voor wat er nog openstaat.

### Nog open na de terugvalregels (okt 2026)
- **Het vel 3 × 7 (70 × 42,4 mm) haalt de garantie niet.** Met de standaardinhoud (preset Everything) worden op de eigen collectie bij 32 van 116 platen nog titels of kopregels afgekapt, en bij 13 stickers is de tracklijst zelfs op 6 pt zonder terugloop hoger dan de sticker (`overflows`). Dat gebeurde vóór deze wijziging ook al, alleen nu wordt het gemeld. Oplossing hoort bij punt 4 (elk design geeft een minimum- en maximummaat op): dit vel alleen toestaan met Minimal, of zo'n plaat over twee stickers verdelen zoals `split_release_into_stickers` nu al doet boven 8 nummers.
  - *Challenge:* niet "eerst waarschuwen". Volgens het uitgangspunt bovenaan is een waarschuwing alleen niet genoeg, dus moet stap 3 die combinatie onmogelijk maken.
- **De kopregel (artiest en releasetitel) krimpt niet en loopt niet door.** Hij staat vast op 11 en 9 pt en eindigt op "…" als hij te lang is. Bij de eigen collectie gebeurt dat op de standaardmaat bij één plaat (de V/A-sampler "Age Of Love 15 Years Anniversary Vinyl Sampler 1/3", waar het achtervoegsel `[A·B]` de rest wegduwt). Het wordt nu gemeld via `header_truncated`, maar het is nog wel afkappen. Een tweede kopregel duwt de tracklijst omlaag, dus dit hoort bij het design-werk van punt 2.
- **Een lege duurkolom kost nog steeds ruimte.** Romanthony heeft op Discogs geen duur per nummer, toch houdt de layout die kolom vrij. Die breedte teruggeven aan de titel zou terugloop vaak voorkomen. Klein, maar het verandert per plaat de uitlijning van de kolommen, dus het is een ontwerpkeuze.

### Volgorde binnen dit blok
1. Hoes-preview (punt 1) en de contrastcheck: klein, en staat los van de rest.
2. Designs als begrip in de engine (punt 2), met de huidige layout als eerste design "Label". Dit is het fundament voor de rest.
3. Krat-strook (punt 3), met liggend A4 (punt 4) erbij.
4. Thema's (punt 5), daarna kleuren uit de hoes (punt 6).

### Open vragen
- Krat-strook: wanneer valt hij terug op een BPM-bereik, bij een vast aantal nummers of als de engine uitrekent dat het niet past?
- Strook + label van dezelfde plaat: één set met twee designs, of twee aparte sets?
- Moet de hoes-preview de hoes van Discogs laten zien (hotlinken van `thumb_url`) of een neutrale hoes? Hotlinken kost niets, maar dan hangt de preview aan de beschikbaarheid van Discogs-afbeeldingen.

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
- ~~**Tidal BPM-data**~~ — **beantwoord (sep 2026, zie "BPM-bronnen onderzoek" onderaan)**: Tidal's open API vereist OAuth *en* exposeert geen tempo-veld. Zelfde conclusie voor Apple Music (`Songs.Attributes` heeft geen tempo/BPM). Beide afvoeren als bron; niet opnieuw onderzoeken.
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


## Frontend: van dashboard naar flow ("sleeve labels")

De huidige UI is een 1-op-1 vertaling van de CLI-pipeline: de sidebar heet "Actions" en bevat *Discogs sync*, *Import csv*, *BPM lookup* — interne stappen, geen gebruikersdoelen. De gebruiker moet zelf weten dat de volgorde sync → BPM → print run is. Het dashboard toont KPI's (coverage, aantallen); dat is een beheerdersscherm.

**Kernbeslissing: de print run *wordt* de flow.** Geen aparte wizard naast een dashboard — dat is de eerste keer fijn en de vijfde keer irritant. Elke keer dat je labels maakt doorloop je dezelfde vier stappen; de eerste keer komt daar alleen de Discogs-connect en de volledige sync bij. Eén implementatie, twee gevoelens.

Klikbaar prototype: <https://claude.ai/code/artifact/2a5115b1-4087-45b0-92e6-06ff3beef6fe>

### Vastgelegde keuzes
- **Naam: "sleeve labels"** vervangt "print run" in de hele UI. Raakt sidebar, page titles, breadcrumbs, dashboard, README — in één pass (zie de rename-regel in CLAUDE.md).
- **Sidebar verdwijnt tijdens de flow.** Alleen een step-rail bovenin met vier stappen + één exit ("Save & close"). Een zichtbare sidebar nodigt uit tot wegklikken midden in de flow.
- **Bibliotheek hangt onder Home**, niet als concurrerende top-level nav. Records en Tracks blijven volledig bestaan als referentie — de bulk-override-grid op `/tracks` is te waardevol om te schrappen — maar zijn geen stap.

### De vier stappen
1. **Which records** — bij een lege collectie meteen syncen in plaats van eerst een modal met "Folder" en "Limit" tonen (operator-knoppen → "geavanceerd"). Het scherm ís de progress. Daarna selecteren, default "nieuw sinds laatste print" via `already_printed_ids`.
2. **Fill in the missing BPMs** — geen actie die naar de interne cascade genoemd is, maar de stand van zaken: "13 van de 54 tracks hebben nog geen BPM. [Ophalen]". Daarna review van alleen missers + disputed, inline corrigeerbaar. Overslaan moet altijd kunnen.
3. **Design** — instellingen links, **live label-preview rechts**. Drie presets (Minimal / DJ / Everything) boven de bestaande negen checkboxes. Goedkoopste grote winst: `render_release_stickers_svg` bestaat al en wordt al hergebruikt in de collection-drawer.
4. **Get them** — vel-preview + twee uitgangen: Download PDF, en "Order them printed" (zie *Print fulfillment & monetisatie* hierboven). Die tweede slot hoort er nu al te staan, desnoods als wachtlijst — anders eindigt de flow op een file-download in plaats van op het product.

**Home** vervangt het dashboard: één grote "New sleeve labels" met badge ("12 nieuwe platen sinds je laatste print"), daaronder je opgeslagen sets (opnieuw downloaden / dupliceren), daaronder de bibliotheek als typografische regel. Geen KPI-kaartenraster.

### Technische consequenties
- `run_bpm_cascade` (`webapp/records/services/bpm_cascade.py`) is collectie-breed. Stap 2 vraagt een `release_ids=`-parameter zodat alleen de selectie wordt opgehaald.
- `PrintRun` ontstaat nu pas bij Save. Een hervatbare flow vraagt een draft-status + huidige stap, anders is je werk weg als je de tab sluit.
- Tijdens de flow is een job geen achtergrondruis maar *de stap zelf*: de stap toont zijn eigen voortgang. De toast in `#run-banner` blijft voor wie wegnavigeert.
- De drie action-modals in `webapp/templates/actions/` verdwijnen als sidebar-ingang; hun opties worden step-content of "geavanceerd".

### Open vraag
- **Enkelvoud van het object.** "Sleeve labels" werkt als feature-naam, maar hoe heet één opgeslagen set? In het prototype staat er alleen een door de gebruiker gekozen naam + datum ("Summer restock · 14 Aug 2026"), wat het probleem omzeilt maar niet oplost — nodig zodra er een detailpagina of breadcrumb omheen moet.


## BPM-bronnen onderzoek (sep 2026)

Empirisch onderzoek naar de vraag "hoe krijgen we van élke plaat een BPM". Alle cijfers hieronder komen uit een benchmark van 30 tracks die de vorm van een Discogs-vinylcollectie nabootst (mainstream controls, house/techno-klassiekers, obscure 12"-B-kanten, disco-reissues, dub, ambient, modern). Meetscript + ruwe resultaten stonden in de scratchpad van die sessie; de conclusies staan hier.

### Wat we gemeten hebben aan de huidige cascade
| bron | hit-rate (n=30) |
|---|---|
| AcousticBrainz | 24/30 (80%) |
| SongBPM | 19/30 (63%) |
| Deezer | 12/30 (40%) |
| ReccoBeats | **0/30 (0%)** |
| **union (≥1 bron)** | **27/30 (90%)** |

Twee losse problemen die uit die meting vallen:
- **7 van de 30 tracks hangen op één enkele bron.** Die waarde is per definitie ongeverifieerd — geen tweede stem om tegen af te zetten. Dat is een groter kwaliteitsprobleem dan de 3 volledige missers.
- **De 3 volledige missers zijn allemaal vinyl-only** (Levon Vincent, DJ Sotofett, Call Super). Ze staan niet op Deezer/iTunes, niet in MusicBrainz. Geen enkele metadata-bron gaat die ooit dekken.

### Bronnen die zijn afgevoerd (met reden — niet opnieuw onderzoeken)
- **Tunebat, SongData.io, Musicstax, Chosic, GetSongBPM** — allemaal achter een Cloudflare-challenge ("Just a moment..."). Niet server-side scraapbaar zonder headless browser. GetSongBPM heeft een gratis API mét backlink-verplichting; de API-host zit óók achter Cloudflare, dus eerst vanaf de productie-host verifiëren voordat je er tijd in steekt.
- **Apple Music API** — geen tempo-veld op het Song-object. Bevestigd, niet alleen aangenomen.
- **Tidal** — OAuth vereist én geen BPM-veld.
- **Traxsource, Boomkat, Beatport-web** — Cloudflare 403.
- **Beatport API v4** — geen publieke client-credentials-tier; enkel `authorization_code` met de swagger-client_id, dus per-user browserlogin. Dat is precies waarom de bron nu uitstaat. Alleen zinvol als onderdeel van de per-user koppelingen hierboven.
- **ISRC-matching via MusicBrainz** — leek de elegante oplossing voor exact matchen, maar is een doodlopende weg: slechts **3 van de 30** MB-recordings dragen een ISRC, en er resolvede er **1** naar Deezer. MusicBrainz heeft simpelweg geen ISRC's voor underground vinyl.
- **YouTube (yt-dlp)** — zoeken werkt goed (4/5 exact de juiste video), maar audio-download geeft `HTTP 403 Forbidden`. Onbetrouwbaar én ToS-grijs. Hooguit noodgreep, nooit fundament.
- **Juno Download** — zou per juni 2026 gesloten zijn; niet hard geverifieerd, maar geen reden meer om erin te investeren.

### Wat wél werkt: audio zelf analyseren
De doorbraak is dat we niet langer afhankelijk hoeven zijn van de vraag "heeft iemand anders deze BPM al ergens gepubliceerd". `essentia` (`RhythmExtractor2013`, method=multifeature — dezelfde engine waar AcousticBrainz op draaide) installeert op macOS arm64 en levert op een **30s Deezer-preview**:
- 18/25 binnen 1,5 BPM van een bestaande bron, 21/25 binnen 3,5 BPM;
- de **confidence-waarde is een bruikbaar filter** — de twee ergste missers (Underworld "Born Slippy" → 172,3; Gas "Pop 1" → 112,1) hebben conf 0,54 en 0,36, terwijl alle goede uitkomsten boven 1,7 zitten;
- essentia verslaat librosa duidelijk: waar ze verschillen heeft essentia gelijk (Actress "Hubble": essentia 130,2 vs librosa 169,4).

**Hardwax is de verrassing.** Geen Cloudflare, en de audio-snippets zijn **90 seconden op 128kbps** (3× langer dan een Deezer-preview) én genoemd naar **vinyl-positie**: `media.hardwax.com/audio/88494_A1.mp3`. Dat mapt één-op-één op een Discogs-tracklist en lost daarmee het enige probleem op dat tekstmatching principieel niet kan oplossen: naamloze B-kanten en white labels ("Untitled A1"). Gevalideerd op 4 tracks met bekende BPM — 4/4 exact (Skee Mask 135,5 / Overmono 130,0 / Function 126,9 / Actress 130,1). Beperking: Hardwax' fulltext-zoekfunctie is zwak en de catalogus is beperkt tot wat ze ooit verkocht hebben.

**Bandcamp** levert volledige tracks (geen 30s-clip) via een publieke JSON-blob (`data-tralbum`) plus een publieke zoek-API, zonder auth of Cloudflare. Beste audiobron qua lengte.

### De belangrijkste waarschuwing
**Meer bronnen toevoegen maakt het resultaat slechter, niet beter, zolang de matchdrempel op `ARTIST_TITLE_MIN_FUZZ = 70` staat.** Bandcamp met die drempel leverde stelselmatig de verkeerde track: "Central – Six Five Two" voor Levon Vincent, "Timora" voor Call Super, "Under Belly" voor Blawan — allemaal fuzz 76–85, allemaal een plausibel ogende maar volstrekt foute BPM op de sticker. Elke nieuwe bron — audio helemaal — hoort achter een strengere poort (fuzz ≥ 90 plus een aparte artiestcheck) dan de huidige 70. En zelfs een titel-exacte match kan een andere versie zijn: Actress "Hubble" gaf via Bandcamp 116,9 tegen 130 uit drie andere bronnen.

### Concrete gebreken in de huidige code
- **`_spotify_search_best` slikt niet-200 responses stil in** (`continue` bij elke statuscode ≠ 200). Spotify geeft op dit moment `429 QUOTA_EXCEEDED`, dus ReccoBeats levert 0/30 — en erger: de track wordt daarna in `sources_tried` weggeschreven alsof de bron netjes "niks gevonden" zei. Dat vervuilt de cache permanent. Een 429/5xx hoort `SourceUnavailable` te gooien, niet `None` terug te geven.
- **`_canonical_octave` kent alleen ×2 / ÷2.** De 3:2-relatie (triplet/shuffle-feel) wordt niet afgevangen: Robert Hood "Minus" komt uit op 92,5 terwijl de bronnen 139 zeggen — precies ×1,5. Dat is de enige hoog-confidence misser in de hele meting, en hij is systematisch oplosbaar.

### Voorgestelde richting
1. Spotify-429 correct afhandelen (bugfix, los van al het bovenstaande).
2. `essentia` als bron toevoegen die op audio werkt in plaats van op namen, gevoed door Deezer-preview → Hardwax-snippet → Bandcamp-stream, met confidence-gate en een matchdrempel van 90.
3. Hardwax specifiek per **vinyl-positie** matchen in plaats van per titel — dat is de enige route naar naamloze tracks.
4. ×1,5 / ×⅔ toevoegen aan de octaaf-reconciliatie.

### Meetengines: wat installeert er eigenlijk (getest, sep 2026)
Van het hele veld overleven er **twee** een moderne Python (3.14, macOS arm64):
- **Essentia** (`RhythmExtractor2013`, multifeature) — klassieke DSP, geeft een `confidence`.
- **Beat This!** (CPJKU, ISMIR 2024) — transformer, pip-installeerbaar vanaf GitHub, CPU volstaat. Geeft beat-posities; tempo = 60/mediane interval, en de spreiding daarvan is je betrouwbaarheidsmaat.

Afgevallen op build-/dependency-fouten, niet op kwaliteit: **madmom** (oude numpy/Cython-API's), **aubio** (geen wheels, onbeheerd), **TempoCNN** (`ResolutionImpossible`, TF-pins).

**Draai ze allebei.** Op 10 head-to-head gevallen zijn ze het 7× eens tot op een tiende BPM, en in alle drie de botsingen wijst de zelfdiagnose van de verliezer hem aan: Robert Hood "Minus" — Essentia 92,5 (conf 3,13) vs Beat This! 138,7 (stab 1,00, correct); Skee Mask "Rev8617" — Beat This! 74,3 (stab 0,22) vs Essentia 135,0 (conf 1,72, correct); Gas "Pop 1" — beide weigeren, terecht. Regel: eens → klaar; oneens → volg de gezonde zelfdiagnose; beide onzeker → disputed.

Belangrijkste bijvangst: bij Theo Parrish "Falling Up" (bronnen 116 vs 126) en Omar S "Set It Out" (bron 118) komen beide luisteraars onafhankelijk op 123. **Op deze catalogus is zelf meten niet de noodoplossing maar de betrouwbaarste stem.**

### Rekordbox als bron
`~/Library/Pioneer/rekordbox/master.db` is SQLCipher-versleuteld maar `pyrekordbox` opent hem zonder gedoe. BPM staat als honderdsten (`12800` = 128,00), plus `KeyName` en `AnalysisDataPath` naar het beatgrid. Dat is potentieel de beste bron die er is — geanalyseerd én met de hand gecorrigeerd. Vandaag marginaal: de bibliotheek telt 32 tracks en de meeste hebben `BPM=0`. Goedkope import zodra die groeit; niet nu op bouwen.

### Operationeel detail
**Deezer's preview-URL's zijn tijdelijk getekend** en geven na ongeveer een uur `403`. Niet de URL cachen — op analysemoment opnieuw resolven.

### Uitvoeringsstatus (okt 2026)
Gedaan: Spotify/ReccoBeats 429/5xx geeft nu `SourceUnavailable` (geen cache-vervuiling meer); Discogs `videos` worden opgeslagen (`Release.videos`) en zijn de eerste route voor audio-analyse, gematcht op titel (≥90) + duur (±10%).

Nog te doen, in volgorde:
1. ~~**Essentia + Beat This! in plaats van librosa**~~ — **Essentia gedaan (22 okt 2026), Beat This! bewust uitgesteld.** Gemeten op drie Deezer-previews uit de benchmark:
   | track (ref) | librosa | Essentia (conf) | Beat This! (stabiel) |
   |---|---|---|---|
   | Overmono "So U Kno" (130) | 130,0 | 129,6 (3,67) | 130,0 (64/64) |
   | Robert Hood "Minus" (139) | **92,5** | **92,5** (3,13) | 138,6 (69/69) |
   | Skee Mask "Rev8617" (135) | **106,4** | 135,0 (1,72) | weigert (0/36) |
   | piekgeheugen per proces | 386 MB | 197 MB | 515 MB |

   librosa gaf twee van drie fout en de oude stabiliteitsdrempel liet beide door; Essentia is lichter, sneller en beter. Drempel: confidence ≥ 1,5.
   - *Waarom Beat This! nog niet:* torch past niet in een Render-starter (512 MB). Het vraagt een groter worker-plan (~$18 per maand meer), een image die ~800 MB groter is (CPU-torch) en een checkpoint van 81 MB in de image. Dat komt pas als metingen op de echte collectie laten zien dat audio vaak de doorslag geeft.
   - *Correctie op "Draai ze allebei":* bij "Minus" zien béíde engines er gezond uit (Essentia conf 3,13, Beat This! 69/69 stabiel) en zijn ze het oneens. 92,5 × 1,5 = 138,75, dus het is een maatsoort-dubbelzinnigheid. "Volg de gezonde zelfdiagnose" beslist dat niet. Bij twee engines wordt de regel: eens → klaar; één weigert → de ander; beide gezond maar oneens → `disputed`.
   - *Restrisico nu:* een zelfverzekerde 3:2-fout van Essentia. Tegen metadata wordt die `disputed` (items 3 en 4); bij vinyl-only tracks zonder metadata komt hij als enkele bron, zonder stip, op de sticker.
   - *Lokaal:* Essentia heeft alleen wheels voor Python 3.14 (macOS 15+, Linux x86_64). Docker en CI zijn daarom naar 3.14 gegaan. Op een Apple Silicon-Mac bouw je de image met `--platform linux/amd64`.
2. ~~**Audiobronnen-keten**~~ — **gedaan in kleinere vorm (23 okt 2026)**: Discogs-video → **iTunes-preview** → YouTube-zoek. Deezer-preview, Hardwax en Bandcamp zijn geschrapt, op basis van een meting op de echte collectie in plaats van de benchmark van 30 tracks.

   **Wie bereikt audio überhaupt?** 227 van de 445 tracks (51%) hebben 0 of 1 metadata-BPM: 97 zonder, 130 met één bron. In de benchmark was dat 10%. Op deze collectie is audio dus voor de helft van de tracks de enige tweede stem, geen randgeval.

   **Dekking per bron op die 227**, met strikte match (artiest en titel elk fuzz ≥ 90, duur ±10%):
   | bron | dekking | extra na de bronnen ervoor | eens met de ene metadata-BPM (na gate) |
   |---|---|---|---|
   | Discogs-video | 174 (76%) | 174 | 13/15 (steekproef); 15/15 downloads gelukt, ~3 s per stuk |
   | iTunes-preview | 155 (68%) | **+35** | 78/94 (83%) |
   | Deezer-preview | 122 (53%) | +0 | 69/80 (86%) |
   | Hard Wax, per vinylpositie | 9 (3%) | +0 | – |
   | Bandcamp | 90 (39%) | +2 | matcht verkeerde versies ("diapason-mixed", "disco-dynasty-edit"); de zoek-API geeft geen duur |
   | geen enkele bron | 16 | – | bijna allemaal remixen, acapella's, radio-edits |

   Discogs-video + iTunes dekken samen 209/227 (92%). Wat overblijft vindt alleen YouTube-zoek misschien nog (vereist `YOUTUBE_API_KEY`).
   - *Versieregel (na de agent-review van PR #13):* fuzzy titelscores laten een remix door waarvan de titel het origineel bevat ("Droogs" zit helemaal in "Droogs (Paleman Remix)"), en 43% van de tracks heeft geen Discogs-duur om dat op te vangen. Daarom moeten alle drie de routes het eens zijn over de **productie-tags**: remix-, dub-, rework-, live- en acapella-groepen met hun naam erbij, fuzzy vergeleken, omdat namen verschillend gespeld worden ("Errick" / "Errik"). Lengte- en remaster-tags tellen niet mee, want die houden het tempo gelijk: "(Long Version)", "(Extended Mix)", "(12\" Version)", "[2011 Remaster]".
   - *Effect, opnieuw gemeten:* Discogs-video 170 van 173 behouden. De 3 die afvallen waren fout: een acapella-video bij een dub, een "Progressive Remix" bij het origineel, en een ambigue "1979 Re-Mix". "Libera (Benjamin Fröhlich Remix)" zat eerst aan de originele "Libera"-video vast en krijgt nu de remix. Voor iTunes wordt daarnaast de songnaam zonder haakjes **symmetrisch** vergeleken (tweede review): "Freak!" van Trevor Rockcliffe matchte eerst "Freak Like This". iTunes houdt 145 van de 155; de 9 die afvallen zijn live-opnames, een Axwell-remix, afwijkende duren en "Freak Like This", plus 2 onterecht ('Mutation Mix "Skywalker"', "More, More, More, Pt.1"). **Samen nu 205/227 (90%).**
   - *Eerste poging, verworpen:* een symmetrische titelvergelijking. Die verloor 70 van de 173 goede video-matches (YouTube-titels vol posities, catalogusnummers, "HQ HD"), en wees "Hasta Luego Trinidad (Long Version)" af, terwijl dat met 288 s tegen 287 s juist de goede opname was.
   - *Correctie op het onderzoek:* "yt-dlp geeft HTTP 403" gold niet voor de Discogs-video's van deze collectie: 15/15 downloads lukten. Blijven volgen, want YouTube verandert dit vaker.
   - *Bijvangst:* audio lost ook remix-collapse op. AcousticBrainz gaf alle vier de Vapourspace-remixen 134; audio meet 142,8 / 134,7 / 135,8 / 134,9.
   - *Hardwax afgevoerd als bron:* 8 van de 116 releases, en geen enkele track die niet al door een andere bron gedekt werd. Het idee "enige route naar naamloze B-kanten" klopt op deze collectie niet: de gaten zijn remixen, geen white labels. Heroverwegen als een tester een collectie vol white labels heeft.
   - *Juridisch grijs:* iTunes-previews zijn bedoeld om af te spelen, niet om te analyseren. Hetzelfde geldt voor Deezer-previews en YouTube. We slaan geen audio op, alleen het getal.
3. ~~**Audio als verificatie van `single`-hits**~~ — **gedaan, als bijeffect van PR #2.** Audio draait voor tracks met 0 of 1 metadata-hit; sinds `_align_audio_bpm` weg is, wordt een botsing tussen audio en die ene bron `disputed` (geen stip).
4. **×1,5 / ×⅔ in `_canonical_octave`** en `_octave_consensus` (Robert Hood "Minus"). Dit hoort in de consensus met een eigen confidence, **nooit** als het bijbuigen van een meting naar een andere bron. Dat deed `_align_audio_bpm` tot okt 2026. Een foute songbpm-match van 100 trok een audiometing van 67 mee (×1,5), en de sticker drukte 100 met de vertrouwensstip. Verwijderd na de eerste agent-review van PR #2.
5. **Rekordbox/Serato/Traktor-import** en bestand-upload ("analyseer mijn rip") als gebruikerslaag; ReccoBeats kan ook een geüpload bestand analyseren (nauwkeurigheid onbekend).
6. **Agent-fallback** voor wat overblijft: eerst programmatisch alle bronnen doorlopen, en pas als dat niets oplevert een AI-agent die de BPM echt op het web gaat opzoeken. Geopperd met N8N of vergelijkbare orchestratie.
   - *Tegenargument om eerst te beslechten:* dit is precies de plek waar een plausibel ogende maar foute BPM op de sticker belandt — zie "De belangrijkste waarschuwing" hierboven. Een agent heeft geen `confidence` en geen tweede stem. Als dit er komt, dan met bronvermelding per waarde en een zichtbare "ongeverifieerd"-status in stap 2, niet als stille zesde bron in de cascade.
   - *Tegenargument op N8N:* een externe orchestrator is een tweede runtime en een tweede vendor, tegen het stack-uitgangspunt hierboven. Een Django-Q2-taak die een API-call naar een agent doet, levert hetzelfde zonder die last.
7. **Gedeelde BPM-cache over users heen.** `BpmCache` bestaat al en doet meer dan het oorspronkelijke idee ("eenmaal een BPM opgehaald = aan cache toevoegen"): hij is gekeyed op een genormaliseerde artist+title-hash, dus dezelfde song op single én LP én compilatie deelt één rij. Wat er *niet* is, is hergebruik tussen gebruikers — `unique_together = (user, cache_key)` betekent dat elke nieuwe user de hele cascade opnieuw draait voor tracks die we al weten.
   - Winst: een nieuwe user heeft bij eerste gebruik meteen BPM's, en de externe bronnen worden fors minder belast (relevant bij rate limits als Spotify's 429).
   - *Spanning om eerst op te lossen:* dit doorbreekt de tenantgrens die in "Hardening & onderhoudslast" juist strakker wordt getrokken, en het raakt het uitgangspunt over klantdata. Werkbare vorm: een aparte, **niet-user-gebonden** `GlobalBpm`-tabel met alleen genormaliseerde artist/title + waarde + bron (geen user-FK, geen collectie-informatie), die als bron *vóór* de externe bronnen in de cascade zit. Per-user `Override` blijft altijd winnen. Een user mag zijn eigen handmatige correcties niet ongevraagd doneren — dat is opt-in of helemaal niet.

Let op: `is_cached_release` eist nu ook `videos`, dus de eerstvolgende Discogs-sync haalt bestaande releases één keer opnieuw op (1 API-call per release) om de video's aan te vullen.

### Open vragen
- **Waar draait de analyse?** Essentia in de Django-Q2-worker betekent een zwaardere dependency en trage jobs (download + decode + analyse per track). Alternatief is een aparte analyse-worker of een on-demand "analyseer deze track"-knop in plaats van collectie-breed.
- **Welke waarde wint** bij een dispuut? Vandaag staat `audio_analysis` vooraan in `SOURCE_PRIORITY`, dus bij een botsing drukt de sticker de audiometing, zonder stip. Dat is librosa op een YouTube-upload, die ondanks fuzz ≥90 en ±10% duur een remix of live-versie kan zijn. Opgemerkt in de agent-review van PR #2; bewust nog niet veranderd, want het hoort bij item 1–3.
- **Welke waarde wint** als essentia en een metadata-bron elkaar tegenspreken? De meting suggereert dat essentia vaker gelijk heeft dan de losse bronnen, maar dat is op n=30 gemeten, niet op de echte collectie.
- ~~**Hardwax-matching op catalogusnummer**~~ — **getest (okt 2026): werkt niet.** Zoeken op catalogusnummer gaf voor geen enkele release een resultaat (Hardwax noteert vaak alleen het nummer, "X 004"; reissues hebben een ander catno). Zoeken op "artiest titel" plus titelmatch vond 8/116. Zie item 2.

### Efficiënter en betrouwbaarder ophalen (besproken 23 okt 2026)

**Alleen het audiofragment ophalen** — De huidige implementatie downloadt de volledige audiostream en knipt daarna 120 seconden vanaf seconde 30 uit. Onderzoek gedeeltelijk downloaden via yt-dlp `--download-sections` + ffmpeg. Begin met een fragment van 30–45 seconden buiten de intro; haal bij twijfel een tweede fragment elders op en verleng alleen indien nodig. Fragmentlengte en selectie zijn hypotheses: vergelijk tegen de huidige 120 seconden met onafhankelijk gecontroleerde BPM's, meet ook werkelijk gedownloade bytes, latency en fouten. Twee fragmenten van dezelfde opname zijn geen twee onafhankelijke bronnen.

**Extra onderzoek bij onzekerheid** — Audio wordt nu alleen gestart bij hoogstens één metadata-hit, ook als twee hits elkaar tegenspreken. Laat ontbrekende BPM, zwakke versiematch en conflicten bepalen of aanvullende bronnen/audio nodig zijn. Overweeg gefaseerde metadata-lookups met een stopregel bij voldoende betrouwbaar bewijs; houd hervatbaarheid en de cache-compleetheidsregel daarmee consistent.

**Bronkeuze op bewijs** — Audio niet automatisch laten winnen bij een dispuut. Weeg eerst de juiste opname/versie, dan onafhankelijke overeenstemming en meetkwaliteit. Bewaar fragmentlocatie, ruwe BPM en analyseversie. Presenteer onopgeloste halve/dubbele tempo's of conflicten als controlepunt. Beoordeel bronkwaliteit tegen handmatig gecontroleerde referenties, niet uitsluitend overeenstemming met andere bronnen.

## UX proposals (from the Oct 2026 interface research)

Ordered by impact over effort. Each is a flow-level change; none needs new engine logic except the stock picker.

- ~~**Sheet preview in step 3**~~ — *superseded (Oct 2026) by "Stickerontwerp" punt 1: the main preview shows the label on the sleeve, not the sheet.* Original idea: show the real A4 sheet with actual labels (and a picker for which release to preview) instead of one label only. Same render pipeline as the PDF, so the preview is the PDF.
- **Label stock picker** — replace free width/height/columns/rows with a list of known sheet templates plus "Custom" behind a disclosure. Default stays today's 96 × 50.8, 2×5.
- **Step 2 keyboard and triage** — Enter/Tab moves to the next BPM field, rows with a measurable gap first, an explicit "print without BPM" for the rest. Check that typed overrides are saved per row, not only on Continue.
- **Undo instead of confirm** — "Done" (marks records as printed) becomes a toast with Undo; same for Clear on step 1. Keep a real confirm only for deleting a set.
- **Smart selections in step 1** — one-click selections ("New since last print", "Missing BPM", Discogs folders as crates), shift-click range select, sticky selected count.
- **Step 4 as review + ending** — summary lines with per-step Edit links (records, BPM gaps, design, stock), print tips (A4, 100% scale), and a calm confirmation after Done with "Download again".
- **Resume card on Home** — show the four steps with their state on the draft card, not just "step N".
- **Open questions** — Camelot-coloured keys on the sticker (common in DJ tools, not verified by the research); whether a command palette is worth it for non-developer users (current view: no).

## Feedback van testers

Mensen uit het eigen netwerk gaan de app op staging testen en moeten **in de app zelf** feedback kunnen geven. Een agent pikt die feedback op, en reageert eventueel zelf met vragen of suggesties.

### Uitvoeringsstatus (21 okt 2026)
Gedaan:
- `render.yaml` deployt van `main`. De Docker-build faalde bovendien: hij kopieerde `sleeve_notes_web/`, verwijderd in `ea7dc03`. Lokaal gebouwd en gedraaid met `ENVIRONMENT=staging`: healthz 200, Basic Auth 401/200, worker start.
- Feedback v1 zoals hieronder voorgesteld: app `feedback`, knop in top bar en step rail, eigen tabel, 90 dagen bewaartermijn via een dagelijkse Q2-schedule, uitlezen met `manage.py list_feedback --json`. De open vragen zijn zo beantwoord: **eigen tabel**, en **ook in productie aan** (uit te zetten met `FEEDBACK_ENABLED`); er is geen reden om feedback van echte gebruikers te weigeren.

Nog te doen, in het Render-dashboard (niet vanuit code):
1. Een staging-only `FIELD_ENCRYPTION_KEY` genereren, in Bitwarden zetten, en op web én worker invullen. Zonder die sleutel start geen van beide.
2. Database terugzetten indien verwijderd (Blueprint sync), web en worker resumen. Zie RUNBOOK → Resume staging.
3. Backups/PITR aanzetten en één restore oefenen (uit stap 1).
4. Testers de Basic Auth-gegevens geven.
- *Restpunt:* de Django-admin heeft op staging geen opmaak, want er is geen `collectstatic`/whitenoise. Bruikbaar maar lelijk; pas oplossen als de admin vaker nodig is.

### Voorwaarden (blokkeren het nu)
- **Staging staat uit** (gepauzeerd om kosten te sparen, zie `RUNBOOK.md`) én `render.yaml` deployt nog van branch `django-port`, die sinds de merge naar `main` niet meer bestaat. Hervatten zonder die branch om te zetten geeft een kapotte deploy.
- **Hardening eerst** (zie "Volgorde"): testers zetten hun Discogs-token in onze database.

### Voorstel v1
- Eén vaste "Feedback"-knop in de top bar en in de step rail, die een modal opent met één tekstveld.
- Automatisch meesturen wat nodig is om het na te spelen: URL, flow-stap, `PrintRun`-id, tijdstip, user-id. **Geen** screenshots of formulierinhoud: dat is klantdata die je niet nodig hebt.
- Opslaan in een eigen `Feedback`-tabel met een bewaartermijn. "Een agent pikt het op" is dan gewoon: Claude Code de tabel laten lezen. Daarvoor hoeft niets gebouwd te worden.

### Challenge op het agent-deel
- **Een agent die zelf terugschrijft naar testers: niet in v1.** Het zijn mensen uit je netwerk die je een gunst doen. Een automatisch antwoord met vervolgvragen voelt als een bot, en verkeerde suggesties ("dit kun je al via X") kosten goodwill die je bij een handvol testers niet terugkrijgt. Zelf antwoorden schaalt prima tot tientallen testers.
- Wat wél waarde heeft: een agent die de feedback **voor jou** clustert en aan roadmap-items koppelt.

### Open vragen
- **Tabel of GitHub-issue?** Een issue is nul code, maar als de repo publiek is, wordt elke feedback publiek, met naam. Bij twijfel: eigen tabel.
- **Alleen op staging, of ook later in productie?** Bepaalt of de knop achter een `ENVIRONMENT`-check moet.

## Huisstijl

Idee: een huisstijl toevoegen, eventueel eerst een design system maken.

### Challenge: het design system bestaat al, de huisstijl niet
Er ligt al een design system in embryonale vorm: kleur- en vlaktokens in `base.html` `:root`, productwaarheid in `PRODUCT.md`, een lijst bewust vermeden patronen in CLAUDE.md, en de Impeccable-skill die daarop leunt. Een apart design system bouwen (componentbibliotheek, Storybook-achtig) is voor één maintainer met server-rendered templates onderhoud zonder afnemer.

Wat ontbreekt is de **merklaag**: wordmark, een eigen typeface, één herkenbaar element, en een toon. Dat is klein in code (een font, een SVG, een paar tokens) en groot in effect.

### Voorstel
- Eerst de bestaande tokens vastleggen als `DESIGN.md` (de Impeccable-documenter doet dat vanuit wat er staat), daarna de merklaag erop zetten. Zo vervangt de huisstijl tokens en hoeft er niets herschreven te worden.
- Na de eerste testerfeedback, niet ervoor (zie "Volgorde").

### Open vraag
- **Komt de huisstijl op de sticker zelf?** De sticker is het enige wat anderen ooit van de app zien (op een hoes, in een DJ-booth). Een klein merkteken is gratis zichtbaarheid, maar gebruikers willen waarschijnlijk geen logo op hun platen. Hooguit opt-in. De QR-code wijst nu rechtstreeks naar de Discogs-release (`DISCOGS_RELEASE_URL` in `sticker_layout.py`). Die via een eigen redirect laten lopen geeft wel een plek voor het merk, maar dan hangt elke ooit geprinte sticker voor altijd aan ons domein. Niet doen zonder die afweging.

## Hardening & onderhoudslast (okt 2026)

Drie items uit het stack-gesprek van 21 okt 2026. De conclusie van dat gesprek was dat de stack-keuze (Django 5.2 LTS + Postgres + één worker, server-rendered met HTMX) de juiste is — de reden is dat de renderlaag Python *is en blijft* (ReportLab-typografie heeft in JS geen gelijkwaardige tegenhanger) en dat Discogs **OAuth 1.0a** gebruikt. Bij geen van de gangbare auth-vendors (Supabase, Clerk, Auth0, Firebase) staat Discogs als kant-en-klare provider, en generiek OAuth1 is daar hooguit een nichepad via een custom connection. Dan heb je een extra vendor voor iets wat de eigen allauth-provider in ~30 regels al doet, en de tokens moeten alsnog naar de worker voor API-calls namens de user. Het interessante werk zit dus niet in de stack maar hieronder.

### Uitvoeringsstatus (21 okt 2026)
Gedaan:
- **Tokens versleuteld.** Eigen `DiscogsConnection`-model met een `EncryptedTextField` (`core/fields.py`, Fernet via `cryptography`, `MultiFernet` voor rotatie), `SOCIALACCOUNT_STORE_TOKENS=False`, tokens gevangen via het `user_logged_in`-signaal. Een datamigratie verplaatst de bestaande tokens en verwijdert de platte `SocialToken`-rijen. Bewust **niet** `django-cryptography`: dat pakket wordt nauwelijks onderhouden en ondersteunt Django 5 niet officieel.
- **Sentry**: `send_default_pii=False` én `include_local_variables=False`, plus een middleware die alleen het user-id meestuurt. Het tweede bleek nodig: Sentry's scrubber maskeert alleen exacte sleutelnamen (`token`, `secret`), dus `token_secret`, `tok_sec` of een `user` met de username als repr gingen anders mee in elke traceback.
- **Tenant-isolatietest** (`core/tests_tenant_isolation.py`): user B loopt alle `<uuid:…>`-routes af met A's id's (GET en POST), plus de routes die id's in body of query krijgen. Een nieuwe UUID-route zonder dekking laat de test falen. Geverifieerd door tijdelijk een filter te verwijderen: de test werd rood.
- `requirements.txt` verwijderd.

Nog open:
- **Backups + PITR + één restore-oefening.** Infra, geen code; doen bij het hervatten van staging (Volgorde stap 2). `FIELD_ENCRYPTION_KEY` moet daar ook gezet zijn, anders start de app niet.
- **Uptime-check op de worker.** Nog open.
- ~~**Dependabot + `pip-audit`**~~ — **gedaan (22 okt 2026)**: CI (`.github/workflows/ci.yml`) draait tests + `pip-audit` op elke PR, en Render deployt staging alleen bij groene checks. De eerste audit vond 53 kwetsbaarheden in vijf pakketten (urllib3, Pillow, oauthlib, PyJWT, certifi). Ze waren alleen op te lossen met een major-upgrade, en de oude `urllib3<2`-pin blokkeerde de eerste. Na de upgrade zijn de tests, een echte PDF-render en een echte Discogs OAuth-call gecontroleerd.
- *Restrisico, klein:* als een login pauzeert (signup-formulier bij een naamsbotsing, e-mailverificatie) zet allauth de hele `sociallogin` inclusief token geserialiseerd in de sessie, en sessies staan in de DB. Tijdelijk en zeldzaam met `AUTO_SIGNUP=True`. Alleen oppakken als we ooit e-mailverificatie of MFA aanzetten.

### 1. Tenantgrens structureel maken in plaats van disciplinair
Vandaag is multi-tenancy een *gewoonte*: elke view moet `filter(user=request.user)` onthouden (zie CLAUDE.md). Eén vergeten filter is een cross-tenant leak, en UUID-PKs zijn daar geen vangnet voor — alleen een rem op enumeratie.

- *Uitgesteld (21 okt 2026), met reden:* een audit liet zien dat alle ~40 querysites al filteren, inclusief de paden met id's in de POST-body. De omzetting naar `for_user()` zou een grote diff zijn die niets repareert, terwijl de test hieronder het eigenlijke vangnet is en nu bestaat. Oppakken als de test ooit een echte lek vindt, of samen met RLS.
- Een `TenantQuerySet.for_user(user)` op de user-owned modellen (`Release`, `Track`, `BpmCache`, `Override`, `PrintRun`, `AuditEvent`), en views gebruiken alleen die. **Niet** de default manager `objects` laten weigeren of leeg teruggeven: Django gebruikt `_default_manager` voor admin, `dumpdata`, ModelForm-keuzelijsten en management commands, en die breken dan stil.
- **De test is het eigenlijke vangnet**: twee users in een fixture, user A heeft van elk model een object, en een test loopt alle URL-patronen af met A's UUID's terwijl user B is ingelogd. Alles behalve 404/403 is rood. Een leak moet in CI falen, niet in review opvallen.
- Eventueel Postgres RLS (`SET LOCAL app.user_id` per request) als tweede laag. Expliciet *als vangnet*, niet als primaire mechaniek — RLS is policy-onderhoud per tabel en dus meer code om te onderhouden dan één manager.

### 2. Discogs-tokens versleutelen at rest
`SOCIALACCOUNT_STORE_TOKENS=True` zet de per-user Discogs access token + secret plat in de `SocialToken`-tabel. Een DB-dump of een lekkende backup is daarmee direct API-toegang namens elke ingelogde gebruiker. Dit is het grootste concrete datarisico in de app en volgt direct uit het uitgangspunt "zo weinig mogelijk klantdata opslaan" bovenaan dit document.

- Versleutelde velden (`django-cryptography` of een eigen `Fernet`-veld) met de sleutel uit de environment, niet uit de DB. Dit is dezelfde keuze die de per-user BPM-koppelingen hierboven al nodig hebben → één keer goed doen, twee features bedienen.
- Backups + PITR aanzetten op de managed Postgres, en **één keer een restore oefenen**. Ongeteste backups zijn geen backups.
- Open vraag: allauth's `SocialToken` is niet ons model. Opties zijn een eigen `DiscogsConnection`-model dat de tokens overneemt (en `SOCIALACCOUNT_STORE_TOKENS` uitzetten), of een migratie die het bestaande veldtype vervangt. Het eigen model sluit beter aan op de `*Connection`-architectuur die bij de BPM-koppelingen hoort.

### 3. Jobs-backend: django-q2 heroverwegen
`django-q2` zit in het kritieke pad (elke sync, elke cascade) en is een klein, single-maintainer pakket — plus de bron van de macOS-fork-crash die in CLAUDE.md gedocumenteerd staat. Django 6.0 (dec 2025) heeft een ingebouwd Tasks-framework (`django.tasks`), maar levert zelf alleen een `ImmediateBackend` en `DummyBackend`: geen worker. Een database-backend met worker zit in het losse pakket `django-tasks`, dat óók een klein pakket is. Overstappen vóórdat zo'n backend in Django zelf zit, ruilt dus de ene kleine afhankelijkheid in voor de andere.

- **Koppelen aan de LTS-upgrade.** We zitten op 5.2 LTS (security-support tot april 2028). 6.0 en 6.1 zijn geen LTS; 6.2 wordt de volgende (verwacht april 2027). Op dat moment kijken of er een DB-backend in Django zit: zo ja, meenemen in dezelfde upgrade; zo nee, op django-q2 blijven.
- Wel nu al nakijken of `django.tasks` het patroon van `webapp/records/jobs.py` aankan: progress-logging naar `UserJobLock`, `finally`-ack, hervatbaarheid.
- Migratiepad is beperkt in omvang omdat alle enqueues al achter `enqueue_*`-helpers zitten en alle voortgang via `_make_progress_logger` loopt — de abstractie staat er al.

### Randvoorwaarden die bij deze drie horen
- **Sentry: `send_default_pii=True` uitzetten.** Sentry is al geconfigureerd (`webapp/sleevenotes_app/settings.py`, actief zodra `SENTRY_DSN` gezet is), maar met die vlag gaan usernames, IP-adressen, cookies en request-data naar een derde partij. Dat botst rechtstreeks met het klantdata-uitgangspunt, en het maakt Sentry een verwerker waarvoor je een verwerkersovereenkomst nodig hebt. Een user-id zonder naam is genoeg om een fout te herleiden.
- **Uptime-check op de worker**, niet alleen op web. Stille workerdood is de klassieke faalmodus van deze architectuur en precies wat de fork-crash veroorzaakte.
- **Renovate/Dependabot + `pip-audit` in CI.** Dat is het hele security-maintenance-verhaal, geautomatiseerd. Blijf op de Django LTS-track: één serieuze upgrade per ~2 jaar in plaats van een continu upgradeproject.
- **`requirements.txt` weggooien.** Het wordt nergens gebruikt: de Dockerfile doet `pip install .` vanuit `pyproject.toml`, en `render.yaml` verwijst er niet naar. Het bevat nog `fastapi`/`uvicorn`/`jinja2`/`itsdangerous` en mist alle Django-deps. Laat je het staan, dan opent Dependabot PR's en meldt `pip-audit` kwetsbaarheden voor pakketten die niet eens geïnstalleerd worden.

## Merkidentiteit

**Nieuwe naam + brand** — "Sleeve Notes" voelt generiek ("notes"). Gekozen basis: lichtgrijze "hardware"-uitstraling met oranje als LED (alleen actief/aandacht). Drie richtingen zijn uitgewerkt als mock-up (A Faceplate, B Signal, C Sleeve). Naamkandidaten: Deadwax, Downbeat, Spine, Cuedot, Runout, Crate Label (beschikbaarheid nog niet gecontroleerd).
*Challenge* — De app wordt overdag aan een bureau met printvel gebruikt; een volledig donkere app past daar niet. Brand dus vooral in landing, stickers en cijfertypografie. Een naamswijziging raakt UI, README, CLI-entrypoint (`sleeve-notes`) en OAuth-appnaam bij Discogs: pas doorvoeren als de naam vaststaat.
