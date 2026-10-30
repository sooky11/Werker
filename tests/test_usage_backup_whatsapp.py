import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from werker import cli
from werker.classify import CLASSIFY_MODEL, Classifier
from werker.connectors.whatsapp import TEMPLATE_SENT, WhatsAppConnector, WindowClosedError
from werker.invoicing import Invoicing
from werker.models import Intent, Message
from werker.reply import REPLY_MODEL, Replier
from werker.reports import summary
from werker.runtime import Runtime
from werker.store import Store
from werker.web import create_app

from test_billing import OVERDUE_DAY, FakeMailer, ubl

ANNA = {"name": "anna", "role": "beheerder", "password": "anna-wachtwoord"}
JAN = {"name": "jan", "role": "medewerker", "password": "jan-wachtwoord"}


class UsageClient:
    """Nep-client die net als de echte SDK een usage-object meestuurt."""

    def __init__(self, tool_input, input_tokens=120, output_tokens=30):
        self.messages = self
        self.tool_input = tool_input
        self.usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)

    def create(self, **kwargs):
        name = kwargs["tool_choice"]["name"]
        return SimpleNamespace(
            model=kwargs["model"],
            usage=self.usage,
            content=[SimpleNamespace(type="tool_use", name=name, input=self.tool_input)],
        )


def _msg(received=None, channel="whatsapp", external_id="1"):
    return Message(
        channel=channel,
        external_id=external_id,
        thread_id="31612345678",
        sender="Sara <31612345678>",
        subject="",
        body="Wat kost een knipbeurt?",
        received_at=received or datetime.now(timezone.utc),
    )


