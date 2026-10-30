import base64
from datetime import datetime, timezone
from pathlib import Path

from werker.connectors.gmail import GMAIL_ID_HEADER, GMAIL_LABELS, GmailConnector, intent_labeler
from werker.models import Classification, Draft, Intent, Message
from werker.pipeline import Pipeline
from werker.policy import PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.rules import RuleConfig
from werker.store import Store

EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"


class FakeGmail:
    """Neppe Gmail-service: onthoudt elke aanroep en geeft vaste antwoorden terug."""

    def __init__(self, labels=None, messages=None):
        self.labels = dict(labels or {})  # naam -> id
        self.messages = dict(messages or {})  # gmail-id -> rauwe mail (bytes)
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 1

    def users(self):
        return _User(self)

    def list_labels(self, userId):
        return {"labels": [{"id": i, "name": n} for n, i in self.labels.items()]}

    def create_label(self, userId, body):
        new_id = f"Label_{self._next_id}"
        self._next_id += 1
        self.labels[body["name"]] = new_id
        return {"id": new_id, "name": body["name"]}

    def list_messages(self, userId, q, maxResults):
        return {"messages": [{"id": gid} for gid in self.messages]}

    def get_message(self, userId, id, format):
        return {"raw": base64.urlsafe_b64encode(self.messages[id]).decode("ascii")}

    def modify(self, userId, id, body):
        return {"id": id}

    def send_message(self, userId, body):
        return {"id": "sent-1"}


class _User:
    def __init__(self, fake):
        self._fake = fake

    def labels(self):
        return _Resource(self._fake, "labels", {"list": "list_labels", "create": "create_label"})

    def messages(self):
        return _Resource(
            self._fake,
            "messages",
            {"list": "list_messages", "get": "get_message", "modify": "modify", "send": "send_message"},
        )


class _Resource:
    def __init__(self, fake, name, methods):
        self._fake = fake
        self._name = name
        self._methods = methods

    def __getattr__(self, method):
        if method not in self._methods:
            raise AttributeError(method)
        return lambda **kwargs: _Pending(self._fake, f"{self._name}.{method}", getattr(self._fake, self._methods[method]), kwargs)


class _Pending:
    def __init__(self, fake, name, run, kwargs):
        self._fake, self._name, self._run, self._kwargs = fake, name, run, kwargs

    def execute(self):
        self._fake.calls.append((self._name, self._kwargs))
        return self._run(**self._kwargs)


def _gmail_msg(gmail_id="gm-1", external_id="<1@x>") -> Message:
    return Message(
        channel="mail:gmail",
        external_id=external_id,
        thread_id=external_id,
        sender="klant@example.com",
        subject="Prijs?",
        body="Wat kost een herenknipbeurt?",
        received_at=datetime.now(timezone.utc),
        headers={GMAIL_ID_HEADER: gmail_id},
    )


def _calls(fake, name):
    return [kwargs for called, kwargs in fake.calls if called == name]


def test_label_is_created_when_missing_and_applied_to_the_message():
    fake = FakeGmail()
    connector = GmailConnector(fake, sender_address="info@salon.nl")

    connector.label(_gmail_msg("gm-1"), "Werker/Prijsvraag")

    created = _calls(fake, "labels.create")
    assert len(created) == 1
    assert created[0]["body"]["name"] == "Werker/Prijsvraag"
    assert _calls(fake, "messages.modify") == [
        {"userId": "me", "id": "gm-1", "body": {"addLabelIds": [fake.labels["Werker/Prijsvraag"]]}}
    ]


def test_existing_label_is_reused_and_ids_are_cached():
    fake = FakeGmail(labels={"Werker/Klacht": "Label_42"})
    connector = GmailConnector(fake, sender_address="info@salon.nl")

    connector.label(_gmail_msg("gm-1"), "Werker/Klacht")
    connector.label(_gmail_msg("gm-2"), "Werker/Klacht")

    assert _calls(fake, "labels.create") == []
    assert len(_calls(fake, "labels.list")) == 1  # de tweede keer staat het id al in het geheugen
    assert [c["body"]["addLabelIds"] for c in _calls(fake, "messages.modify")] == [["Label_42"], ["Label_42"]]


