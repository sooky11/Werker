# Werker uitrollen op een server

Deze handleiding zet Werker op een Linux-server met Docker. De webpagina en de WhatsApp-webhook draaien achter een HTTPS-reverse-proxy. Werker luistert zelf alleen op `127.0.0.1:8000`.

## Vereisten

- Een Linux-server met Docker en Docker Compose (`docker compose version` moet werken).
- Een domeinnaam, bijvoorbeeld `werker.example.nl`, met een DNS-record dat naar de server wijst.
- Een HTTPS-reverse-proxy. Dit voorbeeld gebruikt Caddy, die certificaten zelf regelt.
- Een Anthropic API-sleutel.
- Voor WhatsApp: een Meta Business-app met WhatsApp Cloud API.
- Voor mail: IMAP/SMTP-gegevens of een Gmail OAuth-token.

### Caddy

Installeer Caddy volgens de documentatie op caddyserver.com en zet de volgende regel in `/etc/caddy/Caddyfile`:

```
werker.example.nl {
    reverse_proxy 127.0.0.1:8000
}
```

Laad daarna de configuratie opnieuw met `sudo systemctl reload caddy`. Caddy vraagt het certificaat automatisch aan zodra het domein naar de server wijst.

**Inlogbeveiliging achter de proxy.** Werker blokkeert een IP-adres na vijf foute inlogpogingen. Achter een proxy ziet Werker standaard alleen het adres van de proxy, waardoor één persoon iedereen kan buitensluiten. Zet daarom in `.env` `WERKER_TRUSTED_PROXIES`. Draait Werker in Docker met de poort op `127.0.0.1:8000` (zoals in `docker-compose.yml`), dan komt de verbinding binnen via het interne Docker-adres; zet dan `WERKER_TRUSTED_PROXIES=*`. Dat is alleen veilig omdat de poort niet van buitenaf bereikbaar is; open de poort nooit naar buiten met deze instelling.

## Installatie

1. Haal de code op en ga naar de projectmap:

   ```
   git clone <repository-url> werker
   cd werker
   ```

2. Maak de configuratie aan en vul de waarden in:

   ```
   cp .env.example .env
   nano .env
   ```

   Vul minimaal `ANTHROPIC_API_KEY`, een mailconfiguratie, `WHATSAPP_*` en `WERKER_UI_PASSWORD` in. Laat `WERKER_AUTOSEND=false` staan.

3. Maak de datamap aan. De container draait als gebruiker met UID 1000 en moet de map kunnen schrijven:

   ```
   mkdir -p data
   sudo chown -R 1000:1000 data
   ```

   Zet je prijslijst (optioneel) als `data/prices.yaml` en gebruik `WERKER_PRICES_FILE=/data/prices.yaml`. Voor Gmail zet je `token.json` in `data/` en gebruik je `WERKER_GMAIL_TOKEN=/data/token.json`.

4. Start de services:

   ```
   docker compose up -d --build
   ```

   Dit start twee containers:
   - `werker`: de webpagina en de webhook op poort 8000.
   - `sync`: draait elk uur `werker sync` (prijzen verversen en nieuwe mail ophalen).

5. Controleer of de service draait:

   ```
   docker compose ps
   docker compose logs -f werker
   curl -I http://127.0.0.1:8000/
   ```

   Een `401` op `/` is goed: de inbox vraagt om inloggen.

## WhatsApp koppelen

1. Open in de Meta-ontwikkelaarsconsole de WhatsApp-configuratie van je app.
2. Stel bij de webhook-URL in: `https://werker.example.nl/webhook/whatsapp`.
3. Vul als **Verify token** exact dezelfde waarde in als `WHATSAPP_VERIFY_TOKEN` in `.env`.
4. Klik op verifiëren en abonneer op het veld `messages`.
5. Stuur een testbericht naar het WhatsApp-nummer en controleer of het in de inbox verschijnt.

Na het wijzigen van `.env` herstart je met `docker compose up -d`.

## Bijwerken

```
git pull
docker compose up -d --build
```

De database in `./data` blijft behouden.

## Back-ups

De database is een SQLite-bestand. Kopieer het met de service gestopt of met de SQLite-backupfunctie, zodat je geen half geschreven bestand meeneemt:

```
mkdir -p backups
docker compose stop werker sync
cp data/werker.db backups/werker-$(date +%F).db
docker compose start werker sync
```

Bewaar ook `.env` en `data/token.json` (als je Gmail gebruikt) op een beveiligde plek. Deze bevatten geheimen.

## Beveiligingscontrole

- [ ] `WERKER_UI_PASSWORD` is ingevuld met een lang, uniek wachtwoord. Werker weigert op een netwerk-interface te starten zonder dit wachtwoord.
- [ ] De poort staat alleen op localhost (`127.0.0.1:8000`). Alleen de reverse-proxy is van buitenaf bereikbaar. Controleer dit met `ss -tlnp | grep 8000`.
- [ ] Firewall: alleen poort 80 en 443 staan open, niet 8000.
- [ ] `WHATSAPP_APP_SECRET` is ingevuld, zodat webhook-aanroepen worden geverifieerd.
- [ ] `WERKER_AUTOSEND` staat op `false`. Zet dit pas op `true` als de evaluatie van de concepten goed is en je weet welke intenties automatisch mogen verzenden (`WERKER_AUTOSEND_INTENTS`).
- [ ] `.env`, `data/` en back-ups staan niet in git en zijn alleen leesbaar voor de beheerder (`chmod 600 .env`).
- [ ] De API-sleutels en wachtwoorden zijn niet eerder gedeeld of in een chat geplakt. Roteer ze als dat wel gebeurd is.
