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

## Instellingen (omgevingsvariabelen)

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

## Gebruik

```bash
werker prices                       # toon de prijslijst zoals het programma die ziet
werker process-eml voorbeeld.eml    # test op een bestand; verzendt nooit
werker sync                         # haal nieuwe mail op en verwerk die
werker drafts                       # concepten die op goedkeuring wachten
werker approve 3                    # keur concept 3 goed en verstuur het
werker approve 3 --text aangepast.txt
werker reject 3
werker serve --port 8000            # WhatsApp-webhook (achter HTTPS, URL instellen bij Meta)
```

## Let op

- **WhatsApp**: alleen de officiële WhatsApp Business Cloud API. Vrije tekst kan alleen binnen 24 uur na het laatste bericht van de klant; daarna is een goedgekeurd template nodig, die nog niet is gebouwd.
- **Gmail**: het OAuth-token maak je eenmalig met `google-auth-oauthlib`. Voor productiegebruik vraagt Google verificatie van de app aan.
- **Privacy**: berichttekst gaat naar de Anthropic API. Neem dat op in je privacyverklaring.
- **Hosting**: de service moet op een vaste host draaien om webhooks te ontvangen. Prijzen worden per run ververst; plan `werker sync` en de prijsverversing als cron-taak (bijvoorbeeld elk uur).
- Nog niet gebouwd: de inbox-webpagina en de dagelijkse digest (zie het plan).
