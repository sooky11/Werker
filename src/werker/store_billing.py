"""Opslag voor betalingsherinneringen: één rij per herinnering, gekoppeld aan een factuur.

Een herinnering heeft geen bericht nodig, daarom staat ze in een eigen tabel en niet tussen de berichten.
De laatste herinnering per factuur bepaalt of er binnen zeven dagen al een nieuwe komt.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from werker.store import _locked

REMINDER_WAITING = "wacht op goedkeuring"
REMINDER_NO_ADDRESS = "geen adres bekend"
REMINDER_SENT = "verzonden"
REMINDER_REJECTED = "afgewezen"
REMINDER_EXPIRED = "vervallen"
REMINDER_PENDING = (REMINDER_WAITING, REMINDER_NO_ADDRESS)

BILLING_SCHEMA = """
create table if not exists reminders (
    id integer primary key autoincrement,
    invoice_id integer not null references invoices(id),
    status text not null,
    recipient text not null default '',
    subject text not null default '',
    body text not null default '',
    created_at text not null,
    decided_at text,
    sent_at text,
    last_error text not null default ''
);
create index if not exists reminders_invoice on reminders (invoice_id, created_at);
"""

_REMINDER_FIELDS = {"status", "recipient", "subject", "body", "decided_at", "sent_at", "last_error"}


class BillingStoreMixin:
    def _ensure_billing_schema(self) -> None:
        self._db.executescript(BILLING_SCHEMA)

    @_locked
    def last_reminder_at(self, invoice_id: int) -> str | None:
        row = self._db.execute(
            "select max(created_at) as last from reminders where invoice_id = ?", (invoice_id,)
        ).fetchone()
        return row["last"]

    @_locked
    def add_reminder(
        self, invoice_id: int, status: str, recipient: str, subject: str, body: str, created_at: str
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into reminders (invoice_id, status, recipient, subject, body, created_at)
                   values (?, ?, ?, ?, ?, ?)""",
                (invoice_id, status, recipient, subject, body, created_at),
            )
        return cursor.lastrowid

    @_locked
    def get_reminder(self, reminder_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from reminders where id = ?", (reminder_id,)).fetchone()

    @_locked
    def update_reminder(self, reminder_id: int, **values: Any) -> None:
        if set(values) - _REMINDER_FIELDS:
            raise ValueError(f"onbekend herinneringsveld: {set(values) - _REMINDER_FIELDS}")
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._db:
            self._db.execute(f"update reminders set {assignments} where id = ?", [*values.values(), reminder_id])

    @_locked
    def pending_reminders(self, invoice_status: str) -> list[sqlite3.Row]:
        """Herinneringen die op jou wachten, alleen bij facturen met de gegeven status (wacht op betaling)."""
        placeholders = ", ".join("?" for _ in REMINDER_PENDING)
        return self._db.execute(
            f"""select r.*, i.number, i.buyer_name, i.total_incl, i.currency, i.due_date,
                       i.status as invoice_status, i.client_id
                from reminders r join invoices i on i.id = r.invoice_id
                where r.status in ({placeholders}) and i.status = ?
                order by r.id""",
            [*REMINDER_PENDING, invoice_status],
        ).fetchall()

    @_locked
    def all_pending_reminders(self) -> list[sqlite3.Row]:
        """Alle openstaande herinneringen, ook die waarvan de factuur al betaald is (om ze op te ruimen)."""
        placeholders = ", ".join("?" for _ in REMINDER_PENDING)
        return self._db.execute(
            f"""select r.id, r.invoice_id, i.status as invoice_status
                from reminders r join invoices i on i.id = r.invoice_id
                where r.status in ({placeholders})""",
            REMINDER_PENDING,
        ).fetchall()
