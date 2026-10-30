# Werker

Sorteert zakelijke mail en WhatsApp-berichten, en stelt antwoorden voor. Prijzen komen uit een vaste lijst; een antwoord met een bedrag dat niet op die lijst staat, wordt geblokkeerd.

## Hoe het werkt

1. **Ophalen**: mail via IMAP of Gmail API (`werker sync`); WhatsApp via de webhook (`werker serve`).
2. **Regels**: eigen adressen worden overgeslagen; blocklist en nieuwsbrieven (`List-Unsubscribe`) krijgen een label zonder AI-aanroep.
3. **Classificatie** (`claude-haiku-5-5`): intent, urgentie, taal en zekerheid.
4. **Concept** (`claude-sonnet-5-5`): alleen voor prijsvragen, afspraken, klachten, facturen en overig. Het model krijgt de prijslijst mee.
5. **Prijscontrole**: elk bedrag in het concept moet exact op de prijslijst staan.
6. **Beleid**: standaard wordt niets verzonden. Je keurt concepten goed met `werker approve`.

Klachten en facturen gaan altijd naar een mens, ook als auto-verzenden aan staat.

## Installatie

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # voor Gmail: pip install -e ".[dev,gmail]"
pytest
```

## Omgevingsvariabelen (optioneel, voor servers)

| Variabele | Doel |
|---|---|
| `ANTHROPIC_API_KEY` | Sleutel voor de Claude API. |
| `WERKER_DB` | Pad naar de SQLite-database (standaard `werker.db`). |
| `WERKER_PRICES_CSV_URL` | Google Sheet als CSV-export. Lege waarde = lokaal bestand. |
| `WERKER_PRICES_FILE` | Lokaal prijsbestand (YAML), standaard `prices.example.yaml`. |
| `WERKER_OWN_ADDRESSES` | Eigen adressen, komma-gescheiden; deze worden overgeslagen. |
| `WERKER_BLOCKLIST` | Adressen die als spam gelabeld worden. |
| `WERKER_AUTOSEND` | `true` om auto-verzenden aan te zetten. Standaard `false` (kill switch). |
| `WERKER_AUTOSEND_INTENTS` | Intents die automatisch mogen verzenden, standaard `prijsvraag`. |
| `WERKER_MIN_CONFIDENCE` | Minimale zekerheid voor auto-verzenden, standaard `0.85`. |
| `WERKER_MAX_AUTO_AMOUNT` | Bedragen boven deze grens gaan naar een mens, standaard `500`. |
| `WERKER_IMAP_HOST`, `WERKER_IMAP_USER`, `WERKER_IMAP_PASSWORD`, `WERKER_SMTP_HOST`, `WERKER_SENDER_ADDRESS` | Mail via IMAP/SMTP. |
| `WERKER_GMAIL_TOKEN` | Pad naar een OAuth-token voor Gmail (scopes: gmail.readonly, gmail.modify, gmail.send). |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Business Cloud API. |
| `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | Voor webhook-verificatie. |
| `WERKER_UI_PASSWORD` | Wachtwoord voor de inbox (HTTP-basisauthenticatie, gebruikersnaam is wat je wilt). Verplicht als je op een netwerk-interface luistert. |

## Gebruik

```bash
werker prices                       # toon de prijslijst zoals het programma die ziet
werker process-eml voorbeeld.eml    # test op een bestand; verzendt nooit
werker sync                         # haal nieuwe mail op en verwerk die
werker drafts                       # concepten die op goedkeuring wachten
werker approve 3                    # keur concept 3 goed en verstuur het
werker approve 3 --text aangepast.txt
werker reject 3
werker reset-password anna          # nieuw wachtwoord voor teamaccount anna (minstens 8 tekens)
werker serve --port 8000            # inbox op http://127.0.0.1:8000 + WhatsApp-webhook
```

`sync`, `approve` en `reject` gebruiken dezelfde instellingen als de webpagina: mailboxen, sleutels en instellingen uit het instelscherm (de datamap naast `WERKER_DB`) tellen mee, naast de omgevingsvariabelen.

## De inbox

Open `http://127.0.0.1:8000`. Links staan de categorieën met aantallen, in het midden de lijst, rechts het bericht met de samenvatting en het concept-antwoord.

- Wijzig het concept gerust; de tekst die je goedkeurt is de tekst die verzonden wordt.
- Bij een prijs die niet op de lijst staat, krijg je een waarschuwing en moet je expliciet bevestigen.
- Sneltoetsen: `j` / `k` om door de lijst te bewegen, `/` om te zoeken.
- Synchroniseer haalt nieuwe mail op en verwerkt die direct.

De pagina gebruikt geen externe bestanden; alles staat in `src/werker/static/index.html`.

## Instellingen

Alles wat je nodig hebt staat op het instelscherm (tandwiel in de inbox): API-sleutel, automatisch verzenden, mailboxen, WhatsApp en iMessage. Wijzigingen gelden meteen, zonder herstart.

- Niet-geheime instellingen komen in de database.
- API-sleutels en wachtwoorden komen in `secrets.json` in de datamap (alleen leesbaar voor jouw gebruiker). Ze worden nooit teruggetoond; het scherm laat alleen zien dat ze ingesteld zijn.
- Waarden uit de omgeving (bijvoorbeeld Docker) gelden als standaard; het instelscherm wint.

De app start ook zonder API-sleutel, zodat je eerst de instellingen kunt invullen.

## Klanten

