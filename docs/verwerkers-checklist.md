# Checklist verwerkersovereenkomsten en AVG-basis — CONCEPT

> **Geen juridisch advies.** Dit is een werklijst om met een jurist of je boekhouder door te lopen.
> Voorwaarden van dienstverleners veranderen; controleer bij elke partij de **actuele** tekst.
> Niets hieronder is door mij bij de partijen zelf gecontroleerd.

## 1. Welke verwerkers gebruik je echt?

Vink alleen aan wat in je Werker-instellingen aan staat.

| ✓ | Partij | Wat krijgt die partij | Waar vind je de verwerkersafspraken (zoek op) |
|---|---|---|---|
| ☐ | Anthropic (AI) | Tekst van elk binnenkomend bericht, de prijslijst, conceptantwoorden | "Anthropic Data Processing Addendum" / commerciële voorwaarden |
| ☐ | Google (Gmail, Agenda, Sheets) | Al je mail, agenda-items, prijslijst | "Google Workspace Data Processing Amendment" (alleen bij Workspace, niet bij een gratis Gmail-account) |
| ☐ | Microsoft (Outlook / 365) | Al je mail | "Microsoft Products and Services Data Protection Addendum" |
| ☐ | Meta (WhatsApp Business Cloud API) | WhatsApp-berichten en telefoonnummers | "WhatsApp Business Data Processing Terms" |
| ☐ | Mailprovider van je eigen domein | Al je mail | Verwerkersovereenkomst van je provider |
| ☐ | Peppol access point (e-Connect, Banqup, Storecove …) | Facturen met klantgegevens | Verwerkersovereenkomst van de provider |
| ☐ | Hostingpartij (alleen online versie) | De volledige database en back-ups | Verwerkersovereenkomst van de host |
| ☐ | Plek waar je back-ups bewaart (externe schijf, cloud) | Kopie van de database | Verwerkersovereenkomst als het een clouddienst is |

## 2. Per verwerker controleren

Voor elke aangevinkte partij:

- ☐ Er is een verwerkersovereenkomst (DPA) die op jouw account van toepassing is. Bij veel diensten zit die in de
  zakelijke voorwaarden en geldt die automatisch; bij gratis/consumentenaccounts vaak **niet**.
- ☐ Het land waar de gegevens worden verwerkt is bekend.
- ☐ Bij doorgifte buiten de EER: er is een geldige grond (adequaatheidsbesluit, bijvoorbeeld het EU-VS Data Privacy
  Framework als de partij daarbij is aangesloten, of standaardcontractbepalingen).
- ☐ Bekend is of de partij je gegevens gebruikt om eigen modellen of diensten te trainen, en dat dit uit staat of
  niet gebeurt.
- ☐ Bekend is hoe lang de partij de gegevens zelf bewaart.
- ☐ Bekend is welke subverwerkers de partij inschakelt.
- ☐ Datum van controle en de versie van de voorwaarden genoteerd: ____________

## 3. Je eigen administratie

- ☐ **Verwerkingsregister** bijgehouden (wat, waarom, welke grondslag, welke verwerkers, bewaartermijn).
  Kleine ondernemingen zijn hier niet altijd toe verplicht, maar bij regelmatige verwerking van klantgegevens
  meestal wel — laten controleren.
- ☐ **Privacyverklaring** ingevuld en gepubliceerd (zie `privacyverklaring-concept.md`).
- ☐ **Bewaartermijn berichten** gekozen en ingesteld (Instellingen → Bewaren en verwijderen).
- ☐ **Bewaartermijn facturen** nagevraagd bij je boekhouder (verschilt per land).
- ☐ **Bewaartermijn back-ups** gekozen (Instellingen → Back-ups; aantal × interval = hoe lang een verwijderde klant
  nog in een back-up kan staan).
- ☐ **Automatisch verzenden**: bewust gekozen aan of uit, en zo beschreven in de privacyverklaring.
- ☐ **Gerechtvaardigd belang** voor AI-sortering kort onderbouwd (belangenafweging op papier).
- ☐ **DPIA** (gegevensbeschermingseffectbeoordeling): laten beoordelen of die nodig is. Grootschalige of
  gevoelige verwerking maakt dat waarschijnlijker; een kleine zaak met alleen prijsvragen minder.
- ☐ **Procedure datalek**: wie doet wat, en melden bij de toezichthouder binnen 72 uur als het risico niet laag is.

## 4. Technische maatregelen in Werker (afvinken bij installatie)

- ☐ Online versie alleen achter HTTPS (zie `deploy.md`).
- ☐ Een eigen account per medewerker; alleen beheerders kunnen klanten verwijderen of back-ups downloaden.
- ☐ `secrets.json`, `.env` en back-ups met geheimen alleen leesbaar voor de beheerder.
- ☐ Automatische back-ups aan, en de map wordt naar een **andere** plek gekopieerd.
- ☐ Terugzetten van een back-up één keer getest.
- ☐ Beveiligingschecklist in `deploy.md` doorlopen.
- ☐ Laptop/computer met het bureaubladprogramma heeft schijfversleuteling aan (BitLocker / FileVault).

## 5. Verzoeken van klanten

- Inzage of kopie: klantpagina → *Exporteren*.
- Verwijdering: klantpagina → *Verwijderen* (beheerder). Facturen blijven, met de naam vervangen.
- Antwoord binnen één maand; noteer datum van verzoek en afhandeling. Werker houdt de acties bij in het
  privacylogboek (Instellingen).
