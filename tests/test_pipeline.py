from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from werker.models import Classification, Draft, Intent, Message
from werker.pipeline import Pipeline
from werker.policy import Action, PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.rules import RuleConfig
from werker.store import Store

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"


class FakeClassifier:
    def __init__(self, intent: Intent, confidence: float = 0.95):
        self.result = Classification(intent, "normaal", "nl", confidence, "test")
        self.calls = 0

    def classify(self, msg):
        self.calls += 1
        return self.result


class FakeReplier:
    def __init__(self, body: str, needs_human: bool = False):
        self.draft_result = Draft(body=body, needs_human=needs_human, reason="")
        self.calls = 0

    def draft(self, msg, cls, prices):
        self.calls += 1
        return self.draft_result


class FakeConnector:
    name = "mail"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def fetch_new(self):
        return []

    def send(self, msg, body):
        self.sent.append((msg.external_id, body))


def _msg(external_id: str = "<1@x>", sender: str = "klant@example.com") -> Message:
    return Message(
        channel="mail",
        external_id=external_id,
        thread_id=external_id,
        sender=sender,
        subject="Prijs?",
        body="Wat kost een herenknipbeurt?",
        received_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "werker.db"))
    yield s
    s.close()


def _pipeline(store, classifier, replier, connector, autosend=False, rules=RuleConfig()):
    prices = PriceBook(lambda: load_yaml(EXAMPLE))
    prices.refresh()
    policy = PolicyConfig(autosend_enabled=autosend, max_auto_amount=Decimal("500"))
    return Pipeline(store, classifier, replier, prices, policy, rules, {"mail": connector})


def test_default_mode_queues_draft_and_does_not_send(store):
    connector = FakeConnector()
    pipeline = _pipeline(store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 25,00."), connector)
    decision = pipeline.process(_msg())
    assert decision.action is Action.DRAFT
    assert connector.sent == []
    assert [row["subject"] for row in store.pending_drafts()] == ["Prijs?"]


def test_invented_price_is_queued_with_error_and_not_sent(store):
    connector = FakeConnector()
    pipeline = _pipeline(
        store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 19,00."), connector, autosend=True
    )
    decision = pipeline.process(_msg())
    assert decision.action is Action.DRAFT
    assert connector.sent == []
    [row] = store.pending_drafts()
    assert "19,00" in row["errors"]


def test_valid_price_is_sent_when_autosend_enabled(store):
    connector = FakeConnector()
    pipeline = _pipeline(
        store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 25,00."), connector, autosend=True
    )
    decision = pipeline.process(_msg())
    assert decision.action is Action.SEND
    assert connector.sent == [("<1@x>", "Dat is € 25,00.")]
    assert store.pending_drafts() == []


def test_duplicate_message_is_processed_once(store):
    classifier = FakeClassifier(Intent.PRIJSVRAAG)
    replier = FakeReplier("Dat is € 25,00.")
    pipeline = _pipeline(store, classifier, replier, FakeConnector())
    pipeline.process(_msg())
    assert pipeline.process(_msg()) is None
    assert classifier.calls == 1


def test_blocklisted_sender_skips_model_entirely(store):
    classifier = FakeClassifier(Intent.PRIJSVRAAG)
    rules = RuleConfig(blocklist=frozenset({"klant@example.com"}))
    pipeline = _pipeline(store, classifier, FakeReplier("x"), FakeConnector(), rules=rules)
    decision = pipeline.process(_msg())
    assert decision.action is Action.LABEL
    assert classifier.calls == 0


def test_approve_blocks_invented_price_unless_forced(store):
    connector = FakeConnector()
    pipeline = _pipeline(store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 19,00."), connector)
    pipeline.process(_msg())
    [row] = store.pending_drafts()
    with pytest.raises(ValueError):
        pipeline.approve(row["id"])
    assert connector.sent == []
    pipeline.approve(row["id"], force=True)
    assert connector.sent == [("<1@x>", "Dat is € 19,00.")]


def test_approve_with_edited_correct_text_sends_edit(store):
    connector = FakeConnector()
    pipeline = _pipeline(store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 19,00."), connector)
    pipeline.process(_msg())
    [row] = store.pending_drafts()
    pipeline.approve(row["id"], body="Dat is € 25,00. Groet, Salon")
    assert connector.sent == [("<1@x>", "Dat is € 25,00. Groet, Salon")]


def test_reject_removes_draft_from_queue(store):
    pipeline = _pipeline(store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 25,00."), FakeConnector())
    pipeline.process(_msg())
    [row] = store.pending_drafts()
    pipeline.reject(row["id"])
    assert store.pending_drafts() == []


def test_failed_send_keeps_message_visible(store):
    class BrokenConnector(FakeConnector):
        def send(self, msg, body):
            raise ConnectionError("smtp down")

    pipeline = _pipeline(
        store, FakeClassifier(Intent.PRIJSVRAAG), FakeReplier("Dat is € 25,00."), BrokenConnector(), autosend=True
    )
    decision = pipeline.process(_msg())
    assert decision.action is Action.SEND
    assert store.get_message(1).body == "Wat kost een herenknipbeurt?"
    assert store.find_message_id("mail", "<1@x>") == 1
    row = store._db.execute("select status from messages where id = 1").fetchone()
    assert row["status"] == "verzending mislukt"
