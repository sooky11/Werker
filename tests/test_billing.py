from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from werker.invoicing import SENT, TO_SEND, WAITING_PAYMENT, Invoicing, ReminderSendError
from werker.models import Classification, Intent, Message
from werker.reports import summary
from werker.runtime import Runtime
from werker.store import Store
from werker.store_billing import (
    REMINDER_EXPIRED,
    REMINDER_NO_ADDRESS,
    REMINDER_REJECTED,
    REMINDER_SENT,
    REMINDER_WAITING,
)
from werker.web import create_app

CUSTOMIZATION = "urn:cen.eu:en16931:2017#conformant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"


def ubl(number, due, excl="1000.00", vat="210.00", incl="1210.00", buyer="Klant B.V."):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:CustomizationID>{CUSTOMIZATION}</cbc:CustomizationID>
  <cbc:ID>{number}</cbc:ID>
  <cbc:IssueDate>2026-10-01</cbc:IssueDate>
  <cbc:DueDate>{due}</cbc:DueDate>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:AccountingCustomerParty><cac:Party>
    <cbc:EndpointID schemeID="0106">12345678</cbc:EndpointID>
    <cac:PartyLegalEntity><cbc:RegistrationName>{buyer}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">{vat}</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:TaxExclusiveAmount currencyID="EUR">{excl}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">{incl}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">{incl}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID></cac:InvoiceLine>
