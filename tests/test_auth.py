import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from werker.auth import clean_accounts, hash_password, verify_password
from werker.models import Classification, Draft, Intent, Message
from werker.pipeline import Pipeline
from werker.policy import PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.rules import RuleConfig
from werker.runtime import Runtime
from werker.store import Store
from werker.web import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"

ANNA = {"name": "anna", "role": "beheerder", "password": "anna-wachtwoord"}
JAN = {"name": "jan", "role": "medewerker", "password": "jan-wachtwoord"}


class FakeClassifier:
    def classify(self, msg):
        return Classification(Intent.PRIJSVRAAG, "normaal", "nl", 0.95, "Vraagt naar de prijs.")


class FakeReplier:
    def draft(self, msg, cls, prices):
        return Draft(body="Dat is € 25,00.", needs_human=False, reason="")


class FakeConnector:
    name = "mail"

    def __init__(self):
        self.sent = []

    def fetch_new(self):
        return []

    def send(self, msg, body):
        self.sent.append((msg.external_id, body))


def _msg() -> Message:
    return Message(
        channel="mail",
        external_id="<1@x>",
        thread_id="<1@x>",
        sender="Jan Jansen <jan@example.com>",
        subject="Prijs herenknipbeurt?",
        body="Hoi, wat kost een herenknipbeurt?",
        received_at=datetime.now(timezone.utc),
    )


def _build(tmp_path, users=None, legacy=""):
    store = Store(str(tmp_path / "w.db"))
    connector = FakeConnector()
    # Zonder bedenktijd: deze tests verwachten dat goedkeuren meteen verstuurt.
    runtime = Runtime(store, tmp_path, {"WERKER_UNDO_SECONDS": "0"})
    if users is not None:
        runtime.update(users=users)  # bouwt eerst een eigen pipeline; die vervangen we hieronder
    runtime.pipeline = Pipeline(
        store,
        FakeClassifier(),
        FakeReplier(),
        PriceBook(lambda: load_yaml(EXAMPLE)),
        PolicyConfig(),
        RuleConfig(),
        {"mail": connector},
    )
    runtime.pipeline.prices.refresh()
    app = create_app(runtime, ui_password=legacy)
    return TestClient(app), runtime, connector


def _pending_draft_id(client, auth) -> int:
    return client.get("/api/messages", params={"pending": 1}, auth=auth).json()[0]["draft_id"]


def _audit_details(client, auth, message_id: int) -> list[str]:
    log = client.get(f"/api/messages/{message_id}", auth=auth).json()["log"]
    return [entry["detail"] for entry in log]


def test_hashing_round_trip_and_salt():
    stored = hash_password("geheim123")
    assert stored.startswith("pbkdf2$200000$")
    assert "geheim123" not in stored
    assert verify_password("geheim123", stored)
    assert not verify_password("geheim124", stored)
    assert not verify_password("geheim123", "geen-hash")
    assert hash_password("geheim123") != stored  # elke hash krijgt een eigen salt


def test_clean_accounts_requires_password_for_new_users_and_a_beheerder():
    secrets_map: dict[str, str] = {}
    with pytest.raises(ValueError, match="wachtwoord nodig"):
        clean_accounts([{"name": "anna", "role": "beheerder"}], secrets_map)
    with pytest.raises(ValueError, match="beheerder"):
        clean_accounts([{"name": "jan", "role": "medewerker", "password": "jan-wachtwoord"}], secrets_map)
    with pytest.raises(ValueError, match="minstens 8"):
        clean_accounts([{"name": "anna", "role": "beheerder", "password": "kort"}], secrets_map)


def test_wrong_password_and_unknown_user_are_rejected(tmp_path):
    client, _, _ = _build(tmp_path, users=[ANNA, JAN])
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", auth=("jan", "fout-wachtwoord")).status_code == 401
    assert client.get("/api/stats", auth=("piet", "jan-wachtwoord")).status_code == 401
    assert client.get("/api/stats", auth=("jan", "jan-wachtwoord")).status_code == 200


def test_passwords_are_stored_hashed_and_never_returned(tmp_path):
    client, runtime, _ = _build(tmp_path, users=[ANNA, JAN])
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert stored["USER_JAN_PW"].startswith("pbkdf2$200000$")
    assert "jan-wachtwoord" not in (tmp_path / "secrets.json").read_text(encoding="utf-8")

    settings = client.get("/api/settings", auth=("anna", "anna-wachtwoord")).json()
    assert settings["users"] == [{"name": "anna", "role": "beheerder"}, {"name": "jan", "role": "medewerker"}]
    assert "pbkdf2" not in json.dumps(settings)


