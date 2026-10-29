import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from werker.classify import Classification
from werker.models import Draft, Intent, Message
from werker.pipeline import Pipeline, SendError
from werker.policy import Action, PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.reply import Revision, RevisionError, apply_edits
from werker.rules import RuleConfig
from werker.runtime import Runtime, mailbox_secret_key
from werker.store import SCHEDULED, WAITING, Store

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"


class FakeClassifier:
    def classify(self, msg):
        return Classification(Intent.PRIJSVRAAG, "normaal", "nl", 0.95, "Vraagt naar de prijs.")


class FakeReplier:
    """Schrijft een vast concept; bij een gerichte aanpassing vervangt hij alleen het prijsbedrag."""

    def __init__(self, body="Een herenknipbeurt is € 25,00.", revise_error=False):
        self.body = body
        self.revise_error = revise_error
        self.revisions = 0

    def draft(self, msg, cls, prices):
        return Draft(body=self.body, needs_human=False, reason="")

    def revise(self, msg, current, prices):
        self.revisions += 1
        if self.revise_error:
            raise RevisionError("niet uniek")
        body = apply_edits(current, [{"old": "€ 25,00", "new": "€ 15,00"}])
        return Revision(Draft(body=body, needs_human=False, reason=""), "Prijs van de kinderknipbeurt toegevoegd.")


class FakeConnector:
    def __init__(self, name="mail:zakelijk", fail=False):
        self.name = name
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def fetch_new(self):
        return []

    def send(self, msg, body):
        if self.fail:
            raise ConnectionError("smtp down")
        self.sent.append((msg.external_id, body))


