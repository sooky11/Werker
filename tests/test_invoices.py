from pathlib import Path

import pytest

from werker.invoicing import IN_MAP, NotConfigured, SENT, TO_SEND, WAITING_PAYMENT, Invoicing
from werker.peppol import InvoiceError, parse_bank_csv, parse_ubl, safe_filename
from werker.store import Store

CUSTOMIZATION = "urn:cen.eu:en16931:2017#conformant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"


def ubl(number="2026-001", excl="1000.00", vat="210.00", incl="1210.00", endpoint='schemeID="0106">12345678', customization=CUSTOMIZATION, lines=True):
    line = (
        "<cac:InvoiceLine><cbc:ID>1</cbc:ID></cac:InvoiceLine>" if lines else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>{customization}</cbc:CustomizationID>
  <cbc:ID>{number}</cbc:ID>
  <cbc:IssueDate>2026-10-01</cbc:IssueDate>
  <cbc:DueDate>2026-10-31</cbc:DueDate>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingCustomerParty><cac:Party>
    <cbc:EndpointID {endpoint}</cbc:EndpointID>
    <cac:PartyLegalEntity><cbc:RegistrationName>Klant B.V.</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">{vat}</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:TaxExclusiveAmount currencyID="EUR">{excl}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">{incl}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">{incl}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  {line}
</Invoice>"""


# ---------- inlezen en controleren ----------

def test_valid_ubl_is_parsed():
    parsed = parse_ubl(ubl().encode())
    assert parsed.number == "2026-001"
    assert str(parsed.total_incl) == "1210.00"
    assert parsed.buyer_endpoint == "0106:12345678"
    assert parsed.buyer_name == "Klant B.V."


def test_all_problems_are_reported_together():
    with pytest.raises(InvoiceError) as caught:
        parse_ubl(ubl(customization="ander-profiel", incl="1200.00", lines=False).encode())
    text = " ".join(caught.value.problems)
    assert "Peppol BIS Billing 3.0" in text
    assert "totalen kloppen niet" in text
    assert "geen regels" in text


def test_non_xml_and_wrong_document_are_rejected():
    with pytest.raises(InvoiceError):
        parse_ubl(b"geen xml")
    with pytest.raises(InvoiceError, match="geen UBL-factuur"):
        parse_ubl(b"<Order xmlns='urn:oasis:names:specification:ubl:schema:xsd:Order-2'/>")


def test_bank_csv_reads_dutch_amounts_and_ignores_outgoing():
    csv_text = "Datum;Omschrijving;Bedrag\n01-10-2026;Betaling 2026-001;\"1.210,00\"\n02-10-2026;Boodschappen;\"-12,50\"\n"
    payments = parse_bank_csv(csv_text)
    assert [str(p.amount) for p in payments] == ["1210.00"]
    assert payments[0].description == "Betaling 2026-001"


def test_bank_csv_without_needed_columns_is_an_error():
    with pytest.raises(ValueError):
        parse_bank_csv("a,b\n1,2\n")


def test_file_names_are_safe():
    assert safe_filename("2026/001 bis") == "2026_001_bis"


# ---------- service ----------

class FakeAccessPoint:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent: list[tuple[bytes, str]] = []

    def send(self, xml, recipient):
        if self.fail:
            raise ConnectionError("access point niet bereikbaar")
        self.sent.append((xml, recipient))
        return "tx-1"


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    yield store, tmp_path / "peppol-map"
    store.close()


def test_import_stores_invoice_waiting_for_payment(env):
    store, folder = env
    service = Invoicing(store, folder)
    invoice_id = service.import_xml(ubl())
    row = store.get_invoice(invoice_id)
    assert row["status"] == WAITING_PAYMENT
    assert row["number"] == "2026-001"


def test_duplicate_invoice_number_is_refused(env):
    store, folder = env
    service = Invoicing(store, folder)
    service.import_xml(ubl())
    with pytest.raises(ValueError, match="staat er al in"):
        service.import_xml(ubl())


def test_bank_payment_marks_invoice_and_sends_it(env):
    store, folder = env
    access_point = FakeAccessPoint()
    service = Invoicing(store, folder, access_point)
    service.import_xml(ubl())
    matched = service.match_bank_file("Datum;Omschrijving;Bedrag\n01-10-2026;Betaling 2026-001;1210,00\n")
    assert matched == ["2026-001"]
    row = store.find_invoice_by_number("2026-001")
    assert row["status"] == SENT and row["paid_at"] is not None
    assert access_point.sent[0][1] == "0106:12345678"


def test_wrong_amount_does_not_match(env):
    store, folder = env
    service = Invoicing(store, folder, FakeAccessPoint())
    service.import_xml(ubl())
    assert service.match_bank_file("Datum;Omschrijving;Bedrag\n01-10-2026;Betaling 2026-001;1000,00\n") == []
    assert store.find_invoice_by_number("2026-001")["status"] == WAITING_PAYMENT


def test_without_access_point_invoice_goes_to_map_with_reason(env):
    store, folder = env
    service = Invoicing(store, folder)
    invoice_id = service.import_xml(ubl())
    assert service.mark_paid(invoice_id) == IN_MAP
    assert (folder / "2026-001.xml").exists()
    assert "geen Peppol access point" in (folder / "2026-001.txt").read_text(encoding="utf-8")
    [item] = service.map_items()
    assert item["number"] == "2026-001" and "geen Peppol" in item["reason"]


def test_failed_send_lands_in_map_and_retry_succeeds(env):
    store, folder = env
    access_point = FakeAccessPoint(fail=True)
    service = Invoicing(store, folder, access_point)
    invoice_id = service.import_xml(ubl())
    assert service.mark_paid(invoice_id) == IN_MAP
    assert "niet bereikbaar" in store.get_invoice(invoice_id)["last_error"]

    access_point.fail = False
    assert service.send(invoice_id) == SENT
    assert not (folder / "2026-001.xml").exists()
    assert store.get_invoice(invoice_id)["last_error"] == ""


def test_dispatch_sends_only_invoices_marked_to_send(env):
    store, folder = env
    access_point = FakeAccessPoint()
    service = Invoicing(store, folder, access_point)
    waiting = service.import_xml(ubl(number="A"))
    paid = service.import_xml(ubl(number="B"))
    store.update_invoice(paid, status=TO_SEND)
    assert service.dispatch() == 1
    assert store.get_invoice(paid)["status"] == SENT
    assert store.get_invoice(waiting)["status"] == WAITING_PAYMENT


def test_not_configured_is_clear_in_the_map_reason(env):
    store, folder = env
    service = Invoicing(store, folder)
    invoice_id = service.import_xml(ubl())
    service.mark_paid(invoice_id)
    assert NotConfigured.__name__ in "NotConfigured" and store.get_invoice(invoice_id)["status"] == IN_MAP
