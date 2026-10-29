from __future__ import annotations

import functools
import sqlite3
import threading
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


def _locked(method):
    """FastAPI en de achtergrondtaken gebruiken dezelfde verbinding vanuit verschillende threads."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @_locked
    def find_message_id(self, channel: str, external_id: str) -> int | None:
        row = self._db.execute(
            "select id from messages where channel = ? and external_id = ?",
            (channel, external_id),
        ).fetchone()
        return row["id"] if row else None

    @_locked
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

    @_locked
    def get_message(self, message_id: int) -> Message:
        row = self._db.execute("select * from messages where id = ?", (message_id,)).fetchone()
        if row is None:
            raise LookupError(f"bericht {message_id} bestaat niet")
        return _row_to_message(row)

    @_locked
    def set_classification(self, message_id: int, cls: Classification) -> None:
        with self._db:
            self._db.execute(
                """update messages set intent = ?, urgency = ?, language = ?,
                   confidence = ?, summary = ? where id = ?""",
                (cls.intent.value, cls.urgency, cls.language, cls.confidence, cls.summary, message_id),
            )

    @_locked
    def set_status(self, message_id: int, status: str) -> None:
        with self._db:
            self._db.execute("update messages set status = ? where id = ?", (status, message_id))

    @_locked
    def add_draft(self, message_id: int, draft: Draft, errors: list[str]) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into drafts (message_id, body, errors, reason, created_at)
                   values (?, ?, ?, ?, ?)""",
                (message_id, draft.body, "\n".join(errors), draft.reason, _now()),
            )
        return cursor.lastrowid

    @_locked
    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from drafts where id = ?", (draft_id,)).fetchone()

    @_locked
    def set_draft_status(self, draft_id: int, status: str, body: str | None = None) -> None:
        with self._db:
            if body is None:
                self._db.execute("update drafts set status = ? where id = ?", (status, draft_id))
            else:
                self._db.execute(
                    "update drafts set status = ?, body = ? where id = ?", (status, body, draft_id)
                )

    @_locked
    def pending_drafts(self) -> list[sqlite3.Row]:
        return self._db.execute(
            """select d.id, d.body, d.errors, d.reason, m.channel, m.sender, m.subject, m.summary
               from drafts d join messages m on m.id = d.message_id
               where d.status = 'wacht op goedkeuring' order by d.id"""
        ).fetchall()

    @_locked
    def list_messages(
        self,
        intent: str | None = None,
        pending: bool = False,
        query: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if intent:
            clauses.append("m.intent = ?")
            params.append(intent)
        if pending:
            clauses.append("exists (select 1 from drafts d where d.message_id = m.id and d.status = 'wacht op goedkeuring')")
        if query:
            clauses.append("(m.sender like ? or m.subject like ? or m.body like ?)")
            params.extend([f"%{query}%"] * 3)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._db.execute(
            f"""select m.*, (select d.id from drafts d
                   where d.message_id = m.id and d.status = 'wacht op goedkeuring'
                   order by d.id desc limit 1) as draft_id
               from messages m {where} order by m.received_at desc limit ?""",
            params,
        ).fetchall()

    @_locked
    def get_message_row(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from messages where id = ?", (message_id,)).fetchone()

    @_locked
    def drafts_for_message(self, message_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "select * from drafts where message_id = ? order by id", (message_id,)
        ).fetchall()

    @_locked
    def intent_counts(self) -> dict[str, int]:
        rows = self._db.execute(
            "select intent, count(*) as n from messages where intent is not null group by intent"
        ).fetchall()
        return {row["intent"]: row["n"] for row in rows}

    @_locked
    def pending_count(self) -> int:
        row = self._db.execute(
            "select count(*) as n from drafts where status = 'wacht op goedkeuring'"
        ).fetchone()
        return row["n"]

    @_locked
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
