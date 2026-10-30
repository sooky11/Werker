from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from werker.models import Classification, Draft, Intent, Message
from werker.pipeline import Pipeline
from werker.policy import PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.rules import RuleConfig
from werker.runtime import Runtime, parse_signatures
from werker.store import SCHEDULED, WAITING, Store
from werker.web import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"
ANNA = {"name": "anna", "role": "beheerder", "password": "anna-wachtwoord"}
JAN = {"name": "jan", "role": "medewerker", "password": "jan-wachtwoord"}
ANNA_AUTH = ("anna", "anna-wachtwoord")
JAN_AUTH = ("jan", "jan-wachtwoord")


class FakeClassifier:
    def __init__(self):
        self.calls = 0

    def classify(self, msg):
        self.calls += 1
        return Classification(Intent.PRIJSVRAAG, "normaal", "nl", 0.95, "Vraagt naar de prijs.")


class ExplodingClassifier:
    def classify(self, msg):
        raise AssertionError("de classifier mag niet worden aangeroepen")


class FakeReplier:
    def draft(self, msg, cls, prices):
        return Draft(body="Dat is € 25,00.", needs_human=False, reason="")


class FakeConnector:
    def __init__(self, name):
        self.name = name
        self.sent = []

    def fetch_new(self):
        return []

    def send(self, msg, body):
        self.sent.append((msg.external_id, body))


def _msg(external_id="<1@x>", channel="mail", sender="Jan Jansen <jan@example.com>") -> Message:
    return Message(
        channel=channel,
        external_id=external_id,
        thread_id=external_id,
        sender=sender,
        subject="Prijs herenknipbeurt?",
        body="Hoi, wat kost een herenknipbeurt?",
        received_at=datetime.now(timezone.utc),
    )


def _build(tmp_path, env=None, users=None, signatures=None):
    store = Store(str(tmp_path / "w.db"))
    runtime = Runtime(store, tmp_path, env or {})
    if users is not None:
        runtime.update(users=users)
    connectors = {"mail": FakeConnector("mail"), "whatsapp": FakeConnector("whatsapp")}
    runtime.pipeline = Pipeline(
        store,
        FakeClassifier(),
        FakeReplier(),
        PriceBook(lambda: load_yaml(EXAMPLE)),
        PolicyConfig(),
        RuleConfig(),
        connectors,
        signatures=signatures,
    )
    runtime.pipeline.prices.refresh()
    return TestClient(create_app(runtime)), runtime, connectors


def _pending_draft_id(client, auth=None) -> int:
    return client.get("/api/messages", params={"pending": 1}, auth=auth).json()[0]["draft_id"]


# ---------- ongedaan maken ----------


def test_approve_now_waits_for_the_undo_window(tmp_path):
    client, runtime, connectors = _build(tmp_path)
    runtime.pipeline.process(_msg())
    draft_id = _pending_draft_id(client)
    before = datetime.now(timezone.utc)

    response = client.post(f"/api/drafts/{draft_id}/approve", json={})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == SCHEDULED
    assert data["draft_id"] == draft_id
    undo_until = datetime.fromisoformat(data["undo_until"])
    assert timedelta(seconds=28) <= undo_until - before <= timedelta(seconds=32)
    assert connectors["mail"].sent == []
    # Binnen de bedenktijd verstuurt send_due niets.
    assert runtime.pipeline.send_due() == 0
    assert runtime.pipeline.send_due(now=undo_until + timedelta(seconds=1)) == 1
    assert connectors["mail"].sent == [("<1@x>", "Dat is € 25,00.")]


def test_undo_within_the_window_prevents_sending(tmp_path):
    client, runtime, connectors = _build(tmp_path)
    runtime.pipeline.process(_msg())
    draft_id = _pending_draft_id(client)
    data = client.post(f"/api/drafts/{draft_id}/approve", json={}).json()

    assert client.post(f"/api/drafts/{draft_id}/cancel-schedule", json={}).json() == {"status": WAITING}
    later = datetime.fromisoformat(data["undo_until"]) + timedelta(minutes=1)
    assert runtime.pipeline.send_due(now=later) == 0
    assert connectors["mail"].sent == []
    assert runtime.store.get_draft(draft_id)["status"] == WAITING


