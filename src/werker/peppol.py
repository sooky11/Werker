"""Peppol-facturen: UBL-bestanden inlezen en controleren, betalingen uit een bankbestand halen.

Werker verstuurt geen facturen rechtstreeks. Dat gaat via een Peppol access point (een aanbieder).
Die koppeling zit achter het protocol AccessPoint; zolang er geen aanbieder is gekoppeld, komen
facturen in de map "peppol-map" in de datamap, met een tekstbestand waarom ze daar staan.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

NS = {
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
}
PEPPOL_CUSTOMIZATION = "urn:cen.eu:en16931:2017#conformant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
CENT = Decimal("0.01")


class InvoiceError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class AccessPoint(Protocol):
    """Verstuurt een UBL-factuur naar een ontvanger via een Peppol access point. Geeft een transmissie-id."""

    def send(self, xml: bytes, recipient: str) -> str: ...


@dataclass(frozen=True)
class ParsedInvoice:
    number: str
    issue_date: str
    due_date: str | None
    currency: str
    total_excl: Decimal
    total_vat: Decimal
    total_incl: Decimal
    buyer_name: str
    buyer_endpoint: str
    problems: list[str] = field(default_factory=list)


def _text(root: ET.Element, path: str) -> str | None:
    node = root.find(path, NS)
    return node.text.strip() if node is not None and node.text else None


def _amount(root: ET.Element, path: str, problems: list[str], label: str) -> Decimal | None:
    raw = _text(root, path)
    if raw is None:
        problems.append(f"{label} ontbreekt")
        return None
    try:
        return Decimal(raw).quantize(CENT)
    except InvalidOperation:
        problems.append(f"{label} is geen geldig bedrag: {raw!r}")
        return None


def parse_ubl(xml_bytes: bytes) -> ParsedInvoice:
    """Lees een UBL-factuur en controleer de verplichte velden. Fouten geven een InvoiceError met alle problemen."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise InvoiceError([f"geen geldige XML: {exc}"])
    if root.tag != f"{{{NS['inv']}}}Invoice":
        raise InvoiceError(["dit is geen UBL-factuur (Invoice)"])

    problems: list[str] = []
    customization = _text(root, "cbc:CustomizationID")
    if customization != PEPPOL_CUSTOMIZATION:
        problems.append("geen Peppol BIS Billing 3.0-profiel (CustomizationID)")

    number = _text(root, "cbc:ID") or ""
    if not number:
        problems.append("factuurnummer ontbreekt")
    issue_date = _text(root, "cbc:IssueDate") or ""
    if not issue_date:
        problems.append("factuurdatum ontbreekt")
    currency = _text(root, "cbc:DocumentCurrencyCode") or ""
    if not currency:
        problems.append("valuta ontbreekt")

    buyer_name = _text(root, "cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName") or ""
    if not buyer_name:
        buyer_name = _text(root, "cac:AccountingCustomerParty/cac:Party/cac:PartyName/cbc:Name") or ""
    if not buyer_name:
        problems.append("naam van de klant ontbreekt")
    endpoint_node = root.find("cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", NS)
    if endpoint_node is None or not (endpoint_node.text or "").strip():
        problems.append("elektronisch adres van de klant (EndpointID) ontbreekt")
        buyer_endpoint = ""
    else:
        scheme = endpoint_node.get("schemeID", "")
        buyer_endpoint = f"{scheme}:{endpoint_node.text.strip()}" if scheme else endpoint_node.text.strip()

    if root.find("cac:InvoiceLine", NS) is None:
        problems.append("de factuur heeft geen regels")

    excl = _amount(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount", problems, "totaal excl. btw")
    incl = _amount(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", problems, "totaal incl. btw")
    vat = _amount(root, "cac:TaxTotal/cbc:TaxAmount", problems, "btw-bedrag")
    if excl is not None and incl is not None and vat is not None and (excl + vat).quantize(CENT) != incl:
        problems.append("totalen kloppen niet: excl. btw + btw is niet gelijk aan incl. btw")

    if problems:
        raise InvoiceError(problems)
    return ParsedInvoice(
        number=number,
        issue_date=issue_date,
        due_date=_text(root, "cbc:DueDate"),
        currency=currency,
        total_excl=excl,
        total_vat=vat,
        total_incl=incl,
        buyer_name=buyer_name,
        buyer_endpoint=buyer_endpoint,
    )


@dataclass(frozen=True)
class BankPayment:
    amount: Decimal
    description: str
    date: str


def parse_bank_csv(text: str) -> list[BankPayment]:
    """Lees een bankexport (CSV). Herkent kolommen op naam: bedrag/amount, omschrijving/description, datum/date."""
    reader = csv.DictReader(io.StringIO(text), dialect=_dialect(text))
    fields = {name.lower().strip(): name for name in (reader.fieldnames or [])}

    def find(*options: str) -> str | None:
        for option in options:
            for lower, original in fields.items():
                if option in lower:
                    return original
        return None

    amount_col = find("bedrag", "amount")
    description_col = find("omschrijving", "description", "mededeling")
    date_col = find("datum", "date")
    if amount_col is None or description_col is None:
        raise ValueError("bankbestand: kolommen voor bedrag en omschrijving niet gevonden")

    payments = []
    for row in reader:
        raw = (row.get(amount_col) or "").strip().replace("€", "").replace(" ", "")
        if not raw:
            continue
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        try:
            amount = Decimal(raw).quantize(CENT)
        except InvalidOperation:
            continue
        if amount <= 0:
            continue  # alleen binnenkomende betalingen
        payments.append(
            BankPayment(
                amount=amount,
                description=(row.get(description_col) or "").strip(),
                date=(row.get(date_col) or "").strip() if date_col else "",
            )
        )
    return payments


def _dialect(text: str) -> type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(text[:2048], delimiters=";,\t")
    except csv.Error:
        return csv.excel


def safe_filename(number: str) -> str:
    return re.sub(r"[^\w.-]", "_", number).strip("._") or "factuur"


def write_to_map(folder: Path, number: str, xml: bytes, reason: str) -> Path:
    """Zet de factuur in de map met een tekstbestand waarom ze daar staat. Geeft het pad van het XML-bestand."""
    folder.mkdir(parents=True, exist_ok=True)
    base = safe_filename(number)
    xml_path = folder / f"{base}.xml"
    xml_path.write_bytes(xml)
    (folder / f"{base}.txt").write_text(reason + "\n", encoding="utf-8")
    return xml_path


def remove_from_map(folder: Path, number: str) -> None:
    base = safe_filename(number)
    for suffix in (".xml", ".txt"):
        (folder / f"{base}{suffix}").unlink(missing_ok=True)
