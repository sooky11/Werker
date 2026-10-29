from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from werker.models import Classification, Draft, Message

SCHEMA = """
create table if not exists messages (
    id integer primary key autoincrement,
    channel text not null,
    external_id text not null,
    thread_id text not null default '',
    sender text not null,
    subject text not null default '',
    body text not null,
    received_at text not null,
    intent text,
    urgency text,
    language text,
    confidence real,
    summary text,
    status text not null default 'nieuw',
    unique (channel, external_id)
);
create table if not exists drafts (
    id integer primary key autoincrement,
    message_id integer not null references messages(id),
    body text not null,
    errors text not null default '',
    reason text not null default '',
    status text not null default 'wacht op goedkeuring',
    created_at text not null
);
create table if not exists audit (
    id integer primary key autoincrement,
    at text not null,
    message_id integer,
    event text not null,
    detail text not null default ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def find_message_id(self, channel: str, external_id: str) -> int | None:
        row = self._db.execute(
            "select id from messages where channel = ? and external_id = ?",
            (channel, external_id),
        ).fetchone()
        return row["id"] if row else None

    def add_message(self, msg: Message) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into messages
                   (channel, external_id, thread_id, sender, subject, body, received_at)
                   values (?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.channel,
                    msg.external_id,
                    msg.thread_id,
                    msg.sender,
                    msg.subject,
                    msg.body,
                    msg.received_at.isoformat(),
                ),
            )
        return cursor.lastrowid

    def get_message(self, message_id: int) -> Message:
        row = self._db.execute("select * from messages where id = ?", (message_id,)).fetchone()
        if row is None:
            raise LookupError(f"bericht {message_id} bestaat niet")
        return _row_to_message(row)

    def set_classification(self, message_id: int, cls: Classification) -> None:
        with self._db:
            self._db.execute(
                """update messages set intent = ?, urgency = ?, language = ?,
                   confidence = ?, summary = ? where id = ?""",
                (cls.intent.value, cls.urgency, cls.language, cls.confidence, cls.summary, message_id),
            )

    def set_status(self, message_id: int, status: str) -> None:
        with self._db:
            self._db.execute("update messages set status = ? where id = ?", (status, message_id))

    def add_draft(self, message_id: int, draft: Draft, errors: list[str]) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into drafts (message_id, body, errors, reason, created_at)
                   values (?, ?, ?, ?, ?)""",
                (message_id, draft.body, "\n".join(errors), draft.reason, _now()),
            )
        return cursor.lastrowid

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from drafts where id = ?", (draft_id,)).fetchone()

    def set_draft_status(self, draft_id: int, status: str, body: str | None = None) -> None:
        with self._db:
            if body is None:
                self._db.execute("update drafts set status = ? where id = ?", (status, draft_id))
            else:
                self._db.execute(
                    "update drafts set status = ?, body = ? where id = ?", (status, body, draft_id)
                )

    def pending_drafts(self) -> list[sqlite3.Row]:
        return self._db.execute(
            """select d.id, d.body, d.errors, d.reason, m.channel, m.sender, m.subject, m.summary
               from drafts d join messages m on m.id = d.message_id
               where d.status = 'wacht op goedkeuring' order by d.id"""
        ).fetchall()

    def audit(self, message_id: int | None, event: str, detail: str = "") -> None:
        with self._db:
            self._db.execute(
                "insert into audit (at, message_id, event, detail) values (?, ?, ?, ?)",
                (_now(), message_id, event, detail),
            )


def _row_to_message(row: Any) -> Message:
    return Message(
        channel=row["channel"],
        external_id=row["external_id"],
        thread_id=row["thread_id"],
        sender=row["sender"],
        subject=row["subject"],
        body=row["body"],
        received_at=datetime.fromisoformat(row["received_at"]),
    )