</Invoice>"""


class FakeMailer:
    name = "mail:test"

    def __init__(self, fail=False):
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def send(self, msg, body):
        raise AssertionError("herinneringen horen als nieuwe mail te gaan, niet als antwoord")

    def send_new(self, to, subject, body):
        if self.fail:
            raise ConnectionError("smtp niet bereikbaar")
        self.sent.append((to, subject, body))


OVERDUE_DAY = datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    yield store, tmp_path / "peppol-map"
    store.close()


def client_with_email(store, name="Klant B.V.", email="klant@x.nl"):
    return store.create_client(name, [("email", email)])


# ---------- betalingsherinneringen ----------

def test_overdue_open_invoice_gets_a_reminder(env):
    store, folder = env
    service = Invoicing(store, folder)
    client_id = client_with_email(store)
    invoice_id = service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_id)

    created = service.collect_reminders(OVERDUE_DAY)

    assert len(created) == 1
    [row] = store.pending_reminders(WAITING_PAYMENT)
    assert row["invoice_id"] == invoice_id
    assert row["status"] == REMINDER_WAITING
    assert row["recipient"] == "klant@x.nl"
    assert "2026-001" in row["subject"] and "herinnering" in row["subject"].lower()
    assert "31-10-2026" in row["body"]


def test_paid_or_not_yet_due_invoices_get_no_reminder(env):
    store, folder = env
    service = Invoicing(store, folder)
    client_id = client_with_email(store)
    service.import_xml(ubl("NOG", due="2026-12-31"), client_id=client_id)
    paid_id = service.import_xml(ubl("BETAALD", due="2026-10-31"), client_id=client_id)
    store.update_invoice(paid_id, status=SENT)

    assert service.collect_reminders(OVERDUE_DAY) == []
    assert store.pending_reminders(WAITING_PAYMENT) == []


def test_no_second_reminder_within_seven_days(env):
    store, folder = env
    service = Invoicing(store, folder)
    client_id = client_with_email(store)
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_id)

    assert len(service.collect_reminders(OVERDUE_DAY)) == 1
    assert service.collect_reminders(OVERDUE_DAY) == []
    assert service.collect_reminders(OVERDUE_DAY + timedelta(days=6, hours=23)) == []
    assert len(service.collect_reminders(OVERDUE_DAY + timedelta(days=7))) == 1


def test_client_without_email_is_held_as_no_address(env):
    store, folder = env
    service = Invoicing(store, folder)
    service.import_xml(ubl("2026-001", due="2026-10-31"))

    service.collect_reminders(OVERDUE_DAY)

    [row] = store.pending_reminders(WAITING_PAYMENT)
    assert row["status"] == REMINDER_NO_ADDRESS
    assert row["recipient"] == ""
    with pytest.raises(ValueError, match="geen e-mailadres"):
        service.approve_reminder(row["id"])


def test_approve_sends_to_the_client_email(env):
    store, folder = env
    mailer = FakeMailer()
    service = Invoicing(store, folder)
    service.mailer = mailer
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_with_email(store))
    service.collect_reminders(OVERDUE_DAY)
    [row] = store.pending_reminders(WAITING_PAYMENT)

    service.approve_reminder(row["id"])

    [(to, subject, body)] = mailer.sent
    assert to == "klant@x.nl"
    assert subject == "Betalingsherinnering factuur 2026-001"
    assert "2026-001" in body
    assert store.get_reminder(row["id"])["status"] == REMINDER_SENT
    assert store.pending_reminders(WAITING_PAYMENT) == []
    with pytest.raises(ValueError):
        service.approve_reminder(row["id"])
    assert len(mailer.sent) == 1


def test_failed_send_keeps_the_reminder_waiting(env):
    store, folder = env
    service = Invoicing(store, folder)
    service.mailer = FakeMailer(fail=True)
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_with_email(store))
    service.collect_reminders(OVERDUE_DAY)
    [row] = store.pending_reminders(WAITING_PAYMENT)

    with pytest.raises(ReminderSendError):
        service.approve_reminder(row["id"])
    kept = store.get_reminder(row["id"])
    assert kept["status"] == REMINDER_WAITING
    assert "niet bereikbaar" in kept["last_error"]


def test_reject_leaves_nothing_to_send(env):
    store, folder = env
    mailer = FakeMailer()
    service = Invoicing(store, folder)
    service.mailer = mailer
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_with_email(store))
    service.collect_reminders(OVERDUE_DAY)
    [row] = store.pending_reminders(WAITING_PAYMENT)

    service.reject_reminder(row["id"])

    assert mailer.sent == []
    assert store.get_reminder(row["id"])["status"] == REMINDER_REJECTED
    assert store.pending_reminders(WAITING_PAYMENT) == []
    with pytest.raises(ValueError):
        service.approve_reminder(row["id"])


def test_reminder_expires_when_the_invoice_gets_paid(env):
    store, folder = env
    service = Invoicing(store, folder)
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_with_email(store))
    service.collect_reminders(OVERDUE_DAY)
    [row] = store.pending_reminders(WAITING_PAYMENT)
    store.update_invoice(row["invoice_id"], status=TO_SEND)

    service.collect_reminders(OVERDUE_DAY + timedelta(hours=1))

    assert store.get_reminder(row["id"])["status"] == REMINDER_EXPIRED


def test_reminder_endpoints(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    runtime = Runtime(store, tmp_path, {})
    service = Invoicing(store, tmp_path / "map")
    runtime.invoicing = service
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_with_email(store))
    service.collect_reminders(OVERDUE_DAY)
    http = TestClient(create_app(runtime))

    listed = http.get("/api/reminders").json()
    assert [r["number"] for r in listed] == ["2026-001"] and listed[0]["status"] == REMINDER_WAITING

    rejected = http.post(f"/api/reminders/{listed[0]['id']}/reject", json={})
    assert rejected.status_code == 200 and rejected.json() == {"status": "afgewezen"}
    assert http.get("/api/reminders").json() == []
    assert http.post("/api/reminders/999/approve", json={}).status_code == 404
    store.close()


# ---------- overzicht ----------

def _add_message(store, external_id, channel, received, intent, sent_at=None, client_id=None):
    message_id = store.add_message(
        Message(channel, external_id, external_id, "klant@x.nl", "vraag", "tekst", received),
        client_id=client_id,
    )
    store.set_classification(message_id, _classification(intent))
    if sent_at is not None:
        store._db.execute(
            "insert into audit (at, message_id, event, detail) values (?, ?, 'verzonden', 'test')",
            (sent_at.isoformat(), message_id),
        )
        store._db.commit()
    return message_id


def _classification(intent):
    return Classification(intent, "normaal", "nl", 0.9, "samenvatting")


REPORT_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_summary_numbers_on_a_small_fixture(env):
    store, folder = env
    utc = timezone.utc
    _add_message(store, "a", "mail", datetime(2026, 10, 30, 10, 0, tzinfo=utc), Intent.PRIJSVRAAG,
                 sent_at=datetime(2026, 10, 30, 10, 30, tzinfo=utc))
    _add_message(store, "b", "whatsapp", datetime(2026, 10, 31, 9, 0, tzinfo=utc), Intent.KLACHT)
    _add_message(store, "c", "mail", datetime(2026, 10, 29, 8, 0, tzinfo=utc), Intent.PRIJSVRAAG,
                 sent_at=datetime(2026, 10, 29, 9, 0, tzinfo=utc))
    _add_message(store, "oud", "mail", datetime(2026, 9, 1, 8, 0, tzinfo=utc), Intent.PRIJSVRAAG,
                 sent_at=datetime(2026, 9, 1, 8, 30, tzinfo=utc))
    service = Invoicing(store, folder)
    service.import_xml(ubl("A", due="2026-10-31"))  # 1210,00, verlopen op 1 november
    b = service.import_xml(ubl("B", due="2026-12-31", excl="82.64", vat="17.36", incl="100.00"))
    store.update_invoice(b, status=TO_SEND)
    c = service.import_xml(ubl("C", due="2026-12-31", excl="82.64", vat="17.36", incl="100.00"))
    store.update_invoice(c, status=SENT)

    report = summary(store, REPORT_NOW, days=30)

    assert report["berichten"]["totaal"] == 3
    assert report["berichten"]["per_kanaal"] == {"mail": 2, "whatsapp": 1}
    assert report["berichten"]["per_intentie"] == {"prijsvraag": 2, "klacht": 1}
    assert report["responstijd"]["antwoorden"] == 2
    assert report["responstijd"]["gemiddeld_minuten"] == 45.0  # 30 en 60 minuten
    assert report["prijsvragen_beantwoord"] == 2
    assert report["openstaand"] == {"aantal": 2, "totaal": "1310.00"}
    assert report["verlopen"] == {"aantal": 1, "totaal": "1210.00"}


def test_summary_says_so_when_nothing_was_sent(env):
    store, _ = env
    report = summary(store, REPORT_NOW, days=30)
    assert report["responstijd"]["gemiddeld_minuten"] is None
    assert "Geen verzonden antwoorden" in report["responstijd"]["toelichting"]
    assert report["openstaand"] == {"aantal": 0, "totaal": "0.00"}


def test_reports_endpoint(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    runtime = Runtime(store, tmp_path, {})
    http = TestClient(create_app(runtime))
    body = http.get("/api/reports?days=7").json()
    assert body["dagen"] == 7 and "responstijd" in body
    store.close()
