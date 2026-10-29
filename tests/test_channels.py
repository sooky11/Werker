import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from werker.cli import build_connectors, mailbox_connectors
from werker.connectors import imessage
from werker.connectors.imessage import IMessageConnector, decode_attributed_body
from werker.connectors.mail import parse_email
from werker.models import Message
from werker.pipeline import Pipeline
from werker.policy import PolicyConfig
from werker.prices import PriceBook
from werker.rules import RuleConfig
from werker.store import Store


# ---------- mailboxen ----------

def test_mailboxes_come_from_json_with_passwords_in_separate_variables():
    env = {
        "WERKER_MAILBOXES": json.dumps(
            [
                {
                    "name": "zakelijk",
                    "host": "imap.voorbeeld.nl",
                    "user": "info@voorbeeld.nl",
                    "password_env": "ZAKELIJK_PW",
                    "smtp_host": "smtp.voorbeeld.nl",
                    "sender": "info@voorbeeld.nl",
                }
            ]
        ),
        "ZAKELIJK_PW": "geheim",
    }
    [connector] = mailbox_connectors(env)
    assert connector.name == "mail:zakelijk"
    assert connector._password == "geheim"


def test_each_mailbox_keeps_its_own_channel_when_connectors_are_built():
    entries = [
        {"name": "a", "host": "imap.a.nl", "user": "a", "password_env": "PW_A", "smtp_host": "smtp.a.nl", "sender": "a@a.nl"},
        {"name": "b", "host": "imap.b.nl", "user": "b", "password_env": "PW_B", "smtp_host": "smtp.b.nl", "sender": "b@b.nl"},
    ]
    env = {"WERKER_MAILBOXES": json.dumps(entries), "PW_A": "x", "PW_B": "y", "WERKER_IMESSAGE": "false"}
    connectors = build_connectors(env)
    assert set(connectors) == {"mail:a", "mail:b"}


def test_parse_email_uses_given_channel():
    raw = b"From: Jan <jan@x.nl>\r\nSubject: Hoi\r\nMessage-ID: <q@x>\r\n\r\nHallo\r\n"
    assert parse_email(raw, channel="mail:zakelijk").channel == "mail:zakelijk"


# ---------- iMessage: ontvangen ----------

