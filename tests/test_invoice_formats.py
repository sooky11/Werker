import base64
import io
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from werker.invoice_formats import NOT_READABLE, parse_invoice
from werker.invoicing import IN_MAP, NEEDS_REVIEW, WAITING_PAYMENT, Invoicing
from werker.peppol import InvoiceError
from werker.runtime import Runtime
from werker.store import Store
from werker.web import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "invoices"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def pdf_with_attachment(name: str | None, payload: bytes | None) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if name is not None:
        writer.add_attachment(name, payload)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ---------- formaten inlezen ----------

def test_cii_invoice_is_parsed():
    parsed = parse_invoice(fixture("cii_basic.xml"), "cii_basic.xml")
    assert parsed.format == "cii"
    assert parsed.number == "CII-2026-001"
    assert parsed.issue_date == "2026-10-01"
    assert parsed.due_date == "2026-10-31"
    assert parsed.currency == "EUR"
    assert parsed.total_excl == Decimal("1000.00")
    assert parsed.total_vat == Decimal("190.00")
    assert parsed.total_incl == Decimal("1190.00")
    assert parsed.buyer_name == "Klant GmbH"
    assert parsed.buyer_endpoint == "0088:4012345000016"
    assert parsed.needs_review is False


def test_cii_missing_mandatory_fields_lists_all_problems():
    with pytest.raises(InvoiceError) as caught:
        parse_invoice(fixture("cii_missing_fields.xml"))
    text = " ".join(caught.value.problems)
    assert "naam van de klant ontbreekt" in text
    assert "totaal incl. btw ontbreekt" in text


def test_fatturapa_invoice_is_parsed_with_local_names():
    parsed = parse_invoice(fixture("fatturapa_basic.xml"))
    assert parsed.format == "fatturapa"
    assert parsed.number == "FT/2026/001"
    assert parsed.issue_date == "2026-10-01"
    assert parsed.due_date == "2026-10-31"
    assert parsed.currency == "EUR"
    assert parsed.total_excl == Decimal("1000.00")
    assert parsed.total_vat == Decimal("220.00")
    assert parsed.total_incl == Decimal("1220.00")
    assert parsed.buyer_name == "Cliente Italia S.p.A."
    assert parsed.buyer_endpoint == ""


def test_ksef_invoice_is_parsed_with_nip_as_identifier():
    parsed = parse_invoice(fixture("ksef_fa2.xml"))
    assert parsed.format == "ksef"
    assert parsed.number == "FV/2026/10/001"
    assert parsed.issue_date == "2026-10-01"
    assert parsed.currency == "PLN"
    assert parsed.total_excl == Decimal("1000.00")
    assert parsed.total_vat == Decimal("230.00")
    assert parsed.total_incl == Decimal("1230.00")
    assert parsed.buyer_name == "Klient Sp. z o.o."
    assert parsed.buyer_endpoint == "PL:5252344078"


def test_facturae_invoice_is_parsed():
    parsed = parse_invoice(fixture("facturae_basic.xml"))
    assert parsed.format == "facturae"
    assert parsed.number == "FE-2026-001"
    assert parsed.issue_date == "2026-10-01"
    assert parsed.due_date == "2026-10-31"
    assert parsed.currency == "EUR"
    assert parsed.total_excl == Decimal("1000.00")
    assert parsed.total_vat == Decimal("210.00")
    assert parsed.total_incl == Decimal("1210.00")
    assert parsed.buyer_name == "Comprador S.A."
    assert parsed.buyer_endpoint == ""


def test_facturae_missing_fields_lists_all_problems():
    with pytest.raises(InvoiceError) as caught:
        parse_invoice(fixture("facturae_missing_fields.xml"))
    text = " ".join(caught.value.problems)
    assert "naam van de klant ontbreekt" in text
    assert "totaal incl. btw ontbreekt" in text


def test_x12_810_is_parsed_and_needs_review():
    data = fixture("x12_810.edi")
    assert data[105:106] == b"~", "de ISA-header moet 106 tekens lang zijn"
    parsed = parse_invoice(data, "x12_810.edi")
    assert parsed.format == "x12-810"
    assert parsed.needs_review is True
    assert parsed.number == "X12-2026-001"
    assert parsed.issue_date == "2026-10-01"
    assert parsed.currency == "USD"
    assert parsed.buyer_name == "Buyer Corp"
    assert parsed.total_incl == Decimal("1210.00")
    assert parsed.total_excl == parsed.total_incl
    assert parsed.total_vat == Decimal("0")


def test_x12_that_is_not_810_is_unknown():
    data = fixture("x12_810.edi").replace(b"ST*810*", b"ST*850*")
    with pytest.raises(InvoiceError, match="onbekend formaat"):
        parse_invoice(data)


# ---------- herkenning ----------

def test_unknown_format_is_rejected_with_manual_entry_message():
    with pytest.raises(InvoiceError) as caught:
        parse_invoice(b"dit is gewoon tekst", "notitie.txt")
    text = " ".join(caught.value.problems)
    assert "onbekend formaat" in text
    assert NOT_READABLE in text


def test_detection_uses_content_not_file_name():
    parsed = parse_invoice(fixture("cii_basic.xml"), "factuur.pdf")
    assert parsed.format == "cii"


def test_pdf_with_embedded_xml_is_parsed():
    data = pdf_with_attachment("factur-x.xml", fixture("cii_basic.xml"))
    parsed = parse_invoice(data, "factuur.pdf")
    assert parsed.format == "cii"
    assert parsed.number == "CII-2026-001"
    assert parsed.needs_review is False