def test_undo_seconds_zero_sends_immediately(tmp_path):
    client, runtime, connectors = _build(tmp_path, env={"WERKER_UNDO_SECONDS": "0"})
    runtime.pipeline.process(_msg())
    data = client.post(f"/api/drafts/{_pending_draft_id(client)}/approve", json={}).json()
    assert data["status"] == "verzonden"
    assert data["undo_until"] is None
    assert connectors["mail"].sent == [("<1@x>", "Dat is € 25,00.")]


def test_scheduled_send_at_a_chosen_time_keeps_its_time(tmp_path):
    client, runtime, connectors = _build(tmp_path)
    runtime.pipeline.process(_msg())
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    data = client.post(f"/api/drafts/{_pending_draft_id(client)}/approve", json={"send_at": later.isoformat()}).json()
    assert data["status"] == SCHEDULED
    assert data["undo_until"] is None
    assert datetime.fromisoformat(data["send_at"]) == later


@pytest.mark.parametrize("value", ["-1", "301", "abc", "1.5"])
def test_undo_seconds_is_validated(tmp_path, value):
    _, runtime, _ = _build(tmp_path)
    with pytest.raises(ValueError):
        runtime.update(values={"WERKER_UNDO_SECONDS": value})


# ---------- categorie corrigeren ----------


def test_intent_correction_updates_message_and_remembers_sender(tmp_path):
    client, runtime, _ = _build(tmp_path)
    runtime.pipeline.process(_msg())
    [item] = client.get("/api/messages").json()

    response = client.patch(f"/api/messages/{item['id']}/intent", json={"intent": "klacht", "remember": True})

    assert response.status_code == 200
    assert runtime.store.get_message_row(item["id"])["intent"] == "klacht"
    [rule] = client.get("/api/sender-rules").json()
    assert rule["address"] == "jan@example.com"
    assert rule["intent"] == "klacht"
    log = [entry["event"] for entry in client.get(f"/api/messages/{item['id']}").json()["log"]]
    assert "categorie aangepast" in log

    # Een nieuw bericht van dezelfde afzender krijgt de onthouden categorie, zonder het model.
    runtime.pipeline.reconfigure(classifier=ExplodingClassifier())
    runtime.pipeline.process(_msg("<2@x>", sender="jan@EXAMPLE.com"))
    new_id = runtime.store.find_message_id("mail", "<2@x>")
    assert runtime.store.get_message_row(new_id)["intent"] == "klacht"


def test_intent_correction_without_remember_stores_no_rule(tmp_path):
    client, runtime, _ = _build(tmp_path)
    runtime.pipeline.process(_msg())
    [item] = client.get("/api/messages").json()
    assert client.patch(f"/api/messages/{item['id']}/intent", json={"intent": "overig"}).status_code == 200
    assert runtime.store.get_message_row(item["id"])["intent"] == "overig"
    assert client.get("/api/sender-rules").json() == []


def test_intent_correction_rejects_unknown_intent_and_message(tmp_path):
    client, runtime, _ = _build(tmp_path)
    runtime.pipeline.process(_msg())
    [item] = client.get("/api/messages").json()
    assert client.patch(f"/api/messages/{item['id']}/intent", json={"intent": "onzin"}).status_code == 422
    assert client.patch("/api/messages/999/intent", json={"intent": "klacht"}).status_code == 404


def test_medewerker_cannot_delete_sender_rules(tmp_path):
    client, runtime, _ = _build(tmp_path, users=[ANNA, JAN])
    runtime.pipeline.process(_msg())
    [item] = client.get("/api/messages", auth=JAN_AUTH).json()
    response = client.patch(
        f"/api/messages/{item['id']}/intent", json={"intent": "klacht", "remember": True}, auth=JAN_AUTH
    )
    rule_id = response.json()["rule_id"]

    assert client.request("DELETE", f"/api/sender-rules/{rule_id}", json={}, auth=JAN_AUTH).status_code == 403
    assert client.get("/api/sender-rules", auth=JAN_AUTH).status_code == 403
    [rule] = client.get("/api/sender-rules", auth=ANNA_AUTH).json()
    assert rule["created_by"] == "jan"
    assert client.request("DELETE", f"/api/sender-rules/{rule_id}", json={}, auth=ANNA_AUTH).status_code == 200
    assert client.get("/api/sender-rules", auth=ANNA_AUTH).json() == []
    assert client.request("DELETE", f"/api/sender-rules/{rule_id}", json={}, auth=ANNA_AUTH).status_code == 404