def test_medewerker_can_approve_but_cannot_change_settings(tmp_path):
    client, runtime, connector = _build(tmp_path, users=[ANNA, JAN])
    runtime.pipeline.process(_msg())
    jan = ("jan", "jan-wachtwoord")

    denied = client.put("/api/settings", json={"values": {"WERKER_THEME": "donker"}}, auth=jan)
    assert denied.status_code == 403

    approved = client.post(f"/api/drafts/{_pending_draft_id(client, jan)}/approve", json={}, auth=jan)
    assert approved.status_code == 200
    assert len(connector.sent) == 1


def test_beheerder_can_change_settings_and_manage_users(tmp_path):
    client, _, _ = _build(tmp_path, users=[ANNA, JAN])
    anna = ("anna", "anna-wachtwoord")

    assert client.put("/api/settings", json={"values": {"WERKER_THEME": "donker"}}, auth=anna).status_code == 200

    added = {"name": "sanne", "role": "medewerker", "password": "sanne-wachtwoord"}
    response = client.put(
        "/api/settings", json={"users": [ANNA, JAN, added]}, auth=anna
    )
    assert response.status_code == 200
    assert client.get("/api/stats", auth=("sanne", "sanne-wachtwoord")).status_code == 200


def test_removed_user_loses_access_immediately(tmp_path):
    client, _, _ = _build(tmp_path, users=[ANNA, JAN])
    anna = ("anna", "anna-wachtwoord")
    assert client.get("/api/stats", auth=("jan", "jan-wachtwoord")).status_code == 200

    assert client.put("/api/settings", json={"users": [ANNA]}, auth=anna).status_code == 200

    assert client.get("/api/stats", auth=("jan", "jan-wachtwoord")).status_code == 401
    stored = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert "USER_JAN_PW" not in stored


def test_new_user_without_password_is_refused_with_400(tmp_path):
    client, _, _ = _build(tmp_path, users=[ANNA])
    response = client.put(
        "/api/settings",
        json={"users": [ANNA, {"name": "sanne", "role": "medewerker"}]},
        auth=("anna", "anna-wachtwoord"),
    )
    assert response.status_code == 400
    assert "wachtwoord nodig" in response.json()["detail"]


def test_legacy_password_still_works_without_accounts(tmp_path):
    client, _, _ = _build(tmp_path, legacy="geheim")
    assert client.get("/").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", auth=("werker", "geheim")).status_code == 200
    assert client.put("/api/settings", json={"values": {"WERKER_THEME": "zacht"}}, auth=("werker", "geheim")).status_code == 200


def test_without_password_or_accounts_the_ui_is_open(tmp_path):
    client, _, _ = _build(tmp_path)
    assert client.get("/api/stats").status_code == 200


def test_accounts_replace_the_legacy_password(tmp_path):
    client, _, _ = _build(tmp_path, users=[ANNA], legacy="geheim")
    assert client.get("/api/stats", auth=("werker", "geheim")).status_code == 401
    assert client.get("/api/stats", auth=("anna", "anna-wachtwoord")).status_code == 200


def test_approve_records_the_acting_user_in_the_audit_log(tmp_path):
    client, runtime, _ = _build(tmp_path, users=[ANNA, JAN])
    runtime.pipeline.process(_msg())
    jan = ("jan", "jan-wachtwoord")
    draft_id = _pending_draft_id(client, jan)
    message_id = client.get("/api/messages", params={"pending": 1}, auth=jan).json()[0]["id"]

    assert client.post(f"/api/drafts/{draft_id}/approve", json={}, auth=jan).status_code == 200

    assert "door mens goedgekeurd door jan" in _audit_details(client, jan, message_id)


def test_reject_records_the_acting_user(tmp_path):
    client, runtime, _ = _build(tmp_path, users=[ANNA, JAN])
    runtime.pipeline.process(_msg())
    anna = ("anna", "anna-wachtwoord")
    draft_id = _pending_draft_id(client, anna)
    message_id = client.get("/api/messages", params={"pending": 1}, auth=anna).json()[0]["id"]

    assert client.post(f"/api/drafts/{draft_id}/reject", json={}, auth=anna).status_code == 200

    assert f"concept {draft_id} door anna" in _audit_details(client, anna, message_id)


def test_approve_without_accounts_keeps_the_old_audit_text(tmp_path):
    client, runtime, _ = _build(tmp_path)
    runtime.pipeline.process(_msg())
    draft_id = _pending_draft_id(client, None)
    message_id = client.get("/api/messages", params={"pending": 1}).json()[0]["id"]

    assert client.post(f"/api/drafts/{draft_id}/approve", json={}).status_code == 200

    assert "door mens goedgekeurd" in _audit_details(client, None, message_id)