Berichten worden automatisch aan een klant gekoppeld op basis van het e-mailadres of telefoonnummer. Elke klant heeft een tijdlijn van alles wat er via mail, WhatsApp en iMessage binnenkwam, met de bijbehorende concepten. Een klant hernoem je, voorzie je van notities, of voeg je samen met een andere (bijvoorbeeld als iemand van twee adressen schrijft). Een Nederlands nummer in de vorm 06..., +31 6... of 0031 6... wordt als hetzelfde nummer herkend. Nummers die al in de database staan worden bij het opstarten omgezet. Hoort hetzelfde nummer daardoor bij twee klanten, dan blijven beide staan en meldt het logboek dat je ze moet samenvoegen.

## Gerichte aanpassing en inplannen

- **Vervolgvragen:** stuurt een klant een tweede bericht in dezelfde draad, dan past Werker het lopende concept alleen op de juiste plekken aan. De rest blijft staan. Lukt dat niet veilig, dan schrijft het een nieuw concept. Bij het concept zie je wat er veranderd is en kun je de vorige versie bekijken.
- **Inplannen:** bij goedkeuren kies je "Nu verzenden", een vast tijdstip, of "Op een tijdstip…". Een ingepland bericht kun je annuleren; het gaat dan terug naar de wachtrij. Verzenden gebeurt op de achtergrond, ook als het venster dicht is (bureaubladprogramma) of de server draait.

## Facturen en betalingen

- **Importeren:** upload een UBL-factuur (Peppol BIS 3.0) op het Peppol-tabblad. Ongeldige facturen worden geweigerd, met alle problemen erbij. Koppel een factuur aan een klant; zonder keuze gebeurt dat op naam.
- **Betaling melden:** een klant die schrijft dat hij of zij heeft betaald, zet Werker automatisch op betaald, als dat eenduidig is. Dat gebeurt als het factuurnummer in het bericht staat, of als de klant maar één factuur openstaan heeft. Staat er een bedrag in het bericht dat niet klopt, dan doet Werker niets en laat het je weten.
- **Versturen:** zonder Peppol-koppeling (die is er nog niet) komt een betaalde factuur in de map `peppol-map` in je datamap, met een tekstbestand dat de reden noemt. Daar kun je ze vandaan halen, of later opnieuw laten versturen zodra er een access point is.
- **Bankexport** is optioneel; dat kan ook, maar het is niet nodig.

## Kanalen

| Kanaal | Hoe | Waar |
|---|---|---|
| Gmail | Gmail API (OAuth, `werker gmail-login`) | Overal |
| Eigen domein / zakelijke mail | IMAP, meerdere mailboxen via `WERKER_MAILBOXES` | Overal |
| WhatsApp (zakelijk nummer) | Officiële Cloud API, via webhook | Server met HTTPS |
| iMessage | Lokale berichtendatabase van de Mac | Alleen macOS-programma |

Elke mailbox krijgt een eigen kanaal (`mail:<naam>`), zodat een antwoord altijd via dezelfde mailbox gaat.
Outlook.com wordt nog niet ondersteund: Microsoft blokkeert het gewone wachtwoord voor IMAP.

**Let op:** WhatsApp heeft een publieke webhook nodig, dus die werkt alleen op een server. Het bureaubladprogramma kan geen WhatsApp-berichten ontvangen.

## Bureaubladprogramma

Werker draait ook als eigen venster op je computer, zonder browser en zonder server die je zelf moet starten.

```bash
pip install -e ".[desktop]"
mkdir -p ~/.werker && echo "ANTHROPIC_API_KEY=..." > ~/.werker/werker.env   # plus overige instellingen
werker-desktop
```

Gegevens staan in `~/.werker/` (database, `werker.env` en `werker.log`). Het venster luistert alleen op `127.0.0.1`. Nieuwe berichten worden elke 5 minuten opgehaald (`WERKER_SYNC_MINUTES`).

Installeerbare programma's (Windows en macOS) bouwt GitHub Actions: `.github/workflows/desktop-build.yml`. Start die handmatig of met een tag `v*`; de zips staan dan als artifact klaar. Lokaal bouwen kan met `python packaging/build.py`.

De programma's zijn niet ondertekend. Windows toont daarom "Windows heeft uw pc beschermd": kies *Meer informatie* en *Toch uitvoeren*. macOS vraagt om rechtsklik en *Open*. Voor ondertekening is een Apple Developer-account en een Windows-certificaat nodig.

Op Linux heeft pywebview GTK of Qt nodig.

## Online gebruiken

De inbox is ook een installeerbare webapp. Na het inloggen met `WERKER_UI_PASSWORD` kun je hem via de browser op telefoon of computer aan je startscherm zetten ("Installeren" of "Zet op beginscherm"). Zie `docs/deploy.md` voor het online zetten achter HTTPS.

## Let op

- **WhatsApp**: alleen de officiële WhatsApp Business Cloud API. Vrije tekst kan alleen binnen 24 uur na het laatste bericht van de klant; daarna is een goedgekeurd template nodig, die nog niet is gebouwd.
- **Gmail**: het OAuth-token maak je eenmalig met `google-auth-oauthlib`. Voor productiegebruik vraagt Google verificatie van de app aan.
- **Privacy**: berichttekst gaat naar de Anthropic API. Neem dat op in je privacyverklaring.
- **Hosting**: de service moet op een vaste host draaien om webhooks te ontvangen. Prijzen worden per run ververst; plan `werker sync` en de prijsverversing als cron-taak (bijvoorbeeld elk uur).
- Nog niet gebouwd: de dagelijkse digest en WhatsApp-templates voor buiten het 24-uursvenster.
