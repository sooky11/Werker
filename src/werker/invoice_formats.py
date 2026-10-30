"""Facturen uit verschillende formaten inlezen en naar dezelfde ParsedInvoice brengen.

Ondersteund: UBL (Peppol), UN/CEFACT CII (ZUGFeRD, Factur-X, XRechnung-CII), FatturaPA (Italië),
KSeF FA(2)/FA(3) (Polen), FacturaE 3.2 (Spanje), X12 810 (VS) en PDF met ingebedde XML.
Herkenning gebeurt op de inhoud (rootelement, namespace, ISA-header), niet op de bestandsnaam.

Er is geen AI-uitlezing: een PDF zonder ingebedde XML wordt geweigerd met een duidelijke melding.
De KSeF- en FacturaE-parsers zijn gebouwd op openbare specificaties die niet tegen de officiële
XSD-bestanden zijn gecontroleerd.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET  # alleen voor het Element-type en ParseError; parsen gaat via peppol.parse_xml
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from werker.peppol import CENT, InvoiceError, ParsedInvoice, parse_ubl, parse_xml

UBL_INVOICE = "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice"
CII_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
KSEF_NS_PREFIX = "http://crd.gov.pl/wzor/"
FACTURAE_NS_PREFIX = "http://www.facturae.gob.es/"

NOT_READABLE = "dit bestand kan niet automatisch gelezen worden; voer de factuur handmatig in"
LABELS = {
    "number": "factuurnummer",
    "issue_date": "factuurdatum",
    "currency": "valuta",
    "buyer_name": "naam van de klant",
}


def _unknown(filename: str) -> InvoiceError:
    where = f" ({filename})" if filename else ""
    return InvoiceError([f"onbekend formaat{where}: {NOT_READABLE}"])


# ---------- hulpfuncties op elementen (op lokale naam, zodat versienummers in namespaces niet uitmaken) ----------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _find(node: ET.Element | None, path: str) -> ET.Element | None:
    current = node
    for part in path.split("/"):
        current = _child(current, part)
        if current is None:
            return None
    return current


def _text(node: ET.Element | None, path: str) -> str | None:
    found = _find(node, path)
    if found is None or found.text is None or not found.text.strip():
        return None
    return found.text.strip()


def _all(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in node if _local(child.tag) == name]


def _decimal(raw: str | None, label: str, problems: list[str]) -> Decimal | None:
    if raw is None:
        problems.append(f"{label} ontbreekt")
        return None
    try:
        return Decimal(raw.replace(",", ".")).quantize(CENT)
    except InvalidOperation:
        problems.append(f"{label} is geen geldig bedrag: {raw!r}")
        return None


def _iso_date(raw: str | None, label: str, problems: list[str]) -> str | None:
    """Datum als YYYY-MM-DD (FacturaE, KSeF, FatturaPA)."""
    if raw is None:
        problems.append(f"{label} ontbreekt")
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        problems.append(f"{label} is geen geldige datum: {raw!r}")
        return None


def _compact_date(raw: str | None, label: str, problems: list[str]) -> str | None:
    """Datum als YYYYMMDD (CII format 102, X12 BIG01). Ontbreekt de datum, dan is dat een probleem."""
    if raw is None:
        problems.append(f"{label} ontbreekt")
        return None
    if re.fullmatch(r"\d{8}", raw):
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:])).isoformat()
        except ValueError:
            pass
    problems.append(f"{label} is geen geldige datum (YYYYMMDD): {raw!r}")
    return None


def _check_totals(excl: Decimal | None, vat: Decimal | None, incl: Decimal | None, problems: list[str]) -> None:
    if excl is not None and vat is not None and incl is not None and (excl + vat).quantize(CENT) != incl:
        problems.append("totalen kloppen niet: excl. btw + btw is niet gelijk aan incl. btw")


def _require(problems: list[str], values: dict[str, str]) -> None:
    """Voeg een probleem toe voor elk verplicht tekstveld dat leeg is."""
    for key, value in values.items():
        if not value:
            problems.append(f"{LABELS[key]} ontbreekt")


def _raise_if(problems: list[str]) -> None:
    if problems:
        raise InvoiceError(problems)


# ---------- UN/CEFACT CII (ZUGFeRD / Factur-X / XRechnung-CII) ----------

def _parse_cii(root: ET.Element) -> ParsedInvoice:
    problems: list[str] = []
    trade = _child(root, "SupplyChainTradeTransaction")
    agreement = _child(trade, "ApplicableHeaderTradeAgreement")
    settlement = _child(trade, "ApplicableHeaderTradeSettlement")
    summary = "SpecifiedTradeSettlementHeaderMonetarySummation"

    number = _text(root, "ExchangedDocument/ID") or ""
    issue_date = _compact_date(_text(root, "ExchangedDocument/IssueDateTime/DateTimeString"), "factuurdatum", problems)
    due_raw = _text(settlement, "SpecifiedTradePaymentTerms/DueDateDateTime/DateTimeString")
    due_date = _compact_date(due_raw, "vervaldatum", problems) if due_raw else None
    currency = _text(settlement, "InvoiceCurrencyCode") or ""
    buyer_name = _text(agreement, "BuyerTradeParty/Name") or ""
    uri = _find(agreement, "BuyerTradeParty/URIUniversalCommunication/URIID")
    buyer_endpoint = ""
    if uri is not None and uri.text and uri.text.strip():
        scheme = uri.get("schemeID", "")
        buyer_endpoint = f"{scheme}:{uri.text.strip()}" if scheme else uri.text.strip()

    _require(problems, {"number": number, "issue_date": issue_date or "", "currency": currency, "buyer_name": buyer_name})
    if not _all(trade, "IncludedSupplyChainTradeLineItem"):
        problems.append("de factuur heeft geen regels")
    excl = _decimal(_text(settlement, f"{summary}/TaxBasisTotalAmount"), "totaal excl. btw", problems)
    vat = _decimal(_text(settlement, f"{summary}/TaxTotalAmount"), "btw-bedrag", problems)
    incl = _decimal(_text(settlement, f"{summary}/GrandTotalAmount"), "totaal incl. btw", problems)
    _check_totals(excl, vat, incl, problems)
    _raise_if(problems)
    return ParsedInvoice(
        number=number, issue_date=issue_date or "", due_date=due_date, currency=currency,
        total_excl=excl, total_vat=vat, total_incl=incl, buyer_name=buyer_name,
        buyer_endpoint=buyer_endpoint, format="cii",
    )


# ---------- FatturaPA (Italië) ----------

def _parse_fattura(root: ET.Element) -> ParsedInvoice:
    problems: list[str] = []
    body = _child(root, "FatturaElettronicaBody")
    doc = _find(body, "DatiGenerali/DatiGeneraliDocumento")
    party = _find(root, "FatturaElettronicaHeader/CessionarioCommittente/DatiAnagrafici/Anagrafica")

    number = _text(doc, "Numero") or ""
    issue_date = _iso_date(_text(doc, "Data"), "factuurdatum", problems)
    currency = _text(doc, "Divisa") or ""
    buyer_name = _text(party, "Denominazione") or " ".join(
        filter(None, [_text(party, "Nome"), _text(party, "Cognome")])
    )
    _require(problems, {"number": number, "buyer_name": buyer_name, "currency": currency})
    due_raw = _text(body, "DatiPagamento/DettaglioPagamento/DataScadenzaPagamento")
    due_date = _iso_date(due_raw, "vervaldatum", problems) if due_raw else None

    blocks = _all(_find(body, "DatiBeniServizi"), "DatiRiepilogo")
    if not blocks:
        problems.append("geen btw-samenvatting (DatiRiepilogo)")
    excl = Decimal("0")
    vat = Decimal("0")
    for block in blocks:
        excl += _decimal(_text(block, "ImponibileImporto"), "imponibel bedrag", problems) or Decimal("0")
        vat += _decimal(_text(block, "Imposta"), "btw-bedrag", problems) or Decimal("0")
    # Het totaal incl. btw kan bollo of ingehouden bedragen bevatten; daarom geen optelcontrole hier.
    incl = _decimal(_text(doc, "ImportoTotaleDocumento"), "totaal incl. btw", problems)
    _raise_if(problems)
    return ParsedInvoice(
        number=number, issue_date=issue_date or "", due_date=due_date, currency=currency,
        total_excl=excl.quantize(CENT), total_vat=vat.quantize(CENT), total_incl=incl,
        buyer_name=buyer_name, buyer_endpoint="", format="fatturapa",
    )


# ---------- KSeF FA(2) / FA(3) (Polen) ----------
# Gebaseerd op de openbare FA-specificatie; niet tegen de officiële XSD gecontroleerd.

def _parse_ksef(root: ET.Element) -> ParsedInvoice:
    problems: list[str] = []
    fa = _child(root, "Fa")
    if fa is None:
        raise InvoiceError(["geen factuurgegevens (Fa) gevonden"])
    number = _text(fa, "P_2") or ""
    issue_date = _iso_date(_text(fa, "P_1"), "factuurdatum", problems)
    currency = _text(fa, "KodWaluty") or ""
    buyer = _find(root, "Podmiot2/DaneIdentyfikacyjne")
    buyer_name = _text(buyer, "Nazwa") or " ".join(filter(None, [_text(buyer, "Imie"), _text(buyer, "Nazwisko")]))
    nip = _text(buyer, "NIP")
    buyer_endpoint = f"PL:{nip}" if nip else ""  # keuze: de NIP van de afnemer als identificatie

    _require(problems, {"number": number, "issue_date": issue_date or "", "currency": currency, "buyer_name": buyer_name})
    net_fields = [c for c in fa if re.fullmatch(r"P_13_\d+", _local(c.tag))]
    vat_fields = [c for c in fa if re.fullmatch(r"P_14_\d", _local(c.tag))]
    if not net_fields:
        problems.append("netto bedrag ontbreekt (P_13_*)")
    excl = Decimal("0")
    for field_node in net_fields:
        excl += _decimal(field_node.text, "netto bedrag", problems) or Decimal("0")
    vat = Decimal("0")
    for field_node in vat_fields:
        vat += _decimal(field_node.text, "btw-bedrag", problems) or Decimal("0")
    incl = _decimal(_text(fa, "P_15"), "totaal incl. btw", problems)
    # Geen optelcontrole: vrijstellingen en speciale regelingen maken de som niet altijd betrouwbaar.
    _raise_if(problems)
    return ParsedInvoice(
        number=number, issue_date=issue_date or "", due_date=None, currency=currency,
        total_excl=excl.quantize(CENT), total_vat=vat.quantize(CENT), total_incl=incl,
        buyer_name=buyer_name, buyer_endpoint=buyer_endpoint, format="ksef",
    )


# ---------- FacturaE 3.2.x (Spanje) ----------
# Gebaseerd op de openbare FacturaE-specificatie; niet tegen de officiële XSD gecontroleerd.

def _parse_facturae(root: ET.Element) -> ParsedInvoice:
    problems: list[str] = []
    invoice = _find(root, "Invoices/Invoice")
    number = _text(invoice, "InvoiceHeader/InvoiceNumber") or ""
    issue_date = _iso_date(_text(invoice, "InvoiceIssueData/IssueDate"), "factuurdatum", problems)
    currency = _text(invoice, "InvoiceIssueData/InvoiceCurrencyCode") or ""
    buyer = _find(root, "Parties/BuyerParty")
    buyer_name = _text(buyer, "LegalEntity/CorporateName") or " ".join(
        filter(None, [_text(buyer, "Individual/Name"), _text(buyer, "Individual/FirstSurname"),
                      _text(buyer, "Individual/SecondSurname")])
    )
    due_raw = _text(invoice, "PaymentDetails/Installment/InstallmentDueDate")
    due_date = _iso_date(due_raw, "vervaldatum", problems) if due_raw else None

    _require(problems, {"number": number, "issue_date": issue_date or "", "currency": currency, "buyer_name": buyer_name})
    totals = _find(invoice, "InvoiceTotals")
    before_tax = _text(totals, "TotalGrossAmountBeforeTaxes") or _text(totals, "TotalGrossAmount")
    excl = _decimal(before_tax, "totaal excl. btw", problems)
    vat = _decimal(_text(totals, "TotalTaxOutputs"), "btw-bedrag", problems)
    incl = _decimal(_text(totals, "TotalExecutableAmount"), "totaal incl. btw", problems)
    # Geen optelcontrole: ingehouden bedragen en kortingen maken de som niet betrouwbaar als check.
    _raise_if(problems)
    return ParsedInvoice(
        number=number, issue_date=issue_date or "", due_date=due_date, currency=currency,
        total_excl=excl, total_vat=vat, total_incl=incl, buyer_name=buyer_name,
        buyer_endpoint="", format="facturae",
    )


# ---------- EDI X12 810 (VS) ----------

def _parse_x12(data: bytes, filename: str) -> ParsedInvoice:
    text = data.decode("latin-1")
    if len(text) < 4:
        raise _unknown(filename)
    element_sep = text[3]
    segment_term = text[105] if len(text) > 105 else "~"
    segments = [part.strip().split(element_sep) for part in text.split(segment_term) if part.strip()]

    st = next((seg for seg in segments if seg[0] == "ST"), None)
    if st is None or len(st) < 2 or st[1] != "810":
        raise InvoiceError([f"onbekend formaat: X12-bestand is geen 810-factuur ({filename or 'onbekend bestand'})"])

    problems: list[str] = []
    big = next((seg for seg in segments if seg[0] == "BIG"), [])
    issue_date = _compact_date(big[1] if len(big) > 1 else None, "factuurdatum", problems)
    number = big[2] if len(big) > 2 else ""
    buyer = next((seg for seg in segments if seg[0] == "N1" and len(seg) > 2 and seg[1] == "BT"), [])
    buyer_name = buyer[2] if len(buyer) > 2 else ""
    currency_segs = [seg for seg in segments if seg[0] == "CUR" and len(seg) > 2]
    chosen = next((seg for seg in currency_segs if seg[1] == "BY"), currency_segs[0] if currency_segs else None)
    currency = chosen[2] if chosen else "USD"
    tds = next((seg for seg in segments if seg[0] == "TDS"), [])
    total_cents = tds[1] if len(tds) > 1 else None

    _require(problems, {"number": number, "issue_date": issue_date or "", "buyer_name": buyer_name})
    if total_cents is None or not total_cents.isdigit():
        problems.append("totaalbedrag (TDS) ontbreekt of is ongeldig")
        incl = None
    else:
        incl = (Decimal(total_cents) / 100).quantize(CENT)
    _raise_if(problems)
    # Een 810 bevat meestal geen btw-splitsing: het totaal wordt als incl. btw gelezen en moet gecontroleerd worden.
    return ParsedInvoice(
        number=number, issue_date=issue_date or "", due_date=None, currency=currency,
        total_excl=incl, total_vat=Decimal("0.00"), total_incl=incl, buyer_name=buyer_name,
        buyer_endpoint="", format="x12-810", needs_review=True,
    )


# ---------- PDF met ingebedde XML ----------

_PREFERRED_ATTACHMENTS = ("factur-x.xml", "zugferd-invoice.xml")


def _embedded_xml(data: bytes) -> bytes | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        attachments: dict[str, Any] = PdfReader(io.BytesIO(data)).attachments
    except Exception:
        return None
    for wanted in _PREFERRED_ATTACHMENTS:
        for name, payloads in attachments.items():
            if name.lower() == wanted and payloads:
                return payloads[0]
    for name, payloads in attachments.items():
        if name.lower().endswith(".xml") and payloads:
            return payloads[0]
    return None


# ---------- dispatch ----------

def _parse_xml(data: bytes, filename: str) -> ParsedInvoice:
    try:
        root = parse_xml(data)
    except ET.ParseError:
        raise _unknown(filename)
    if root.tag == UBL_INVOICE:
        return parse_ubl(data)
    namespace, _, local = root.tag.lstrip("{").rpartition("}")
    if local == "CrossIndustryInvoice" and namespace == CII_NS:
        return _parse_cii(root)
    if local == "FatturaElettronica":
        return _parse_fattura(root)
    if local == "Faktura" and namespace.startswith(KSEF_NS_PREFIX):
        return _parse_ksef(root)
    if local == "Facturae" and namespace.startswith(FACTURAE_NS_PREFIX):
        return _parse_facturae(root)
    raise _unknown(filename)


def document_text(data: bytes) -> str:
    """De tekst van de factuur zoals die bewaard wordt: de XML of X12-tekst, bij een PDF de ingebedde XML."""
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"%PDF"):
        embedded = _embedded_xml(head) or b""
        return embedded.decode("utf-8", errors="replace")
    return head.decode("utf-8", errors="replace")


def parse_invoice(data: bytes, filename: str = "") -> ParsedInvoice:
    """Herken het formaat aan de inhoud en geef een ParsedInvoice.

    Geeft InvoiceError met alle problemen als een herkend formaat verplichte velden mist. Een onherkenbaar
    bestand, en een PDF zonder ingebedde XML, geeft een InvoiceError met de melding om de factuur handmatig in te voeren.
    """
    head = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"%PDF"):
        embedded = _embedded_xml(head)
        if embedded is None:
            raise InvoiceError([NOT_READABLE])
        return _parse_xml(embedded.lstrip(b"\xef\xbb\xbf \t\r\n"), filename)
    if head.startswith(b"ISA"):
        return _parse_x12(head, filename)
    if head.startswith(b"<"):
        return _parse_xml(head, filename)
    raise _unknown(filename)
