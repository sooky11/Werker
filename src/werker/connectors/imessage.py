"""iMessage via de lokale berichtendatabase van macOS.

Alleen beschikbaar op een Mac. Ontvangen berichten worden alleen-lezen uit
~/Library/Messages/chat.db gehaald; dat vraagt om "Volledige schijftoegang" voor de app.
Verzenden gaat via het Berichten-programma (AppleScript); de eerste keer vraagt macOS om toestemming.

Groepsgesprekken worden ingelezen, maar een antwoord erop wordt nog niet ondersteund.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werker.models import Message

CHANNEL = "imessage"
DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
LOOKBACK = timedelta(days=2)

SEND_SCRIPT = """
on run argv
    set recipientId to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set theBuddy to buddy recipientId of targetService
        send messageText to theBuddy
    end tell
end run
"""


def available(db_path: Path = DEFAULT_DB) -> bool:
    return sys.platform == "darwin" and db_path.exists()


def decode_attributed_body(blob: bytes | None) -> str:
    """Haal de tekst uit het attributedBody-veld (Apple typedstream), voor berichten zonder text-veld."""
    if not blob:
        return ""
    start = blob.find(b"NSString")
    if start == -1:
        return ""
    marker = blob.find(b"\x01+", start)
    if marker == -1:
        return ""
    pos = marker + 2
    length = blob[pos]
    pos += 1
    if length == 0x81:
        length = int.from_bytes(blob[pos : pos + 2], "little")
        pos += 2
    elif length == 0x82:
        length = int.from_bytes(blob[pos : pos + 4], "little")
        pos += 4
    return blob[pos : pos + length].decode("utf-8", errors="replace")


def _apple_date(value: int | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    # Moderne macOS-versies slaan nanoseconden op; oudere seconden.
    seconds = value / 1_000_000_000 if abs(value) > 1_000_000_000_000 else value
    return APPLE_EPOCH + timedelta(seconds=seconds)


def _apple_threshold(moment: datetime) -> int:
    return int((moment - APPLE_EPOCH).total_seconds() * 1_000_000_000)


class IMessageConnector:
    def __init__(self, db_path: Path = DEFAULT_DB, name: str = CHANNEL, lookback: timedelta = LOOKBACK):
        self.name = name
        self._db_path = db_path
        self._lookback = lookback

    def fetch_new(self) -> list[Message]:
        if not self._db_path.exists():
            return []
        since = _apple_threshold(datetime.now(timezone.utc) - self._lookback)
        # read-only: de Berichten-app blijft de database beheren.
        connection = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """select m.ROWID, m.guid, m.text, m.attributedBody, m.date,
                          h.id as handle, c.chat_identifier, c.display_name
                   from message m
                   left join handle h on h.ROWID = m.handle_id
                   left join chat_message_join cmj on cmj.message_id = m.ROWID
                   left join chat c on c.ROWID = cmj.chat_id
                   where m.is_from_me = 0 and m.date >= ?
                   order by m.date""",
                (since,),
            ).fetchall()
        finally:
            connection.close()

        messages = []
        for row in rows:
            body = row["text"] or decode_attributed_body(row["attributedBody"])
            if not body:
                continue
            handle = row["handle"] or "onbekend"
            is_group = bool(row["chat_identifier"]) and str(row["chat_identifier"]).startswith("chat")
            sender = f"{row['display_name'] or handle} <{handle}>" if is_group else f"{handle}"
            messages.append(
                Message(
                    channel=self.name,
                    external_id=row["guid"],
                    thread_id=row["chat_identifier"] or handle,
                    sender=sender,
                    subject="",
                    body=body,
                    received_at=_apple_date(row["date"]),
                )
            )
        return messages

    def send(self, msg: Message, body: str) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("iMessage verzenden kan alleen op een Mac")
        if str(msg.thread_id).startswith("chat"):
            raise RuntimeError("antwoorden in groepsgesprekken worden nog niet ondersteund")
        recipient = msg.thread_id
        subprocess.run(
            ["osascript", "-e", SEND_SCRIPT, recipient, body],
            check=True,
            timeout=30,
            capture_output=True,
        )
