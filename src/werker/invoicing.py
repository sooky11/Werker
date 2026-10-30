"""Facturen versturen via Peppol: importeren, betalingen herkennen, verzenden of in de map zetten."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from werker.invoice_formats import document_text, parse_invoice
from werker.peppol import AccessPoint, InvoiceError, parse_bank_csv, parse_ubl, remove_from_map, safe_filename, write_to_map
from werker.prices import find_amounts
from werker.store import Store
from werker.store_billing import (
    REMINDER_EXPIRED,
    REMINDER_NO_ADDRESS,
    REMINDER_PENDING,
    REMINDER_REJECTED,
    REMINDER_SENT,
    REMINDER_WAITING,
)

log = logging.getLogger(__name__)

WAITING_PAYMENT = "wacht op betaling"
NEEDS_REVIEW = "wacht op controle"
TO_SEND = "te verzenden"
SENT = "verzonden"
IN_MAP = "in map"
REMINDER_INTERVAL = timedelta(days=7)
# Bestandsvorm per formaat in de map; alleen UBL kan via Peppol.
MAP_SUFFIX = {"x12-810": ".edi"}


class NotConfigured(RuntimeError):
    pass


class NotSendable(RuntimeError):
    """De factuur kan niet via Peppol; de reden staat in het tekstbestand naast de factuur in de map."""


class ReminderSendError(RuntimeError):
    """Het verzenden van een betalingsherinnering is mislukt; de reden staat ook in het auditlog."""


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_overdue(invoice, today: date) -> bool:
    """Verlopen: de vervaldatum ligt vóór vandaag."""
    due = _parse_date(invoice["due_date"])
    return due is not None and due < today


def reminder_text(invoice) -> tuple[str, str]:
    """Onderwerp en tekst van een betalingsherinnering, in het Nederlands."""
    amount = f"€ {Decimal(invoice['total_incl']):.2f}".replace(".", ",")
    due = _parse_date(invoice["due_date"])
    due_text = due.strftime("%d-%m-%Y") if due else "onbekend"
    subject = f"Betalingsherinnering factuur {invoice['number']}"
    body = (
        f"Beste {invoice['buyer_name']},\n\n"
        f"Volgens onze administratie is factuur {invoice['number']} van {amount} nog niet betaald. "
        f"De betaaltermijn liep af op {due_text}.\n\n"
        "Wilt u het bedrag zo snel mogelijk overmaken? Heeft u al betaald, dan kunt u deze e-mail negeren.\n\n"
        "Met vriendelijke groet"
    )
    return subject, body


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


class Invoicing:
    def __init__(self, store: Store, folder: Path, access_point: AccessPoint | None = None):
        self._store = store
        self.folder = folder
        self.access_point = access_point
        # Het mailkanaal waarmee herinneringen naar klanten gaan (een mail:-connector); None zolang er geen mailbox is.
        self.mailer: Any = None
        # Alle mailkanalen op naam; een herinnering gaat bij voorkeur via de mailbox die de klant het laatst gebruikte.
        self.mail_connectors: dict[str, Any] = {}
        self._lock = threading.RLock()

    # ---------- importeren ----------

    def import_xml(self, xml: str, client_id: int | None = None) -> int:
        """Importeer een factuur als tekst (XML of X12). Zie import_document."""
        return self.import_document(xml.encode("utf-8"), client_id=client_id)

    def import_document(self, data: bytes, filename: str = "", client_id: int | None = None) -> int:
        """Importeer een factuur in een van de ondersteunde formaten. Geeft het nieuwe factuur-id.

        Ongeldige facturen worden geweigerd met alle problemen en komen niet in de administratie. Facturen waarvan
        het formaat niet volledig is (needs_review) komen op 'wacht op controle' en blijven daar tot iemand ze
        heeft nagekeken. Zonder client_id koppelen we de factuur aan een klant met dezelfde naam als de afnemer.
        """
        parsed = parse_invoice(data, filename)
        if self._store.find_invoice_by_number(parsed.number) is not None:
            raise ValueError(f"factuur {parsed.number} staat er al in")
        if client_id is None:
            client_id = self._client_by_name(parsed.buyer_name)
        fields = {
            "issue_date": parsed.issue_date,
            "due_date": parsed.due_date,
            "currency": parsed.currency,
            "total_excl": parsed.total_excl,
            "total_vat": parsed.total_vat,
            "total_incl": parsed.total_incl,
            "buyer_name": parsed.buyer_name,
            "buyer_endpoint": parsed.buyer_endpoint,
            "client_id": client_id,
            "format": parsed.format,
            "needs_review": parsed.needs_review,
        }
        status = NEEDS_REVIEW if parsed.needs_review else WAITING_PAYMENT
        return self._store.add_invoice(parsed.number, fields, document_text(data), status)

    def mark_reviewed(self, invoice_id: int) -> None:
        """Iemand heeft de factuur nagekeken: ze gaat naar 'wacht op betaling' en wordt daarna gewoon verwerkt."""
        with self._lock:
            row = self._get(invoice_id)
            if row["status"] != NEEDS_REVIEW:
                raise ValueError("deze factuur staat niet op 'wacht op controle'")
            self._store.update_invoice(invoice_id, status=WAITING_PAYMENT, needs_review=0)
            self._store.audit(None, "factuur nagekeken", row["number"])

    def _client_by_name(self, name: str) -> int | None:
        matches = [row["id"] for row in self._store.list_clients() if row["name"].strip().lower() == name.strip().lower()]
        return matches[0] if len(matches) == 1 else None

    # ---------- betalingen uit berichten ----------

    def match_message(self, body: str, client_id: int | None) -> list[str]:
        """Herken een betaalmelding in een klantbericht en zet de factuur op betaald.

        Automatisch alleen als er precies één openstaande factuur past: het nummer staat in het bericht,
        of de klant heeft er maar één openstaan. Staat er een bedrag in het bericht, dan moet dat kloppen.
        Anders blijft het als melding staan en beslis jij.
        """
        with self._lock:
            return self._match_message(body, client_id)

    def _match_message(self, body: str, client_id: int | None) -> list[str]:
        open_invoices = self._store.list_invoices(status=WAITING_PAYMENT)
        text = body.lower()
        mentioned = [i for i in open_invoices if i["number"].lower() in text]
        if mentioned:
            candidates = mentioned
        elif client_id is not None:
            candidates = [i for i in open_invoices if i["client_id"] == client_id]
        else:
            candidates = []
        if len(candidates) != 1:
            self._store.audit(None, "betaling gemeld", "geen eenduidige factuur gevonden")
            return []
        invoice = candidates[0]
        amounts = find_amounts(body)
        if amounts and Decimal(invoice["total_incl"]) not in amounts:
            self._store.audit(None, "betaling gemeld", f"bedrag komt niet overeen met factuur {invoice['number']}")
            return []
        self._store.update_invoice(invoice["id"], status=TO_SEND, paid_at=datetime.now(timezone.utc).isoformat())
        self._store.audit(None, "factuur betaald (uit bericht)", invoice["number"])
        self._send(invoice["id"])
        return [invoice["number"]]

    # ---------- betalingen ----------

    def match_bank_file(self, csv_text: str) -> list[str]:
        with self._lock:
            return self._match_bank_file(csv_text)

    def _match_bank_file(self, csv_text: str) -> list[str]:
        """Koppel betalingen uit een bankbestand aan openstaande facturen.

        Een betaling telt als match als het factuurnummer in de omschrijving staat en het bedrag klopt.
        Geeft de nummers van de facturen die daardoor op betaald zijn gezet.
        """
        matched = []
        open_invoices = self._store.list_invoices(status=WAITING_PAYMENT)
        for payment in parse_bank_csv(csv_text):
            for invoice in open_invoices:
                if invoice["number"] not in payment.description:
                    continue
                if abs(Decimal(invoice["total_incl"]) - payment.amount) >= Decimal("0.01"):
                    continue
                self._store.update_invoice(
                    invoice["id"], status=TO_SEND, paid_at=datetime.now(timezone.utc).isoformat()
                )
                matched.append(invoice["number"])
                self._store.audit(None, "factuur betaald", invoice["number"])
                self.send(invoice["id"])
                open_invoices = [i for i in open_invoices if i["id"] != invoice["id"]]
                break
        return matched

    def mark_paid(self, invoice_id: int) -> str:
        with self._lock:
            return self._mark_paid(invoice_id)

    def _mark_paid(self, invoice_id: int) -> str:
        row = self._get(invoice_id)
        if row["status"] != WAITING_PAYMENT:
            raise ValueError("deze factuur staat niet op 'wacht op betaling'")
        self._store.update_invoice(invoice_id, status=TO_SEND, paid_at=datetime.now(timezone.utc).isoformat())
        self._store.audit(None, "factuur handmatig betaald", row["number"])
        return self.send(invoice_id)

    # ---------- verzenden ----------

    def send(self, invoice_id: int) -> str:
        """Verstuur via het access point; lukt dat niet, dan komt de factuur in de map. Geeft de nieuwe status."""
        with self._lock:
            return self._send(invoice_id)

    def _send(self, invoice_id: int) -> str:
        row = self._get(invoice_id)
        xml = row["xml"].encode("utf-8")
        suffix = MAP_SUFFIX.get(row["format"], ".xml")
        try:
            if not row["buyer_endpoint"]:
                raise NotSendable("geen Peppol-adres van de afnemer bekend; factuur kan niet via Peppol worden verstuurd")
            if row["format"] != "ubl":
                raise NotSendable(f"formaat {row['format']} kan niet via Peppol worden verstuurd; alleen UBL")
            if self.access_point is None:
                raise NotConfigured("geen Peppol access point gekoppeld")
            self.access_point.send(xml, row["buyer_endpoint"])
        except Exception as exc:
            reason = f"{datetime.now(timezone.utc).isoformat()}: {exc}"
            write_to_map(self.folder, row["number"], xml, reason, suffix)
            self._store.update_invoice(invoice_id, status=IN_MAP, last_error=str(exc))
            self._store.audit(None, "factuur in map", f"{row['number']}: {exc}")
            return IN_MAP
        remove_from_map(self.folder, row["number"])
        self._store.update_invoice(
            invoice_id, status=SENT, sent_at=datetime.now(timezone.utc).isoformat(), last_error=""
        )
        self._store.audit(None, "factuur verzonden via Peppol", row["number"])
        return SENT

    def dispatch(self) -> int:
        """Verstuur alle facturen die klaarstaan. Wordt periodiek aangeroepen."""
        with self._lock:
            return self._dispatch()

    def _dispatch(self) -> int:
        sent = 0
        for row in self._store.list_invoices(status=TO_SEND):
            if self.send(row["id"]) == SENT:
                sent += 1
        return sent

    # ---------- betalingsherinneringen ----------

    def collect_reminders(self, now: datetime | None = None) -> list[int]:
        """Maak herinneringen voor verlopen, onbetaalde facturen. Veilig om vaak aan te roepen.

        Per factuur komt er hooguit één herinnering per zeven dagen. Heeft de klant geen e-mailadres,
        dan komt de herinnering op 'geen adres bekend'. Geeft de nieuwe herinnering-ids.
        """
        with self._lock:
            return self._collect_reminders(_utc(now or datetime.now(timezone.utc)))

    def _collect_reminders(self, now: datetime) -> list[int]:
        self._expire_reminders(now)
        created = []
        for invoice in self._store.list_invoices(status=WAITING_PAYMENT):
            if not is_overdue(invoice, now.date()):
                continue
            last = self._store.last_reminder_at(invoice["id"])
            if last is not None and now - datetime.fromisoformat(last) < REMINDER_INTERVAL:
                continue
            recipient = self._client_email(invoice["client_id"])
            subject, body = reminder_text(invoice)
            status = REMINDER_WAITING if recipient else REMINDER_NO_ADDRESS
            created.append(
                self._store.add_reminder(
                    invoice["id"], status, recipient or "", subject, body, now.isoformat()
                )
            )
            self._store.audit(None, "herinnering aangemaakt", f"{invoice['number']}: {status}")
        return created

    def _expire_reminders(self, now: datetime) -> None:
        """Een herinnering waarvan de factuur intussen betaald is, is niet meer nodig."""
        for row in self._store.all_pending_reminders():
            if row["invoice_status"] != WAITING_PAYMENT:
                self._store.update_reminder(row["id"], status=REMINDER_EXPIRED, decided_at=now.isoformat())
                self._store.audit(None, "herinnering vervallen", f"factuur {row['invoice_id']} is niet meer openstaand")

    def _client_email(self, client_id: int | None) -> str | None:
        if client_id is None:
            return None
        emails = sorted(i["value"] for i in self._store.client_identifiers(client_id) if i["kind"] == "email")
        return emails[0] if emails else None

    def _mailer_for(self, client_id: int | None) -> Any:
        """De mailbox waarop de klant het laatst schreef, als die nog gekoppeld is; anders de standaardmailbox."""
        if client_id is not None:
            for channel in self._store.client_mail_channels(client_id):
                connector = self.mail_connectors.get(channel)
                if connector is not None:
                    return connector
        return self.mailer

    def approve_reminder(self, reminder_id: int, body: str | None = None) -> None:
        """Keur een herinnering goed en verstuur haar naar het e-mailadres van de klant.

        Met body wordt de tekst eerst aangepast. Bij een verzendfout blijft de herinnering staan.
        """
        with self._lock:
            self._approve_reminder(reminder_id, body)

    def _approve_reminder(self, reminder_id: int, body: str | None) -> None:
        row = self._store.get_reminder(reminder_id)
        if row is None:
            raise LookupError(f"herinnering {reminder_id} bestaat niet")
        if row["status"] not in REMINDER_PENDING:
            raise ValueError(f"herinnering staat op '{row['status']}'")
        invoice = self._get(row["invoice_id"])
        now = datetime.now(timezone.utc)
        if invoice["status"] != WAITING_PAYMENT:
            self._store.update_reminder(reminder_id, status=REMINDER_EXPIRED, decided_at=now.isoformat())
            raise ValueError("de factuur staat niet meer op 'wacht op betaling'; de herinnering vervalt")
        recipient = self._client_email(invoice["client_id"])
        if recipient is None:
            self._store.update_reminder(reminder_id, status=REMINDER_NO_ADDRESS)
            raise ValueError("geen e-mailadres bekend voor deze klant; voeg er bij de klant een toe")
        text = body if body is not None else row["body"]
        mailer = self._mailer_for(invoice["client_id"])
        if mailer is None:
            reason = "geen mailbox gekoppeld; koppel er een bij Instellingen"
            self._store.update_reminder(reminder_id, status=REMINDER_WAITING, recipient=recipient, body=text, last_error=reason)
            raise ReminderSendError(reason)
        self._store.update_reminder(reminder_id, status=REMINDER_WAITING, recipient=recipient, body=text)
        try:
            # Een herinnering is een nieuwe mail, geen antwoord: dus geen "Re:" en geen verwijzing naar een eerder bericht.
            mailer.send_new(recipient, row["subject"], text)
        except Exception as exc:
            self._store.update_reminder(reminder_id, last_error=str(exc))
            self._store.audit(None, "herinnering mislukt", f"{invoice['number']}: {exc}")
            raise ReminderSendError(f"verzenden mislukt: {exc}") from exc
        self._store.update_reminder(
            reminder_id, status=REMINDER_SENT, sent_at=now.isoformat(), decided_at=now.isoformat(), last_error=""
        )
        via = getattr(mailer, "name", "")
        self._store.audit(
            None, "herinnering verzonden", f"{invoice['number']} naar {recipient}" + (f" via {via}" if via else "")
        )

    def reject_reminder(self, reminder_id: int) -> None:
        with self._lock:
            row = self._store.get_reminder(reminder_id)
            if row is None:
                raise LookupError(f"herinnering {reminder_id} bestaat niet")
            if row["status"] not in REMINDER_PENDING:
                raise ValueError(f"herinnering staat op '{row['status']}'; ze kan niet meer worden afgewezen")
            self._store.update_reminder(
                reminder_id, status=REMINDER_REJECTED, decided_at=datetime.now(timezone.utc).isoformat()
            )
            self._store.audit(None, "herinnering afgewezen", self._get(row["invoice_id"])["number"])

    # ---------- de map ----------

    def map_items(self) -> list[dict[str, Any]]:
        items = []
        for row in self._store.list_invoices(status=IN_MAP):
            items.append(
                {
                    "invoice_id": row["id"],
                    "number": row["number"],
                    "reason": row["last_error"],
                    "path": str(self.folder / f"{safe_filename(row['number'])}{MAP_SUFFIX.get(row['format'], '.xml')}"),
                }
            )
        return items

    def _get(self, invoice_id: int):
        row = self._store.get_invoice(invoice_id)
        if row is None:
            raise LookupError(f"factuur {invoice_id} bestaat niet")
        return row


def validate_only(xml: str) -> dict[str, Any]:
    """Controleer een factuur zonder op te slaan; geeft de gevonden problemen."""
    try:
        parsed = parse_ubl(xml.encode("utf-8"))
    except InvoiceError as exc:
        return {"valid": False, "problems": exc.problems}
    return {"valid": True, "problems": [], "number": parsed.number, "buyer": parsed.buyer_name}