@pytest.fixture
def store(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    yield db
    db.close()


# ---------- AI-verbruik ----------

def test_usage_is_recorded_for_classify_and_draft(store):
    classify_client = UsageClient(
        {"intent": "prijsvraag", "urgency": "laag", "language": "nl", "confidence": 0.9, "summary": "Prijs."}
    )
    cls = Classifier(classify_client, on_usage=store.record_ai_usage).classify(_msg())
    draft_client = UsageClient({"body": "Dat is € 25,00.", "needs_human": False, "reason": ""}, 800, 200)
    Replier(draft_client, on_usage=store.record_ai_usage).draft(_msg(), cls, [])

    rows = store._db.execute("select model, purpose, input_tokens, output_tokens, at from ai_usage order by id").fetchall()
    assert [(r["model"], r["purpose"], r["input_tokens"], r["output_tokens"]) for r in rows] == [
        (CLASSIFY_MODEL, "classificeren", 120, 30),
        (REPLY_MODEL, "concept", 800, 200),
    ]
    assert all(datetime.fromisoformat(r["at"]).tzinfo is not None for r in rows)


def test_usage_callback_is_optional_and_a_failing_callback_does_not_break(store):
    client = UsageClient(
        {"intent": "prijsvraag", "urgency": "laag", "language": "nl", "confidence": 0.9, "summary": "Prijs."}
    )
    assert Classifier(client).classify(_msg()).intent is Intent.PRIJSVRAAG

    def broken(*args):
        raise RuntimeError("database weg")

    assert Classifier(client, on_usage=broken).classify(_msg()).intent is Intent.PRIJSVRAAG


def test_runtime_wires_usage_into_the_store(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    runtime = Runtime(db, tmp_path, {"ANTHROPIC_API_KEY": "sk-test", "WERKER_IMESSAGE": "false"})
    runtime.reconfigure()
    assert runtime.pipeline._classifier._on_usage == db.record_ai_usage
    assert runtime.pipeline._replier._on_usage == db.record_ai_usage
    db.close()


NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_reports_show_tokens_and_cost_only_when_prices_are_set(store):
    store.record_ai_usage("classificeren", "claude-haiku-5-5", 1_000_000, 100_000, at=NOW - timedelta(days=1))
    store.record_ai_usage("concept", "claude-sonnet-5-5", 500_000, 200_000, at=NOW - timedelta(days=2))
    store.record_ai_usage("concept", "claude-sonnet-5-5", 999, 999, at=NOW - timedelta(days=60))  # buiten de periode

    ai = summary(store, NOW, days=30)["ai"]
    assert ai["aanroepen"] == 2
    assert ai["per_model"]["claude-haiku-5-5"] == {"aanroepen": 1, "invoer_tokens": 1_000_000, "uitvoer_tokens": 100_000}
    assert ai["per_model"]["claude-sonnet-5-5"]["uitvoer_tokens"] == 200_000
    assert ai["geschatte_kosten"] is None
    assert "Geen prijzen ingesteld" in ai["toelichting"]

    only_one = summary(store, NOW, days=30, price_input="1", price_output="")["ai"]
    assert only_one["geschatte_kosten"] is None

    priced = summary(store, NOW, days=30, price_input="1", price_output="5")["ai"]
    # 1,5 miljoen invoer x € 1 + 0,3 miljoen uitvoer x € 5
    assert priced["geschatte_kosten"] == "3.00"


def test_ai_price_settings_must_be_non_negative_numbers(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    runtime = Runtime(db, tmp_path, {"WERKER_IMESSAGE": "false"})
    runtime.update(values={"WERKER_AI_PRICE_INPUT": "0.8", "WERKER_AI_PRICE_OUTPUT": ""})
    assert runtime.env()["WERKER_AI_PRICE_INPUT"] == "0.8"
    for bad in ("-1", "gratis", "nan"):
        with pytest.raises(ValueError):
            runtime.update(values={"WERKER_AI_PRICE_OUTPUT": bad})
    db.close()


def test_reports_endpoint_uses_the_price_settings(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    runtime = Runtime(db, tmp_path, {"WERKER_IMESSAGE": "false"})
    db.record_ai_usage("concept", "claude-sonnet-5-5", 2_000_000, 0)
    http = TestClient(create_app(runtime))
    assert http.get("/api/reports").json()["ai"]["geschatte_kosten"] is None
    runtime.update(values={"WERKER_AI_PRICE_INPUT": "2", "WERKER_AI_PRICE_OUTPUT": "10"})
    assert http.get("/api/reports").json()["ai"]["geschatte_kosten"] == "4.00"
    db.close()


# ---------- back-up en herstel ----------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WERKER_DB", str(tmp_path / "werker.db"))
    db = Store(str(tmp_path / "werker.db"))
    db.add_message(_msg(channel="mail:x", external_id="bewaard"))
    db.close()
    (tmp_path / "secrets.json").write_text(json.dumps({"ANTHROPIC_API_KEY": "sk-geheim"}))
    return tmp_path


def _tables(path):
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
        names = {row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")}
        count = connection.execute("select count(*) from messages").fetchone()[0]
    finally:
        connection.close()
    return names, count


def test_backup_is_a_valid_copy_without_secrets(home):
    target = home / "kopie.db"
    assert cli.main(["backup", str(target)]) == 0
    names, count = _tables(target)
    assert count == 1 and "werker_backup_secrets" not in names
    assert b"sk-geheim" not in target.read_bytes()
    assert cli.main(["backup", str(target)]) == 1  # bestaat al: niet overschrijven


def test_backup_with_secrets_is_private(home):
    target = home / "met-geheimen.db"
    assert cli.main(["backup", str(target), "--met-geheimen"]) == 0
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    connection = sqlite3.connect(target)
    assert dict(connection.execute("select key, value from werker_backup_secrets")) == {"ANTHROPIC_API_KEY": "sk-geheim"}
    connection.close()


def test_restore_requires_ja_and_makes_a_safety_copy(home):
    target = home / "kopie.db"
    assert cli.main(["backup", str(target)]) == 0
    db = Store(str(home / "werker.db"))
    db.add_message(_msg(channel="mail:x", external_id="na-de-backup"))
    db.close()

    assert cli.main(["restore", str(target)]) == 1
    assert _tables(home / "werker.db")[1] == 2  # zonder --ja niets gewijzigd

    assert cli.main(["restore", str(target), "--ja"]) == 0
    assert _tables(home / "werker.db")[1] == 1
    [safety] = list(home.glob("werker.db.voor-herstel-*"))
    assert _tables(safety)[1] == 2
    assert json.loads((home / "secrets.json").read_text()) == {"ANTHROPIC_API_KEY": "sk-geheim"}


def test_restore_with_secrets_writes_them_back_and_not_into_the_database(home):
    target = home / "met-geheimen.db"
    assert cli.main(["backup", str(target), "--met-geheimen"]) == 0
    (home / "secrets.json").write_text(json.dumps({"ANTHROPIC_API_KEY": "sk-nieuw"}))

    assert cli.main(["restore", str(target), "--ja"]) == 0
    assert json.loads((home / "secrets.json").read_text()) == {"ANTHROPIC_API_KEY": "sk-geheim"}
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(home / "secrets.json").st_mode) == 0o600
    assert "werker_backup_secrets" not in _tables(home / "werker.db")[0]
    [old] = list(home.glob("secrets.json.voor-herstel-*"))
    assert "sk-nieuw" in old.read_text()


def test_restore_refuses_a_file_that_is_no_backup(home):
    junk = home / "rommel.db"
    junk.write_text("geen database")
    assert cli.main(["restore", str(junk), "--ja"]) == 1
    assert _tables(home / "werker.db")[1] == 1


def test_backup_endpoint_is_for_beheerders_only(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    runtime = Runtime(db, tmp_path, {"WERKER_IMESSAGE": "false"})
    runtime.update(users=[ANNA, JAN], secrets={"ANTHROPIC_API_KEY": "sk-geheim"})
    db.add_message(_msg(channel="mail:x"))
    http = TestClient(create_app(runtime))

    assert http.get("/api/backup", auth=("jan", "jan-wachtwoord")).status_code == 403

    response = http.get("/api/backup", auth=("anna", "anna-wachtwoord"))
    assert response.status_code == 200
    assert "werker-backup-" in response.headers["content-disposition"]
    assert response.content.startswith(b"SQLite format 3")
    assert b"sk-geheim" not in response.content
    copy = tmp_path / "download.db"
    copy.write_bytes(response.content)
    assert _tables(copy)[1] == 1
    db.close()


# ---------- WhatsApp buiten het venster van 24 uur ----------

def _graph(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read()))
        return httpx.Response(200, json={"messages": [{"id": "wamid.9"}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_outside_window_sends_the_template_when_configured():
    seen, notes = [], []
    connector = WhatsAppConnector(
        "12345", "token", client=_graph(seen), template_name="opvolging", template_lang="nl",
        on_template=lambda msg, name: notes.append(name),
    )
    old = _msg(datetime.now(timezone.utc) - timedelta(hours=25))
    assert connector.send(old, "Hallo") == TEMPLATE_SENT
    assert seen == [{
        "messaging_product": "whatsapp",
        "to": "31612345678",
        "type": "template",
        "template": {"name": "opvolging", "language": {"code": "nl"}},
    }]
    assert notes == ["opvolging"]


def test_outside_window_without_template_still_raises():
    seen = []
    connector = WhatsAppConnector("12345", "token", client=_graph(seen))
    with pytest.raises(WindowClosedError):
        connector.send(_msg(datetime.now(timezone.utc) - timedelta(hours=25)), "Hallo")
    assert seen == []


def test_inside_window_sends_the_text_even_with_a_template():
    seen = []
    connector = WhatsAppConnector("12345", "token", client=_graph(seen), template_name="opvolging")
    assert connector.send(_msg(), "Hallo") is None
    assert seen[0]["type"] == "text" and seen[0]["text"] == {"body": "Hallo"}


def test_runtime_records_the_template_in_the_audit_log(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    runtime = Runtime(db, tmp_path, {
        "WHATSAPP_TOKEN": "token", "WHATSAPP_PHONE_NUMBER_ID": "12345", "WERKER_IMESSAGE": "false",
    })
    runtime.update(values={"WHATSAPP_TEMPLATE_NAME": "opvolging"})
    with pytest.raises(ValueError):
        runtime.update(values={"WHATSAPP_TEMPLATE_NAME": "Geen Geldige Naam"})
    connector = runtime.pipeline._connectors["whatsapp"]
    seen = []
    connector._client = _graph(seen)
    old = _msg(datetime.now(timezone.utc) - timedelta(hours=30))
    message_id = db.add_message(old)

    connector.send(old, "Hallo")
    events = [(row["event"], row["detail"]) for row in db.audit_log(message_id)]
    assert any(event == "sjabloon verstuurd" and "de klant moet eerst reageren" in detail for event, detail in events)
    assert seen[0]["template"]["language"] == {"code": "nl"}
    db.close()


# ---------- herinneringen via de juiste mailbox ----------

class NamedMailer(FakeMailer):
    def __init__(self, name):
        super().__init__()
        self.name = name


def test_reminder_goes_out_from_the_mailbox_the_client_last_used(store, tmp_path):
    client_id = store.create_client("Klant B.V.", [("email", "klant@x.nl")])
    store.add_message(_msg(datetime(2026, 10, 1, tzinfo=timezone.utc), "mail:kantoor", "a"), client_id=client_id)
    store.add_message(_msg(datetime(2026, 10, 5, tzinfo=timezone.utc), "mail:winkel", "b"), client_id=client_id)
    store.add_message(_msg(datetime(2026, 10, 9, tzinfo=timezone.utc), "mail:weg", "c"), client_id=client_id)
    store.add_message(_msg(datetime(2026, 10, 10, tzinfo=timezone.utc), "whatsapp", "d"), client_id=client_id)

    kantoor, winkel = NamedMailer("mail:kantoor"), NamedMailer("mail:winkel")
    service = Invoicing(store, tmp_path / "map")
    service.mailer = kantoor
    service.mail_connectors = {"mail:kantoor": kantoor, "mail:winkel": winkel}
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_id)
    [reminder_id] = service.collect_reminders(OVERDUE_DAY)

    service.approve_reminder(reminder_id)
    # mail:weg is niet meer gekoppeld, dus de laatst gebruikte bestaande mailbox: mail:winkel.
    assert len(winkel.sent) == 1 and kantoor.sent == []


def test_reminder_falls_back_to_the_first_mailbox(store, tmp_path):
    client_id = store.create_client("Klant B.V.", [("email", "klant@x.nl")])
    kantoor = NamedMailer("mail:kantoor")
    service = Invoicing(store, tmp_path / "map")
    service.mailer = kantoor
    service.mail_connectors = {"mail:kantoor": kantoor}
    service.import_xml(ubl("2026-001", due="2026-10-31"), client_id=client_id)
    [reminder_id] = service.collect_reminders(OVERDUE_DAY)
    service.approve_reminder(reminder_id)
    assert len(kantoor.sent) == 1


def test_runtime_hands_all_mailboxes_to_invoicing(tmp_path):
    db = Store(str(tmp_path / "w.db"))
    mailboxes = [
        {"name": n, "host": "imap.x", "user": "u", "smtp_host": "smtp.x", "sender": f"{n}@x.nl", "password_env": f"PW_{n}"}
        for n in ("kantoor", "winkel")
    ]
    runtime = Runtime(db, tmp_path, {
        "WERKER_MAILBOXES": json.dumps(mailboxes), "PW_kantoor": "a", "PW_winkel": "b", "WERKER_IMESSAGE": "false",
    })
    runtime.reconfigure()
    assert set(runtime.invoicing.mail_connectors) == {"mail:kantoor", "mail:winkel"}
    assert runtime.invoicing.mailer is runtime.invoicing.mail_connectors["mail:kantoor"]
    db.close()



def test_template_only_keeps_draft_open_instead_of_marking_sent(tmp_path):
    from datetime import datetime, timedelta, timezone

    from werker.connectors.whatsapp import TEMPLATE_SENT
    from werker.models import Classification, Draft, Intent, Message
    from werker.pipeline import Pipeline
    from werker.policy import PolicyConfig
    from werker.prices import PriceBook
    from werker.rules import RuleConfig
    from werker.store import WAITING, Store

    store = Store(str(tmp_path / "t.db"))

    class Classifier:
        def classify(self, msg):
            return Classification(Intent.OVERIG, "normaal", "nl", 0.9, "vraag")

    class Replier:
        def draft(self, msg, cls, prices, slots=None):
            return Draft("Dank voor je bericht.", False, "")

    class TemplateConnector:
        name = "whatsapp"

        def fetch_new(self):
            return []

        def send(self, msg, body):
            return TEMPLATE_SENT

    pipeline = Pipeline(store, Classifier(), Replier(), PriceBook(lambda: []), PolicyConfig(), RuleConfig(),
                        {"whatsapp": TemplateConnector()})
    old = datetime.now(timezone.utc) - timedelta(days=2)
    pipeline.process(Message("whatsapp", "w9", "316", "Sara <316>", "", "Hoi", old))
    draft_id = store.pending_drafts()[0]["id"]
    pipeline.approve(draft_id)
    assert store.get_draft(draft_id)["status"] == WAITING
    row = store.get_message_row(store.find_message_id("whatsapp", "w9"))
    assert row["status"] == "wacht op klant"
    store.close()
