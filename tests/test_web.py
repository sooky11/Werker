import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from werker.models import Classification, Draft, Intent, Message
from werker.pipeline import Pipeline
from werker.policy import PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.rules import RuleConfig
from werker.store import Store
from werker.runtime import Runtime
from werker.web import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"
SECRET = "app-secret"


class FakeClassifier:
    def classify(self, msg):
        return Classification(Intent.PRIJSVRAAG, "normaal", "nl", 0.95, "Vraagt naar de prijs.")


class FakeReplier:
    def __init__(self, body):
        self.body = body

    def draft(self, msg, cls, prices):
        return Draft(body=self.body, needs_human=False, reason="")


class FakeConnector:
    name = "mail"

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def fetch_new(self):
        return []

    def send(self, msg, body):
        if self.fail:
            raise ConnectionError("smtp down")
        self.sent.append((msg.external_id, body))


def _msg(external_id="<1@x>") -> Message:
    return Message(
        channel="mail",
        external_id=external_id,
        thread_id=external_id,
        sender="Jan Jansen <jan@example.com>",
        subject="Prijs herenknipbeurt?",
        body="Hoi, wat kost een herenknipbeurt?",
        received_at=datetime.now(timezone.utc),
    )


def _build(tmp_path, reply_body="Dat is € 25,00.", ui_password="", connector=None, env=None):
    store = Store(str(tmp_path / "w.db"))
    connector = connector or FakeConnector()
    if env is None:
        env = {"WHATSAPP_APP_SECRET": SECRET, "WHATSAPP_VERIFY_TOKEN": "verify"}
    runtime = Runtime(store, tmp_path, env)
    runtime.pipeline = Pipeline(
        store,
        FakeClassifier(),
        FakeReplier(reply_body),
        PriceBook(lambda: load_yaml(EXAMPLE)),
        PolicyConfig(),
        RuleConfig(),
        {"mail": connector},
    )
    runtime.pipeline.prices.refresh()
    app = create_app(runtime, ui_password=ui_password)
    return TestClient(app), runtime.pipeline, connector


def _seed(client_pipeline_connector) -> None:
    _, pipeline, _ = client_pipeline_connector
    pipeline.process(_msg())


@pytest.fixture
def seeded(tmp_path):
    setup = _build(tmp_path)
    _seed(setup)
    return setup


def test_index_serves_inbox_page(seeded):
    client, _, _ = seeded
    response = client.get("/")
    assert response.status_code == 200
    assert "Wacht op goedkeuring" in response.text


def test_pending_list_and_detail(seeded):
    client, _, _ = seeded
    [item] = client.get("/api/messages", params={"pending": 1}).json()
    assert item["subject"] == "Prijs herenknipbeurt?"
    assert item["draft_id"] is not None
    detail = client.get(f"/api/messages/{item['id']}").json()
    assert detail["body"] == "Hoi, wat kost een herenknipbeurt?"
    assert detail["drafts"][0]["body"] == "Dat is € 25,00."


def test_unknown_message_is_404(seeded):
    client, _, _ = seeded
    assert client.get("/api/messages/999").status_code == 404


def test_stats_count_intents_and_pending(seeded):
    client, _, _ = seeded
    assert client.get("/api/stats").json() == {"intents": {"prijsvraag": 1}, "pending": 1, "scheduled": 0}


def test_search_matches_body(seeded):
    client, _, _ = seeded
    assert len(client.get("/api/messages", params={"q": "knipbeurt"}).json()) == 1
    assert client.get("/api/messages", params={"q": "tandarts"}).json() == []


def test_approve_sends_and_clears_queue(seeded):
    client, _, connector = seeded
    draft_id = client.get("/api/messages", params={"pending": 1}).json()[0]["draft_id"]
    response = client.post(f"/api/drafts/{draft_id}/approve", json={})
    assert response.status_code == 200
    assert connector.sent == [("<1@x>", "Dat is € 25,00.")]
    assert client.get("/api/stats").json()["pending"] == 0


def test_approve_with_invented_price_is_409_until_forced(tmp_path):
    setup = _build(tmp_path, reply_body="Dat is € 19,00.")
    _seed(setup)
    client, _, connector = setup
    draft_id = client.get("/api/messages", params={"pending": 1}).json()[0]["draft_id"]
    blocked = client.post(f"/api/drafts/{draft_id}/approve", json={})
    assert blocked.status_code == 409
    assert connector.sent == []
    forced = client.post(f"/api/drafts/{draft_id}/approve", json={"force": True})
    assert forced.status_code == 200


def test_approve_with_edited_text_sends_the_edit(seeded):
    client, _, connector = seeded
    draft_id = client.get("/api/messages", params={"pending": 1}).json()[0]["draft_id"]
    client.post(f"/api/drafts/{draft_id}/approve", json={"body": "Dat is € 25,00. Groet, Salon"})
    assert connector.sent == [("<1@x>", "Dat is € 25,00. Groet, Salon")]


def test_failed_send_returns_502_and_keeps_draft(tmp_path):
    setup = _build(tmp_path, connector=FakeConnector(fail=True))
    _seed(setup)
    client, _, _ = setup
    draft_id = client.get("/api/messages", params={"pending": 1}).json()[0]["draft_id"]
    assert client.post(f"/api/drafts/{draft_id}/approve", json={}).status_code == 502
    assert client.get("/api/stats").json()["pending"] == 1


def test_reject_clears_queue_without_sending(seeded):
    client, _, connector = seeded
    draft_id = client.get("/api/messages", params={"pending": 1}).json()[0]["draft_id"]
    assert client.post(f"/api/drafts/{draft_id}/reject").status_code == 200
    assert connector.sent == []
    assert client.get("/api/stats").json()["pending"] == 0


def test_sync_endpoint_runs_connectors(tmp_path):
    class InboxConnector(FakeConnector):
        def fetch_new(self):
            return [_msg("<2@x>")]

    setup = _build(tmp_path, connector=InboxConnector())
    client, _, _ = setup
    assert client.post("/api/sync").json() == {"processed": 1}
    assert client.get("/api/stats").json()["pending"] == 1


def test_password_protects_ui_and_api(tmp_path):
    client, _, _ = _build(tmp_path, ui_password="geheim")
    assert client.get("/").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats", auth=("werker", "fout")).status_code == 401
    assert client.get("/api/stats", auth=("werker", "geheim")).status_code == 200


def test_webhook_rejects_missing_or_wrong_signature(tmp_path):
    client, _, _ = _build(tmp_path)
    payload = json.dumps({"entry": []}).encode()
    assert client.post("/webhook/whatsapp", content=payload).status_code == 403
    bad = {"x-hub-signature-256": "sha256=" + "0" * 64}
    assert client.post("/webhook/whatsapp", content=payload, headers=bad).status_code == 403


def test_webhook_accepts_valid_signature(tmp_path):
    client, _, _ = _build(tmp_path)
    payload = json.dumps({"entry": []}).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    response = client.post("/webhook/whatsapp", content=payload, headers={"x-hub-signature-256": signature})
    assert response.status_code == 200


def test_webhook_refuses_everything_without_app_secret(tmp_path):
    client, _, _ = _build(tmp_path, env={})
    payload = json.dumps({"entry": []}).encode()
    signature = "sha256=" + hmac.new(b"", payload, hashlib.sha256).hexdigest()
    response = client.post("/webhook/whatsapp", content=payload, headers={"x-hub-signature-256": signature})
    assert response.status_code == 403
