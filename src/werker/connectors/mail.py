from __future__ import annotations

import hashlib
import html
import imaplib
import re
import smtplib
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from werker.models import Message

CHANNEL = "mail"
_QUOTE_START = re.compile(r"^(Op .+ schreef.*|On .+ wrote.*|-{2,} ?Original Message ?-{2,})$")


def parse_email(raw: bytes, channel: str = CHANNEL) -> Message:
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    message_id = str(parsed["Message-ID"] or "").strip() or hashlib.sha256(raw).hexdigest()
    references = str(parsed["References"] or "").split() + str(parsed["In-Reply-To"] or "").split()
    headers = {key.lower(): str(value) for key, value in parsed.items()}
    return Message(
        channel=channel,
        external_id=message_id,
        thread_id=references[0] if references else message_id,
        sender=str(parsed["From"] or ""),
        subject=str(parsed["Subject"] or ""),
        body=_strip_quotes(_body_text(parsed)),
        received_at=_received_at(parsed["Date"]),
        headers=headers,
    )


def _body_text(parsed: EmailMessage) -> str:
    plain = parsed.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain.get_content()
    richtext = parsed.get_body(preferencelist=("html",))
    if richtext is not None:
        stripped = re.sub(r"<[^>]+>", " ", richtext.get_content())
        return html.unescape(re.sub(r"\s+", " ", stripped)).strip()
    return ""


def _strip_quotes(text: str) -> str:
    kept = []
    for line in text.splitlines():
        if _QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def _received_at(value: str | None) -> datetime:
    if value:
        try:
            return parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def build_reply(msg: Message, body: str, sender: str) -> EmailMessage:
    reply = EmailMessage()
    reply["From"] = sender
    reply["To"] = parseaddr(msg.sender)[1] or msg.sender
    reply["Subject"] = msg.subject if msg.subject.lower().startswith("re:") else f"Re: {msg.subject}"
    reply["In-Reply-To"] = msg.external_id
    reply["References"] = " ".join(dict.fromkeys([msg.thread_id, msg.external_id]))
    reply.set_content(body)
    return reply


class IMAPConnector:
    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        smtp_host: str,
        sender_address: str,
        name: str = CHANNEL,
        port: int = 993,
        smtp_port: int = 465,
        folder: str = "INBOX",
    ):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._sender = sender_address
        self._folder = folder
        self.name = name

    def fetch_new(self) -> list[Message]:
        messages = []
        with imaplib.IMAP4_SSL(self._host, self._port) as imap:
            imap.login(self._user, self._password)
            # readonly + BODY.PEEK laat de berichten ongelezen, zodat de mailbox ongewijzigd blijft.
            imap.select(self._folder, readonly=True)
            _, data = imap.search(None, "UNSEEN")
            for number in data[0].split():
                _, parts = imap.fetch(number, "(BODY.PEEK[])")
                messages.append(parse_email(parts[0][1], channel=self.name))
        return messages

    def send(self, msg: Message, body: str) -> None:
        reply = build_reply(msg, body, self._sender)
        with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port) as smtp:
            smtp.login(self._user, self._password)
            smtp.send_message(reply)
