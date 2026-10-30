from __future__ import annotations

import functools
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from werker.models import Classification, Draft, Message

WAITING = "wacht op goedkeuring"
SCHEDULED = "ingepland"

SCHEMA = """
create table if not exists clients (
    id integer primary key autoincrement,
    name text not null,
    notes text not null default '',
    created_at text not null
);
create table if not exists client_identifiers (
    id integer primary key autoincrement,
    client_id integer not null references clients(id),
    kind text not null,
    value text not null,
    unique (kind, value)
);
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
    client_id integer references clients(id),
    unique (channel, external_id)
);
create table if not exists drafts (
    id integer primary key autoincrement,
    message_id integer not null references messages(id),
    body text not null,
    errors text not null default '',
    reason text not null default '',
    status text not null default 'wacht op goedkeuring',
    created_at text not null,
    scheduled_for text,
    revises integer,
    change_summary text not null default ''
);
create table if not exists audit (
    id integer primary key autoincrement,
    at text not null,
    message_id integer,
    event text not null,
    detail text not null default ''
);
create table if not exists app_settings (
    key text primary key,
    value text not null
);
create table if not exists invoices (
    id integer primary key autoincrement,
    number text not null unique,
    issue_date text not null,
    due_date text,
    currency text not null,
    total_excl text not null,
    total_vat text not null,
    total_incl text not null,
    buyer_name text not null,
    buyer_endpoint text not null,
    xml text not null,
    status text not null,
    paid_at text,
    sent_at text,
    last_error text not null default '',
    created_at text not null,
    client_id integer references clients(id)
);
"""

# Kolommen die na een eerste versie zijn toegevoegd; oudere databases krijgen ze alsnog.
_ADDED_COLUMNS = {
    "messages": {"client_id": "integer references clients(id)"},
    "invoices": {
        "client_id": "integer references clients(id)",
        "format": "text not null default 'ubl'",
        "needs_review": "integer not null default 0",
    },
    "drafts": {
        "scheduled_for": "text",
        "revises": "integer",
        "change_summary": "text not null default ''",
    },
}