def test_other_channels_are_skipped_silently():
    fake = FakeGmail()
    connector = GmailConnector(fake, sender_address="info@salon.nl")
    imap_msg = Message(
        channel="mail:imap",
        external_id="<2@x>",
        thread_id="<2@x>",
        sender="a@b.nl",
        subject="",
        body="",
        received_at=datetime.now(timezone.utc),
        headers={GMAIL_ID_HEADER: "gm-9"},
    )

    connector.label(imap_msg, "Werker/Overig")

    assert fake.calls == []


def test_fetch_new_keeps_the_gmail_id_so_the_label_can_be_set():
    raw = b"From: Jan <jan@example.com>\r\nSubject: Hallo\r\nMessage-ID: <abc@example.com>\r\n\r\nWat kost het?\r\n"
    fake = FakeGmail(messages={"gm-77": raw})
    connector = GmailConnector(fake, sender_address="info@salon.nl")

    (msg,) = connector.fetch_new()
    connector.label(msg, "Werker/Overig")

    assert msg.external_id == "<abc@example.com>"
    assert _calls(fake, "messages.modify")[0]["id"] == "gm-77"


def test_intent_labeler_maps_every_intent_to_its_dutch_label():
    fake = FakeGmail()
    labeler = intent_labeler(GmailConnector(fake, sender_address="info@salon.nl"))

    for intent in Intent:
        labeler(_gmail_msg(f"gm-{intent.value}"), intent)

    assert set(GMAIL_LABELS) == set(Intent)
    assert {c["body"]["name"] for c in _calls(fake, "labels.create")} == set(GMAIL_LABELS.values())
    assert "Werker/Nieuwsbrief" in GMAIL_LABELS.values()


class _Classifier:
    def __init__(self, intent):
        self.intent = intent

    def classify(self, msg):
        return Classification(self.intent, "normaal", "nl", 0.95, "test")


class _Replier:
    def draft(self, msg, cls, prices):
        return Draft(body="Dat is € 25,00.", needs_human=False, reason="")


class _Connector:
    name = "mail:gmail"

    def fetch_new(self):
        return []

    def send(self, msg, body):
        raise AssertionError("niet verzenden in deze test")


def _pipeline(tmp_path, intent, labeler):
    store = Store(str(tmp_path / "w.db"))
    pipeline = Pipeline(
        store,
        _Classifier(intent),
        _Replier(),
        PriceBook(lambda: load_yaml(EXAMPLE)),
        PolicyConfig(),
        RuleConfig(),
        {"mail:gmail": _Connector()},
        labeler=labeler,
    )
    pipeline.prices.refresh()
    return store, pipeline


def test_pipeline_calls_labeler_with_message_and_intent(tmp_path):
    seen = []
    _, pipeline = _pipeline(tmp_path, Intent.KLACHT, lambda msg, intent: seen.append((msg.external_id, intent)))

    pipeline.process(_gmail_msg("gm-5", external_id="<5@x>"))

    assert seen == [("<5@x>", Intent.KLACHT)]


def test_labeler_failure_does_not_block_processing(tmp_path):
    def broken(msg, intent):
        raise RuntimeError("Gmail even niet bereikbaar")

    store, pipeline = _pipeline(tmp_path, Intent.PRIJSVRAAG, broken)

    decision = pipeline.process(_gmail_msg("gm-6", external_id="<6@x>"))

    assert decision is not None
    row = store.get_message_row(store.find_message_id("mail:gmail", "<6@x>"))
    assert row["intent"] == Intent.PRIJSVRAAG.value
    assert row["status"] == "concept klaar"


def test_send_new_sends_a_fresh_mail_without_reply_headers():
    from email import message_from_bytes

    fake = FakeGmail()
    GmailConnector(fake, sender_address="info@salon.nl").send_new("klant@x.nl", "Betalingsherinnering factuur 1", "Graag betalen.")

    [call] = _calls(fake, "messages.send")
    sent = message_from_bytes(base64.urlsafe_b64decode(call["body"]["raw"]))
    assert sent["Subject"] == "Betalingsherinnering factuur 1"
    assert sent["To"] == "klant@x.nl"
    assert sent["In-Reply-To"] is None
