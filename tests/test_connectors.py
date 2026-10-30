import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from werker.connectors.mail import IMAPConnector, build_new_message, build_reply, parse_email
from werker.connectors.whatsapp import WhatsAppConnector, WindowClosedError, parse_webhook, verify_signature
from werker.models import Message


def _raw_mail(body: str, headers: str = "") -> bytes:
    return (
        "From: Jan Jansen <jan@example.com>\r\n"
        "To: info@salon.nl\r\n"
        "Subject: Wat kost knippen?\r\n"
        "Message-ID: <abc@example.com>\r\n"
        "Date: Tue, 27 Oct 2026 10:00:00 +0100\r\n"
        f"{headers}"
        "\r\n"
        f"{body}\r\n"
    ).encode()


def test_parse_email_extracts_fields_and_threading():
    raw = _raw_mail("Hoeveel kost een herenknipbeurt?", headers="References: <root@example.com>\r\n")
    msg = parse_email(raw)
    assert msg.channel == "mail"
    assert msg.external_id == "<abc@example.com>"
    assert msg.thread_id == "<root@example.com>"
    assert msg.sender == "Jan Jansen <jan@example.com>"
    assert msg.subject == "Wat kost knippen?"
    assert msg.body == "Hoeveel kost een herenknipbeurt?"
    assert msg.received_at.year == 2026


def test_parse_email_drops_quoted_previous_message():
    body = "Wat kost kleuren?\n\nOp di 27 okt schreef Salon <info@salon.nl>:\n> Ons tarief is 65,00"
    assert parse_email(_raw_mail(body)).body == "Wat kost kleuren?"


def test_parse_email_without_message_id_gets_stable_id():
    raw = _raw_mail("Hallo").replace(b"Message-ID: <abc@example.com>\r\n", b"")
    assert parse_email(raw).external_id == hashlib.sha256(raw).hexdigest()


def test_build_reply_threads_correctly():
    original = parse_email(_raw_mail("Wat kost knippen?"))
    reply = build_reply(original, "Dat is € 25,00.", sender="info@salon.nl")
    assert reply["To"] == "jan@example.com"
    assert reply["Subject"] == "Re: Wat kost knippen?"
    assert reply["In-Reply-To"] == "<abc@example.com>"
    assert "<abc@example.com>" in reply["References"]


def test_build_new_message_is_not_a_reply():
    message = build_new_message("Klant <klant@x.nl>", "Betalingsherinnering factuur 2026-001", "Graag betalen.", sender="info@salon.nl")
    assert message["To"] == "klant@x.nl"
    assert message["From"] == "info@salon.nl"
    assert message["Subject"] == "Betalingsherinnering factuur 2026-001"
    assert message["In-Reply-To"] is None
    assert message["References"] is None
    assert message.get_content().strip() == "Graag betalen."


def test_imap_send_new_sends_a_fresh_mail(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr("werker.connectors.mail.smtplib.SMTP_SSL", FakeSMTP)
    connector = IMAPConnector("imap.x", "u", "pw", "smtp.x", "info@salon.nl")
    connector.send_new("klant@x.nl", "Betalingsherinnering factuur 1", "Graag betalen.")

    [message] = sent
    assert message["Subject"] == "Betalingsherinnering factuur 1"
    assert not message["Subject"].lower().startswith("re:")
    assert message["In-Reply-To"] is None


def test_verify_signature_accepts_valid_and_rejects_tampered():
    secret, body = "geheim", b'{"entry": []}'
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, good)
    assert not verify_signature(secret, b'{"entry": [1]}', good)
    assert not verify_signature(secret, body, "")


def _webhook(text: str, timestamp: int) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "31612345678", "profile": {"name": "Sara"}}],
                            "messages": [
                                {
                                    "from": "31612345678",
                                    "id": "wamid.1",
                                    "timestamp": str(timestamp),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def test_parse_webhook_reads_text_messages():
    now = int(datetime.now(timezone.utc).timestamp())
    [msg] = parse_webhook(_webhook("Wat kost een kinderknipbeurt?", now))
    assert msg.channel == "whatsapp"
    assert msg.thread_id == "31612345678"
    assert msg.sender == "Sara <31612345678>"
    assert msg.body == "Wat kost een kinderknipbeurt?"


def test_parse_webhook_ignores_status_updates():
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert parse_webhook(payload) == []


def _whatsapp_message(received_at: datetime) -> Message:
    return Message(
        channel="whatsapp",
        external_id="wamid.1",
        thread_id="31612345678",
        sender="Sara <31612345678>",
        subject="",
        body="Prijs?",
        received_at=received_at,
    )


def test_whatsapp_send_posts_text_to_graph_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"messages": [{"id": "wamid.2"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = WhatsAppConnector("12345", "token", client=client)
    connector.send(_whatsapp_message(datetime.now(timezone.utc)), "Dat is € 25,00.")
    assert seen["url"].endswith("/12345/messages")
    assert '"to":"31612345678"' in seen["json"]


def test_whatsapp_send_refuses_outside_24_hour_window():
    connector = WhatsAppConnector("12345", "token", client=httpx.Client(transport=httpx.MockTransport(lambda r: None)))
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    with pytest.raises(WindowClosedError):
        connector.send(_whatsapp_message(old), "Hallo")