# ---------- ondertekening ----------

SIGNATURE = "Met vriendelijke groet,\nSalon X"


def test_signature_is_appended_once_for_its_channel_only(tmp_path):
    client, runtime, connectors = _build(
        tmp_path, env={"WERKER_UNDO_SECONDS": "0"}, signatures={"mail": SIGNATURE}
    )
    runtime.pipeline.process(_msg("<1@x>"))
    runtime.pipeline.process(_msg("<2@x>", channel="whatsapp", sender="+31612345678"))
    runtime.pipeline.process(_msg("<3@x>"))
    pending = client.get("/api/messages", params={"pending": 1}).json()
    by_ext = {runtime.store.get_message_row(item["id"])["external_id"]: item["draft_id"] for item in pending}

    client.post(f"/api/drafts/{by_ext['<1@x>']}/approve", json={})
    client.post(f"/api/drafts/{by_ext['<2@x>']}/approve", json={})
    already_signed = "Dat is € 25,00.\n\n" + SIGNATURE
    client.post(f"/api/drafts/{by_ext['<3@x>']}/approve", json={"body": already_signed})

    assert connectors["mail"].sent == [
        ("<1@x>", "Dat is € 25,00.\n\n" + SIGNATURE),
        ("<3@x>", already_signed),
    ]
    assert connectors["whatsapp"].sent == [("<2@x>", "Dat is € 25,00.")]


def test_signatures_setting_is_validated_and_reaches_the_pipeline(tmp_path):
    _, runtime, _ = _build(tmp_path)
    for bad in ('["a"]', '{"mail": 3}', '{"mail": "' + "x" * 1001 + '"}', "{kapot"):
        with pytest.raises(ValueError):
            runtime.update(values={"WERKER_SIGNATURES": bad})
    runtime.update(values={"WERKER_SIGNATURES": '{"mail:zakelijk": "Groet,\\nSalon X"}'})
    assert runtime.pipeline.signatures == {"mail:zakelijk": "Groet,\nSalon X"}
    assert parse_signatures("{kapot") == {}


def test_remembered_sender_rule_never_autosends(tmp_path):
    from datetime import datetime, timezone

    from werker.models import Draft, Intent, Message
    from werker.pipeline import Pipeline
    from werker.policy import Action, PolicyConfig
    from werker.prices import PriceBook
    from werker.rules import RuleConfig
    from werker.store import Store

    store = Store(str(tmp_path / "r.db"))
    store.set_sender_rule("klant@voorbeeld.nl", "prijsvraag", "test")

    class NoClassifier:
        def classify(self, msg):
            raise AssertionError("de onthouden regel moet het model overslaan")

    class Replier:
        def draft(self, msg, cls, prices, slots=None):
            return Draft("Prijs is € 25,00.", False, "")

    class Connector:
        name = "mail"
        sent = []

        def fetch_new(self):
            return []

        def send(self, msg, body):
            self.sent.append(body)

    connector = Connector()
    prices = PriceBook(lambda: [])
    pipeline = Pipeline(store, NoClassifier(), Replier(), prices,
                        PolicyConfig(autosend_enabled=True, autosend_intents=frozenset({Intent.PRIJSVRAAG})),
                        RuleConfig(), {"mail": connector})
    msg = Message("mail", "<r@x>", "<r@x>", "klant@voorbeeld.nl", "Vraag", "Wat kost het?", datetime.now(timezone.utc))
    decision = pipeline.process(msg)
    assert decision.action is Action.DRAFT
    assert connector.sent == []
    store.close()