def test_pdf_with_other_xml_attachment_is_parsed():
    data = pdf_with_attachment("bijlage.xml", fixture("fatturapa_basic.xml"))
    assert parse_invoice(data).format == "fatturapa"


def test_pdf_without_embedded_xml_is_refused_for_manual_entry():
    with pytest.raises(InvoiceError) as caught:
        parse_invoice(pdf_with_attachment(None, None), "scan.pdf")
    assert caught.value.problems == [NOT_READABLE]


# ---------- importeren en verzenden ----------

class FakeAccessPoint:
    def __init__(self):
        self.sent: list[tuple[bytes, str]] = []

    def send(self, xml, recipient):
        self.sent.append((xml, recipient))
        return "tx-1"


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    yield store, tmp_path / "peppol-map"
    store.close()


def test_import_document_stores_format_and_waiting_status(env):
    store, folder = env
    invoice_id = Invoicing(store, folder).import_document(fixture("cii_basic.xml"), "cii_basic.xml")
    row = store.get_invoice(invoice_id)
    assert row["format"] == "cii"
    assert row["needs_review"] == 0
    assert row["status"] == WAITING_PAYMENT


def test_import_of_pdf_with_xml_stores_embedded_invoice(env):
    store, folder = env
    invoice_id = Invoicing(store, folder).import_document(pdf_with_attachment("factur-x.xml", fixture("cii_basic.xml")))
    assert store.get_invoice(invoice_id)["number"] == "CII-2026-001"


def test_pdf_without_xml_is_not_stored(env):
    store, folder = env
    with pytest.raises(InvoiceError):
        Invoicing(store, folder).import_document(pdf_with_attachment(None, None), "scan.pdf")
    assert store.list_invoices() == []


def test_needs_review_invoice_waits_and_is_not_dispatched(env):
    store, folder = env
    access_point = FakeAccessPoint()
    service = Invoicing(store, folder, access_point)
    invoice_id = service.import_document(fixture("x12_810.edi"), "x12_810.edi")
    row = store.get_invoice(invoice_id)
    assert row["status"] == NEEDS_REVIEW
    assert row["format"] == "x12-810"
    assert service.dispatch() == 0
    assert store.get_invoice(invoice_id)["status"] == NEEDS_REVIEW


def test_mark_reviewed_moves_invoice_to_waiting_for_payment(env):
    store, folder = env
    service = Invoicing(store, folder)
    invoice_id = service.import_document(fixture("x12_810.edi"))
    service.mark_reviewed(invoice_id)
    row = store.get_invoice(invoice_id)
    assert row["status"] == WAITING_PAYMENT
    assert row["needs_review"] == 0
    with pytest.raises(ValueError, match="wacht op controle"):
        service.mark_reviewed(invoice_id)


def test_non_peppol_invoice_goes_to_map_with_reason(env):
    store, folder = env
    access_point = FakeAccessPoint()
    service = Invoicing(store, folder, access_point)
    invoice_id = service.import_document(fixture("facturae_basic.xml"), "facturae_basic.xml")
    assert service.mark_paid(invoice_id) == IN_MAP
    assert access_point.sent == []
    assert (folder / "FE-2026-001.xml").exists()
    reason = (folder / "FE-2026-001.txt").read_text(encoding="utf-8")
    assert "geen Peppol-adres" in reason
    [item] = service.map_items()
    assert "geen Peppol-adres" in item["reason"]


def test_non_ubl_format_with_endpoint_is_not_sent_via_peppol(env):
    store, folder = env
    access_point = FakeAccessPoint()
    service = Invoicing(store, folder, access_point)
    invoice_id = service.import_document(fixture("ksef_fa2.xml"))
    assert service.mark_paid(invoice_id) == IN_MAP
    assert access_point.sent == []
    assert (folder / "FV_2026_10_001.xml").exists()  # bestandsnaam is de veilige versie van het nummer
    assert "alleen UBL" in (folder / "FV_2026_10_001.txt").read_text(encoding="utf-8")


# ---------- webroute ----------

def _payload(name: str, data: bytes) -> dict:
    return {"filename": name, "content_base64": base64.b64encode(data).decode("ascii")}


def test_import_file_route_returns_format_and_review_flag(tmp_path):
    runtime = Runtime(Store(str(tmp_path / "w.db")), tmp_path, {})
    client = TestClient(create_app(runtime))

    ok = client.post("/api/invoices/import-file", json=_payload("cii_basic.xml", fixture("cii_basic.xml")))
    assert ok.status_code == 200
    assert ok.json()["format"] == "cii" and ok.json()["needs_review"] is False

    review = client.post("/api/invoices/import-file", json=_payload("x12_810.edi", fixture("x12_810.edi")))
    assert review.json()["needs_review"] is True and review.json()["format"] == "x12-810"

    duplicate = client.post("/api/invoices/import-file", json=_payload("cii_basic.xml", fixture("cii_basic.xml")))
    assert duplicate.status_code == 409

    refused = client.post("/api/invoices/import-file", json=_payload("scan.pdf", pdf_with_attachment(None, None)))
    assert refused.status_code == 422
    assert refused.json()["detail"]["problems"] == [NOT_READABLE]

    bad = client.post("/api/invoices/import-file", json={"filename": "x", "content_base64": "%%%"})
    assert bad.status_code == 422