def _locked(method):
    """FastAPI en de achtergrondtaken gebruiken dezelfde verbinding vanuit verschillende threads."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


# Na _locked en de andere hulpfuncties: store_privacy importeert die uit dit bestand.
from werker.store_privacy import PrivacyStoreMixin  # noqa: E402
# De mixins gebruiken _locked en _as_utc uit dit bestand, dus ze worden pas hier geïmporteerd.
from werker.store_billing import BillingStoreMixin  # noqa: E402
from werker.store_reports import ReportsStoreMixin  # noqa: E402
from werker.store_rules import RulesStoreMixin  # noqa: E402
from werker.store_attachments import AttachmentsStoreMixin  # noqa: E402
from werker.store_snippets import SnippetsStoreMixin  # noqa: E402


class Store(PrivacyStoreMixin, ReportsStoreMixin, BillingStoreMixin, RulesStoreMixin, AttachmentsStoreMixin, SnippetsStoreMixin):
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._ensure_privacy_schema()
        self._ensure_billing_schema()
        self._ensure_rules_schema()
        self._ensure_attachments_schema()
        self._ensure_snippets_schema()

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in self._db.execute(f"pragma table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    self._db.execute(f"alter table {table} add column {column} {definition}")
        self._db.commit()

    # ---------- berichten ----------

    @_locked
    def find_message_id(self, channel: str, external_id: str) -> int | None:
        row = self._db.execute(
            "select id from messages where channel = ? and external_id = ?",
            (channel, external_id),
        ).fetchone()
        return row["id"] if row else None

    @_locked
    def reset_failed_message(self, channel: str, external_id: str) -> None:
        """Een bericht dat door een fout niet is verwerkt, mag opnieuw verwerkt worden."""
        with self._db:
            self._db.execute(
                "delete from messages where channel = ? and external_id = ? and status = 'fout'",
                (channel, external_id),
            )

    @_locked
    def add_message(self, msg: Message, client_id: int | None = None) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into messages
                   (channel, external_id, thread_id, sender, subject, body, received_at, client_id)
                   values (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.channel,
                    msg.external_id,
                    msg.thread_id,
                    msg.sender,
                    msg.subject,
                    msg.body,
                    _as_utc(msg.received_at).isoformat(),
                    client_id,
                ),
            )
        self._run_message_hooks(cursor.lastrowid, msg)
        return cursor.lastrowid

    @_locked
    def get_message(self, message_id: int) -> Message:
        row = self._db.execute("select * from messages where id = ?", (message_id,)).fetchone()
        if row is None:
            raise LookupError(f"bericht {message_id} bestaat niet")
        return _row_to_message(row)

    @_locked
    def get_message_row(self, message_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from messages where id = ?", (message_id,)).fetchone()

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
    def list_messages(
        self,
        intent: str | None = None,
        pending: bool = False,
        scheduled: bool = False,
        query: str | None = None,
        client_id: int | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if intent:
            clauses.append("m.intent = ?")
            params.append(intent)
        if pending:
            clauses.append("exists (select 1 from drafts d where d.message_id = m.id and d.status = ?)")
            params.append(WAITING)
        if scheduled:
            clauses.append("exists (select 1 from drafts d where d.message_id = m.id and d.status = ?)")
            params.append(SCHEDULED)
        if query:
            clauses.append("(m.sender like ? or m.subject like ? or m.body like ?)")
            params.extend([f"%{query}%"] * 3)
        if client_id is not None:
            clauses.append("m.client_id = ?")
            params.append(client_id)
        where = f"where {' and '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._db.execute(
            f"""select m.*, (select d.id from drafts d
                   where d.message_id = m.id and d.status in (?, ?)
                   order by d.id desc limit 1) as draft_id
               from messages m {where} order by m.received_at desc limit ?""",
            [WAITING, SCHEDULED, *params],
        ).fetchall()

    @_locked
    def messages_since(self, since: datetime) -> list[sqlite3.Row]:
        return self._db.execute(
            "select * from messages where received_at >= ? order by received_at",
            (_as_utc(since).isoformat(),),
        ).fetchall()

    @_locked
    def intent_counts(self) -> dict[str, int]:
        rows = self._db.execute(
            "select intent, count(*) as n from messages where intent is not null group by intent"
        ).fetchall()
        return {row["intent"]: row["n"] for row in rows}

    # ---------- concepten ----------

    @_locked
    def add_draft(
        self,
        message_id: int,
        draft: Draft,
        errors: list[str],
        revises: int | None = None,
        change_summary: str = "",
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into drafts (message_id, body, errors, reason, created_at, revises, change_summary)
                   values (?, ?, ?, ?, ?, ?, ?)""",
                (message_id, draft.body, "\n".join(errors), draft.reason, _now(), revises, change_summary),
            )
        return cursor.lastrowid

    @_locked
    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from drafts where id = ?", (draft_id,)).fetchone()

    @_locked
    def drafts_for_message(self, message_id: int) -> list[sqlite3.Row]:
        return self._db.execute("select * from drafts where message_id = ? order by id", (message_id,)).fetchall()

    @_locked
    def set_draft_status(self, draft_id: int, status: str, body: str | None = None) -> None:
        with self._db:
            if body is None:
                self._db.execute("update drafts set status = ? where id = ?", (status, draft_id))
            else:
                self._db.execute("update drafts set status = ?, body = ? where id = ?", (status, body, draft_id))

    @_locked
    def schedule_draft(self, draft_id: int, body: str, send_at: datetime) -> None:
        with self._db:
            self._db.execute(
                "update drafts set status = ?, body = ?, scheduled_for = ? where id = ?",
                (SCHEDULED, body, _as_utc(send_at).isoformat(), draft_id),
            )

    @_locked
    def unschedule_draft(self, draft_id: int) -> None:
        with self._db:
            self._db.execute("update drafts set status = ?, scheduled_for = null where id = ?", (WAITING, draft_id))

    @_locked
    def due_drafts(self, now: datetime) -> list[sqlite3.Row]:
        return self._db.execute(
            "select * from drafts where status = ? and scheduled_for <= ? order by scheduled_for",
            (SCHEDULED, _as_utc(now).isoformat()),
        ).fetchall()

    @_locked
    def open_draft_for_thread(self, channel: str, thread_id: str) -> sqlite3.Row | None:
        """Het laatste concept dat nog wacht of is ingepland, in dezelfde gespreksdraad."""
        return self._db.execute(
            """select d.* from drafts d join messages m on m.id = d.message_id
               where m.channel = ? and m.thread_id = ? and d.status in (?, ?)
               order by d.id desc limit 1""",
            (channel, thread_id, WAITING, SCHEDULED),
        ).fetchone()

    @_locked
    def pending_drafts(self) -> list[sqlite3.Row]:
        return self._db.execute(
            """select d.id, d.body, d.errors, d.reason, m.channel, m.sender, m.subject, m.summary
               from drafts d join messages m on m.id = d.message_id
               where d.status = ? order by d.id""",
            (WAITING,),
        ).fetchall()

    @_locked
    def pending_count(self) -> int:
        row = self._db.execute("select count(*) as n from drafts where status = ?", (WAITING,)).fetchone()
        return row["n"]

    @_locked
    def scheduled_count(self) -> int:
        row = self._db.execute("select count(*) as n from drafts where status = ?", (SCHEDULED,)).fetchone()
        return row["n"]

    # ---------- klanten ----------

    @_locked
    def find_client_by_identifier(self, kind: str, value: str) -> int | None:
        row = self._db.execute(
            "select client_id from client_identifiers where kind = ? and value = ?", (kind, value)
        ).fetchone()
        return row["client_id"] if row else None

    @_locked
    def create_client(self, name: str, identifiers: list[tuple[str, str]]) -> int:
        with self._db:
            cursor = self._db.execute("insert into clients (name, created_at) values (?, ?)", (name, _now()))
            client_id = cursor.lastrowid
            for kind, value in identifiers:
                self._db.execute(
                    "insert or ignore into client_identifiers (client_id, kind, value) values (?, ?, ?)",
                    (client_id, kind, value),
                )
        return client_id

    @_locked
    def add_identifier(self, client_id: int, kind: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "insert into client_identifiers (client_id, kind, value) values (?, ?, ?)",
                (client_id, kind, value),
            )

    @_locked
    def set_message_client(self, message_id: int, client_id: int) -> None:
        with self._db:
            self._db.execute("update messages set client_id = ? where id = ?", (client_id, message_id))

    @_locked
    def get_client(self, client_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from clients where id = ?", (client_id,)).fetchone()

    @_locked
    def client_identifiers(self, client_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "select kind, value from client_identifiers where client_id = ? order by kind, value", (client_id,)
        ).fetchall()

    @_locked
    def normalize_phone_identifiers(self, convert: Any) -> dict[str, int]:
        """Zet opgeslagen telefoonnummers om met convert. Botst het nieuwe nummer met een andere klant, dan blijft het staan."""
        counts = {"omgezet": 0, "samengevoegd": 0, "botsing": 0}
        rows = self._db.execute("select id, client_id, value from client_identifiers where kind = 'phone'").fetchall()
        with self._db:
            for row in rows:
                new = convert(row["value"])
                if new == row["value"]:
                    continue
                existing = self._db.execute(
                    "select client_id from client_identifiers where kind = 'phone' and value = ?", (new,)
                ).fetchone()
                if existing is None:
                    self._db.execute("update client_identifiers set value = ? where id = ?", (new, row["id"]))
                    counts["omgezet"] += 1
                elif existing["client_id"] == row["client_id"]:
                    self._db.execute("delete from client_identifiers where id = ?", (row["id"],))
                    counts["samengevoegd"] += 1
                else:
                    counts["botsing"] += 1
        return counts

    @_locked
    def list_clients(self, query: str | None = None) -> list[sqlite3.Row]:
        where = ""
        params: list[Any] = []
        if query:
            where = """where c.name like ? or exists (select 1 from client_identifiers i
                       where i.client_id = c.id and i.value like ?)"""
            params = [f"%{query}%", f"%{query}%"]
        return self._db.execute(
            f"""select c.id, c.name, c.notes,
                   count(m.id) as message_count,
                   max(m.received_at) as last_received,
                   group_concat(distinct m.channel) as channels
                from clients c left join messages m on m.client_id = c.id
                {where}
                group by c.id order by last_received desc, c.name""",
            params,
        ).fetchall()

    @_locked
    def update_client(self, client_id: int, name: str | None = None, notes: str | None = None) -> None:
        with self._db:
            if name is not None:
                self._db.execute("update clients set name = ? where id = ?", (name, client_id))
            if notes is not None:
                self._db.execute("update clients set notes = ? where id = ?", (notes, client_id))

    @_locked
    def merge_clients(self, source_id: int, target_id: int) -> None:
        """Voeg source samen met target: berichten en identificaties gaan naar target."""
        with self._db:
            self._db.execute("update messages set client_id = ? where client_id = ?", (target_id, source_id))
            self._db.execute("update client_identifiers set client_id = ? where client_id = ?", (target_id, source_id))
            self._db.execute("delete from clients where id = ?", (source_id,))

    # ---------- facturen ----------

    @_locked
    def add_invoice(self, number: str, fields: dict[str, Any], xml: str, status: str) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into invoices (number, issue_date, due_date, currency, total_excl, total_vat,
                   total_incl, buyer_name, buyer_endpoint, xml, status, created_at, client_id, format, needs_review)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    number,
                    fields["issue_date"],
                    fields["due_date"],
                    fields["currency"],
                    str(fields["total_excl"]),
                    str(fields["total_vat"]),
                    str(fields["total_incl"]),
                    fields["buyer_name"],
                    fields["buyer_endpoint"],
                    xml,
                    status,
                    _now(),
                    fields.get("client_id"),
                    fields.get("format", "ubl"),
                    1 if fields.get("needs_review") else 0,
                ),
            )
        return cursor.lastrowid

    @_locked
    def get_invoice(self, invoice_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from invoices where id = ?", (invoice_id,)).fetchone()

    @_locked
    def find_invoice_by_number(self, number: str) -> sqlite3.Row | None:
        return self._db.execute("select * from invoices where number = ?", (number,)).fetchone()

    @_locked
    def list_invoices(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._db.execute(
                "select * from invoices where status = ? order by issue_date desc, id desc", (status,)
            ).fetchall()
        return self._db.execute("select * from invoices order by issue_date desc, id desc").fetchall()

    @_locked
    def update_invoice(self, invoice_id: int, **values: Any) -> None:
        allowed = {"status", "paid_at", "sent_at", "last_error", "client_id", "needs_review"}
        if set(values) - allowed:
            raise ValueError(f"onbekend factuurveld: {set(values) - allowed}")
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._db:
            self._db.execute(f"update invoices set {assignments} where id = ?", [*values.values(), invoice_id])

    # ---------- instellingen ----------

    @_locked
    def get_settings(self) -> dict[str, str]:
        rows = self._db.execute("select key, value from app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    @_locked
    def set_settings(self, values: dict[str, str | None]) -> None:
        with self._db:
            for key, value in values.items():
                if value is None:
                    self._db.execute("delete from app_settings where key = ?", (key,))
                else:
                    self._db.execute(
                        "insert into app_settings (key, value) values (?, ?) "
                        "on conflict(key) do update set value = excluded.value",
                        (key, value),
                    )

    # ---------- auditlog ----------

    @_locked
    def audit(self, message_id: int | None, event: str, detail: str = "") -> None:
        with self._db:
            self._db.execute(
                "insert into audit (at, message_id, event, detail) values (?, ?, ?, ?)",
                (_now(), message_id, event, detail),
            )

    @_locked
    def audit_log(self, message_id: int, limit: int = 50) -> list[sqlite3.Row]:
        return self._db.execute(
            "select at, event, detail from audit where message_id = ? order by id desc limit ?",
            (message_id, limit),
        ).fetchall()


def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        channel=row["channel"],
        external_id=row["external_id"],
        thread_id=row["thread_id"],
        sender=row["sender"],
        subject=row["subject"],
        body=row["body"],
        received_at=datetime.fromisoformat(row["received_at"]),
    )