def _msg(ext="<1@x>", sender="Jan Jansen <jan@example.com>", thread=None, channel="mail:zakelijk", body="Wat kost knippen?", minutes_ago=0):
    return Message(
        channel=channel,
        external_id=ext,
        thread_id=thread or ext,
        sender=sender,
        subject="Prijs?",
        body=body,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def setup(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    connector = FakeConnector()
    replier = FakeReplier()
    prices = PriceBook(lambda: load_yaml(EXAMPLE))
    prices.refresh()
    pipeline = Pipeline(
        store, FakeClassifier(), replier, prices,
        PolicyConfig(autosend_enabled=False), RuleConfig(), {"mail:zakelijk": connector, "whatsapp": connector},
    )
    yield store, pipeline, connector, replier
    store.close()


def _pending_id(store):
    return store.pending_drafts()[0]["id"]


# ---------- klanten ----------

def test_same_email_address_becomes_one_client(setup):
    store, pipeline, _, _ = setup
    pipeline.process(_msg("<a@x>", sender="Jan <jan@example.com>", thread="<a@x>"))
    pipeline.process(_msg("<b@x>", sender="Jan J. <JAN@example.com>", thread="<b@x>"))
    clients = store.list_clients()
    assert len(clients) == 1
    assert clients[0]["message_count"] == 2


def test_whatsapp_and_mail_share_a_client_when_identifier_matches(setup):
    store, pipeline, _, _ = setup
    pipeline.process(_msg("<a@x>", sender="Lisa <lisa@example.com>", thread="<a@x>"))
    pipeline.process(_msg("w1", sender="Lisa <+31 6 1111 2222>", thread="31611112222", channel="whatsapp"))
    pipeline.process(_msg("w2", sender="Lisa <31611112222>", thread="31611112222", channel="whatsapp"))
    assert len(store.list_clients()) == 2  # e-mail en telefoon zijn nog niet gekoppeld
    phone_client = store.find_client_by_identifier("phone", "31611112222")
    assert len(store.list_messages(client_id=phone_client)) == 2


def test_merge_moves_messages_and_identifiers(setup):
    store, pipeline, _, _ = setup
    pipeline.process(_msg("<a@x>", sender="Jan <jan@example.com>", thread="<a@x>"))
    pipeline.process(_msg("<b@x>", sender="Jan <jan@zaak.nl>", thread="<b@x>"))
    first = store.find_client_by_identifier("email", "jan@example.com")
    second = store.find_client_by_identifier("email", "jan@zaak.nl")
    store.merge_clients(second, first)
    assert store.find_client_by_identifier("email", "jan@zaak.nl") == first
    assert len(store.list_messages(client_id=first)) == 2
    assert len(store.list_clients()) == 1


# ---------- gerichte aanpassing van een concept ----------

def test_follow_up_in_same_thread_revises_only_the_changed_part(setup):
    store, pipeline, connector, replier = setup
    pipeline.process(_msg("<a@x>", thread="<a@x>"))
    original_id = _pending_id(store)
    pipeline.process(_msg("<b@x>", thread="<a@x>", body="En voor een kind?"))
    assert replier.revisions == 1
    current = store.pending_drafts()
    assert len(current) == 1
    row = store.get_draft(current[0]["id"])
    assert row["body"] == "Een herenknipbeurt is € 15,00."
    assert row["revises"] == original_id
    assert row["change_summary"] == "Prijs van de kinderknipbeurt toegevoegd."
    assert store.get_draft(original_id)["status"] == "vervangen"


def test_failed_partial_edit_falls_back_to_new_draft(setup):
    store, pipeline, _, replier = setup
    replier.revise_error = True
    pipeline.process(_msg("<a@x>", thread="<a@x>"))
    pipeline.process(_msg("<b@x>", thread="<a@x>", body="Ook voor een kind?"))
    row = store.get_draft(_pending_id(store))
    assert row["body"] == "Een herenknipbeurt is € 25,00."
    assert row["revises"] is not None and row["change_summary"] == ""


def test_apply_edits_requires_unique_old_text():
    assert apply_edits("Kost 25 euro. Groet", [{"old": "25 euro", "new": "€ 15,00"}]) == "Kost € 15,00. Groet"
    with pytest.raises(RevisionError):
        apply_edits("a a", [{"old": "a", "new": "b"}])
    with pytest.raises(RevisionError):
        apply_edits("abc", [{"old": "xyz", "new": "b"}])


# ---------- inplannen ----------

def test_approve_with_future_time_schedules_instead_of_sending(setup):
    store, pipeline, connector, _ = setup
    pipeline.process(_msg("<a@x>"))
    draft_id = _pending_id(store)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    pipeline.approve(draft_id, send_at=later)
    assert connector.sent == []
    assert store.get_draft(draft_id)["status"] == SCHEDULED
    assert store.scheduled_count() == 1


def test_scheduled_message_is_sent_when_due(setup):
    store, pipeline, connector, _ = setup
    pipeline.process(_msg("<a@x>"))
    draft_id = _pending_id(store)
    pipeline.approve(draft_id, send_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert pipeline.send_due() == 0
    assert pipeline.send_due(now=datetime.now(timezone.utc) + timedelta(hours=2)) == 1
    assert connector.sent == [("<a@x>", "Een herenknipbeurt is € 25,00.")]
    assert store.get_draft(draft_id)["status"] == "verzonden"


def test_cancel_schedule_returns_draft_to_queue(setup):
    store, pipeline, connector, _ = setup
    pipeline.process(_msg("<a@x>"))
    draft_id = _pending_id(store)
    pipeline.approve(draft_id, send_at=datetime.now(timezone.utc) + timedelta(hours=1))
    pipeline.cancel_schedule(draft_id)
    assert store.get_draft(draft_id)["status"] == WAITING
    assert pipeline.send_due(now=datetime.now(timezone.utc) + timedelta(days=1)) == 0


def test_failed_scheduled_send_goes_back_to_queue(setup):
    store, pipeline, connector, _ = setup
    pipeline.process(_msg("<a@x>"))
    draft_id = _pending_id(store)
    pipeline.approve(draft_id, send_at=datetime.now(timezone.utc) + timedelta(hours=1))
    connector.fail = True
    assert pipeline.send_due(now=datetime.now(timezone.utc) + timedelta(hours=2)) == 0
    row = store.get_draft(draft_id)
    assert row["status"] == WAITING and row["scheduled_for"] is None


def test_sending_now_when_time_is_in_the_past(setup):
    store, pipeline, connector, _ = setup
    pipeline.process(_msg("<a@x>"))
    pipeline.approve(_pending_id(store), send_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert len(connector.sent) == 1


# ---------- instellingen en geheimen ----------

def _runtime(tmp_path, env=None):
    store = Store(str(tmp_path / "w.db"))
    return Runtime(store, tmp_path, env or {}), store


def test_settings_are_saved_and_applied_without_restart(tmp_path):
    runtime, store = _runtime(tmp_path)
    runtime.reconfigure()
    assert runtime.pipeline._policy.autosend_enabled is False
    runtime.update(values={"WERKER_AUTOSEND": "true", "WERKER_AUTOSEND_INTENTS": "prijsvraag,afspraak"})
    assert runtime.pipeline._policy.autosend_enabled is True
    assert runtime.pipeline._policy.autosend_intents == {Intent.PRIJSVRAAG, Intent.AFSPRAAK}
    store.close()


def test_unknown_setting_is_rejected(tmp_path):
    runtime, store = _runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.update(values={"WERKER_ONBEKEND": "x"})
    store.close()


def test_secrets_are_stored_with_owner_only_permissions_and_never_returned(tmp_path):
    runtime, store = _runtime(tmp_path)
    runtime.update(secrets={"ANTHROPIC_API_KEY": "sk-geheim"})
    path = tmp_path / "secrets.json"
    assert json.loads(path.read_text())["ANTHROPIC_API_KEY"] == "sk-geheim"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    view = runtime.settings_view()
    assert view["secrets_set"]["ANTHROPIC_API_KEY"] is True
    assert "sk-geheim" not in json.dumps(view)
    store.close()


def test_unknown_secret_is_rejected(tmp_path):
    runtime, store = _runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.update(secrets={"ANDERE_SLEUTEL": "x"})
    store.close()


def test_mailbox_password_goes_to_secrets_and_is_hidden(tmp_path):
    runtime, store = _runtime(tmp_path)
    runtime.update(
        mailboxes=[
            {
                "name": "Zakelijk",
                "host": "imap.voorbeeld.nl",
                "user": "info@voorbeeld.nl",
                "smtp_host": "smtp.voorbeeld.nl",
                "sender": "info@voorbeeld.nl",
                "password": "mijn-wachtwoord",
            }
        ]
    )
    key = mailbox_secret_key("zakelijk")
    assert json.loads((tmp_path / "secrets.json").read_text())[key] == "mijn-wachtwoord"
    stored = json.loads(store.get_settings()["WERKER_MAILBOXES"])
    assert stored[0]["password_env"] == key and "password" not in stored[0]
    view = runtime.settings_view()
    assert view["mailboxes"][0]["password_set"] is True
    assert "mijn-wachtwoord" not in json.dumps(view)
    assert runtime.pipeline is not None
    assert "mail:zakelijk" in runtime.pipeline._connectors
    store.close()


def test_process_environment_is_overridden_by_settings(tmp_path):
    runtime, store = _runtime(tmp_path, env={"WERKER_AUTOSEND": "false"})
    store.set_settings({"WERKER_AUTOSEND": "true"})
    assert runtime.env()["WERKER_AUTOSEND"] == "true"
    store.close()


def test_app_starts_without_api_key_and_explains_why(tmp_path):
    runtime, store = _runtime(tmp_path)
    runtime.reconfigure()
    with pytest.raises(RuntimeError, match="API-sleutel"):
        runtime.pipeline._classifier.classify(_msg())
    store.close()


# ---------- regressies uit de review ----------

def test_approving_twice_sends_once(setup):
    store, pipeline, connector, _ = setup
    pipeline.process(_msg("<a@x>"))
    draft_id = _pending_id(store)
    pipeline.approve(draft_id)
    with pytest.raises(LookupError):
        pipeline.approve(draft_id)
    assert len(connector.sent) == 1


def test_sent_draft_cannot_be_rejected(setup):
    store, pipeline, _, _ = setup
    pipeline.process(_msg("<a@x>"))
    draft_id = _pending_id(store)
    pipeline.approve(draft_id)
    with pytest.raises(ValueError):
        pipeline.reject(draft_id)


def test_failed_message_is_retried_on_next_process(setup):
    store, pipeline, connector, replier = setup
    original_draft = replier.draft

    def broken_draft(msg, cls, prices):
        raise ConnectionError("API tijdelijk weg")

    replier.draft = broken_draft
    with pytest.raises(ConnectionError):
        pipeline.process(_msg("<a@x>"))
    replier.draft = original_draft
    assert pipeline.process(_msg("<a@x>")) is not None
    assert store.pending_count() == 1


def test_edits_apply_to_original_text_and_may_not_overlap():
    original = "Prijs is €10."
    edits = [{"old": "€10", "new": "€10 excl. btw"}, {"old": "€10", "new": "€12"}]
    with pytest.raises(RevisionError):
        apply_edits(original, edits)
    assert apply_edits(original, [{"old": "€10", "new": "€12"}]) == "Prijs is €12."
    with pytest.raises(RevisionError):
        apply_edits(original, [])


def test_mailbox_keys_do_not_collide():
    assert mailbox_secret_key("info.werk") != mailbox_secret_key("info_werk")


def test_mailbox_without_password_is_skipped_not_fatal(tmp_path):
    import json as _json

    from werker.cli import mailbox_connectors

    entry = {"name": "zaak", "host": "h", "user": "u", "password_env": "MAILBOX_ZAAK_X_PW",
             "smtp_host": "s", "sender": "a@b.nl"}
    assert mailbox_connectors({"WERKER_MAILBOXES": _json.dumps([entry])}) == []


def test_invalid_number_setting_is_refused_and_nothing_is_saved(tmp_path):
    runtime, store = _runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.update(values={"WERKER_AUTOSEND": "true", "WERKER_MIN_CONFIDENCE": "abc"})
    assert "WERKER_AUTOSEND" not in store.get_settings()
    store.close()


def test_theme_defaults_and_accepts_only_known_themes(tmp_path):
    runtime, store = _runtime(tmp_path)
    assert runtime.settings_view()["values"]["WERKER_THEME"] == "editorieel"
    runtime.update(values={"WERKER_THEME": "zacht"})
    assert runtime.settings_view()["values"]["WERKER_THEME"] == "zacht"
    with pytest.raises(ValueError):
        runtime.update(values={"WERKER_THEME": "neon"})
    store.close()
