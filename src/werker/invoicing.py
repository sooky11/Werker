"""Facturen versturen via Peppol: importeren, betalingen herkennen, verzenden of in de map zetten."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from werker.peppol import AccessPoint, InvoiceError, parse_bank_csv, parse_ubl, remove_from_map, write_to_map
from werker.store import Store

log = logging.getLogger(__name__)

WAITING_PAYMENT = "wacht op betaling"
TO_SEND = "te verzenden"
SENT = "verzonden"
IN_MAP = "in map"


class NotConfigured(RuntimeError):
    pass


class Invoicing:
    def __init__(self, store: Store, folder: Path, access_point: AccessPoint | None = None):
        self._store = store
        self.folder = folder
        self.access_point = access_point
        self._lock = threading.RLock()

    # ---------- importeren ----------

    def import_xml(self, xml: str) -> int:
        """Importeer een UBL-factuur. Geeft het nieuwe factuur-id. Ongeldige facturen worden geweigerd met alle problemen."""
        raw = xml.encode("utf-8")
        parsed = parse_ubl(raw)
        if self._store.find_invoice_by_number(parsed.number) is not None:
            raise ValueError(f"factuur {parsed.number} staat er al in")
        fields = {
            "issue_date": parsed.issue_date,
            "due_date": parsed.due_date,
            "currency": parsed.currency,
            "total_excl": parsed.total_excl,
            "total_vat": parsed.total_vat,
            "total_incl": parsed.total_incl,
            "buyer_name": parsed.buyer_name,
            "buyer_endpoint": parsed.buyer_endpoint,
        }
        return self._store.add_invoice(parsed.number, fields, xml, WAITING_PAYMENT)

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
        try:
            if self.access_point is None:
                raise NotConfigured("geen Peppol access point gekoppeld")
            self.access_point.send(xml, row["buyer_endpoint"])
        except Exception as exc:
            reason = f"{datetime.now(timezone.utc).isoformat()}: {exc}"
            write_to_map(self.folder, row["number"], xml, reason)
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

    # ---------- de map ----------

    def map_items(self) -> list[dict[str, Any]]:
        items = []
        for row in self._store.list_invoices(status=IN_MAP):
            items.append(
                {
                    "invoice_id": row["id"],
                    "number": row["number"],
                    "reason": row["last_error"],
                    "path": str(self.folder / f"{row['number']}.xml"),
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