def _make_chat_db(path: Path, rows: list[dict]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        create table handle (ROWID integer primary key, id text);
        create table chat (ROWID integer primary key, chat_identifier text, display_name text);
        create table message (ROWID integer primary key, guid text, text text, attributedBody blob,
                              date integer, is_from_me integer, handle_id integer);
        create table chat_message_join (chat_id integer, message_id integer);
        """
    )
    for row in rows:
        connection.execute("insert or ignore into handle (ROWID, id) values (?, ?)", (row["handle_rowid"], row["handle"]))
        connection.execute(
            "insert or ignore into chat (ROWID, chat_identifier, display_name) values (?, ?, ?)",
            (row["chat_rowid"], row["chat_identifier"], row.get("display_name")),
        )
        connection.execute(
            "insert into message (ROWID, guid, text, attributedBody, date, is_from_me, handle_id) values (?, ?, ?, ?, ?, ?, ?)",
            (row["rowid"], row["guid"], row.get("text"), row.get("attributedBody"), row["date"], row.get("is_from_me", 0), row["handle_rowid"]),
        )
        connection.execute(
            "insert into chat_message_join (chat_id, message_id) values (?, ?)", (row["chat_rowid"], row["rowid"])
        )
    connection.commit()
    connection.close()


def _apple_ns(moment: datetime) -> int:
    return int((moment - imessage.APPLE_EPOCH).total_seconds() * 1_000_000_000)


def test_fetch_reads_incoming_imessages_only(tmp_path):
    now = datetime.now(timezone.utc)
    db = tmp_path / "chat.db"
    _make_chat_db(
        db,
        [
            {"rowid": 1, "guid": "g1", "text": "Hoi, wat kost knippen?", "date": _apple_ns(now - timedelta(minutes=5)),
             "handle_rowid": 1, "handle": "+31611112222", "chat_rowid": 1, "chat_identifier": "+31611112222"},
            {"rowid": 2, "guid": "g2", "text": "Mijn eigen antwoord", "date": _apple_ns(now), "is_from_me": 1,
             "handle_rowid": 1, "handle": "+31611112222", "chat_rowid": 1, "chat_identifier": "+31611112222"},
            {"rowid": 3, "guid": "g3", "text": "Te oud", "date": _apple_ns(now - timedelta(days=10)),
             "handle_rowid": 2, "handle": "oud@x.nl", "chat_rowid": 2, "chat_identifier": "oud@x.nl"},
        ],
    )
    [msg] = IMessageConnector(db_path=db).fetch_new()
    assert msg.channel == "imessage"
    assert msg.external_id == "g1"
    assert msg.body == "Hoi, wat kost knippen?"
    assert msg.thread_id == "+31611112222"
    assert msg.sender == "+31611112222"


def test_fetch_uses_attributed_body_when_text_is_empty(tmp_path):
    now = datetime.now(timezone.utc)
    blob = b"\x04\x0bstreamtyped\x81\xe8\x03NSString\x01+" + bytes([14]) + b"Kost dat iets?" + b"\x86\x84"
    db = tmp_path / "chat.db"
    _make_chat_db(
        db,
        [{"rowid": 1, "guid": "g9", "text": None, "attributedBody": blob, "date": _apple_ns(now),
          "handle_rowid": 1, "handle": "+31600000000", "chat_rowid": 1, "chat_identifier": "+31600000000"}],
    )
    [msg] = IMessageConnector(db_path=db).fetch_new()
    assert msg.body == "Kost dat iets?"


def test_missing_database_returns_no_messages(tmp_path):
    assert IMessageConnector(db_path=tmp_path / "nope.db").fetch_new() == []


def test_decode_attributed_body_handles_long_text():
    text = "a" * 200
    encoded = text.encode()
    blob = b"NSString\x01+" + b"\x81" + len(encoded).to_bytes(2, "little") + encoded
    assert decode_attributed_body(blob) == text


def test_decode_attributed_body_returns_empty_for_unknown_data():
    assert decode_attributed_body(b"geen tekst hier") == ""
    assert decode_attributed_body(None) == ""


# ---------- iMessage: verzenden ----------

def test_send_refuses_on_non_mac(monkeypatch):
    monkeypatch.setattr(imessage.sys, "platform", "linux")
    connector = IMessageConnector(db_path=Path("/nonexistent"))
    msg = Message("imessage", "g", "+31611112222", "+31611112222", "", "hoi", datetime.now(timezone.utc))
    with pytest.raises(RuntimeError):
        connector.send(msg, "antwoord")


def test_send_calls_messages_app_with_recipient_and_text(monkeypatch):
    calls = {}
    monkeypatch.setattr(imessage.sys, "platform", "darwin")

    def fake_run(args, **kwargs):
        calls["args"] = args
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(imessage.subprocess, "run", fake_run)
    msg = Message("imessage", "g", "+31611112222", "+31611112222", "", "hoi", datetime.now(timezone.utc))
    IMessageConnector(db_path=Path("/nonexistent")).send(msg, "Dat is € 25,00.")
    assert calls["args"][0] == "osascript"
    assert calls["args"][-2:] == ["+31611112222", "Dat is € 25,00."]


def test_group_replies_are_refused(monkeypatch):
    monkeypatch.setattr(imessage.sys, "platform", "darwin")
    msg = Message("imessage", "g", "chat123", "groep", "", "hoi", datetime.now(timezone.utc))
    with pytest.raises(RuntimeError, match="groepsgesprekken"):
        IMessageConnector(db_path=Path("/nonexistent")).send(msg, "antwoord")


# ---------- dubbele verwerking ----------

def test_concurrent_duplicate_is_not_an_error(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    prices = PriceBook(lambda: [])

    class Nothing:
        def classify(self, msg):
            raise AssertionError("mag niet worden aangeroepen")

    pipeline = Pipeline(store, Nothing(), Nothing(), prices, PolicyConfig(), RuleConfig(), {})
    msg = Message("mail", "<dup@x>", "<dup@x>", "klant@x.nl", "Hoi", "tekst", datetime.now(timezone.utc))
    original_find = store.find_message_id
    # Simuleer dat een andere thread het bericht toevoegt nadat deze thread heeft gecontroleerd.
    store.add_message(msg)
    store.find_message_id = lambda channel, external_id: None
    try:
        assert pipeline.process(msg) is None
    finally:
        store.find_message_id = original_find
        store.close()
