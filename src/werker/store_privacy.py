"""Opslag voor AVG-functies: inzage, verwijderen en bewaartermijn.

Dit is een mixin voor Store. Alle methoden gebruiken dezelfde verbinding en vergrendeling als Store.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from werker.store import WAITING, SCHEDULED, _locked

PRIVACY_SCHEMA = """
create table if not exists privacy_log (
    id integer primary key autoincrement,
    at text not null,
    client_hash text not null,
    clients_deleted integer not null,
    identifiers_deleted integer not null,
    messages_deleted integer not null,
    drafts_deleted integer not null,
    audit_deleted integer not null,
    invoices_unlinked integer not null
);
"""

_OPEN_STATUSES = (WAITING, SCHEDULED)


class PrivacyStoreMixin:
    _db: Any  # sqlite3.Connection, gezet door Store

    def _ensure_privacy_schema(self) -> None:
        with self._db:
            self._db.execute(PRIVACY_SCHEMA.strip())

    # ---------- inzage ----------

    @_locked
    def privacy_client_messages(self, client_id: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "select * from messages where client_id = ? order by received_at, id", (client_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def privacy_client_drafts(self, client_id: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """select d.* from drafts d join messages m on m.id = d.message_id
               where m.client_id = ? order by d.id""",
            (client_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def privacy_client_audit(self, client_id: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            """select a.* from audit a join messages m on m.id = a.message_id
               where m.client_id = ? order by a.id""",
            (client_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- verwijderen ----------

    @_locked
    def privacy_erase_client(self, client_id: int, client_hash: str) -> dict[str, int]:
        """Verwijder een klant met alles wat eraan hangt, en schrijf een logregel. Alles in een transactie."""
        with self._db:
            client = self._db.execute("select id from clients where id = ?", (client_id,)).fetchone()
            if client is None:
                raise LookupError("klant bestaat niet")
            message_ids = "(select id from messages where client_id = ?)"
            audit = self._db.execute(f"delete from audit where message_id in {message_ids}", (client_id,))
            drafts = self._db.execute(f"delete from drafts where message_id in {message_ids}", (client_id,))
            messages = self._db.execute("delete from messages where client_id = ?", (client_id,))
            identifiers = self._db.execute("delete from client_identifiers where client_id = ?", (client_id,))
            # Facturen blijven als belastingadministratie bewaard; de koper wordt onherkenbaar gemaakt.
            invoices = self._db.execute(
                "update invoices set client_id = null, buyer_name = 'verwijderd' where client_id = ?",
                (client_id,),
            )
            self._db.execute("delete from clients where id = ?", (client_id,))
            counts = {
                "clients_deleted": 1,
                "identifiers_deleted": identifiers.rowcount,
                "messages_deleted": messages.rowcount,
                "drafts_deleted": drafts.rowcount,
                "audit_deleted": audit.rowcount,
                "invoices_unlinked": invoices.rowcount,
            }
            self._db.execute(
                """insert into privacy_log (at, client_hash, clients_deleted, identifiers_deleted,
                   messages_deleted, drafts_deleted, audit_deleted, invoices_unlinked)
                   values (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    client_hash,
                    counts["clients_deleted"],
                    counts["identifiers_deleted"],
                    counts["messages_deleted"],
                    counts["drafts_deleted"],
                    counts["audit_deleted"],
                    counts["invoices_unlinked"],
                ),
            )
        return counts

    @_locked
    def privacy_log_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._db.execute("select * from privacy_log order by id desc limit ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- bewaartermijn ----------

    @_locked
    def privacy_purge_messages(self, cutoff: str) -> int:
        """Verwijder berichten ouder dan cutoff (ISO-tijd in UTC), met hun concepten en auditregels.

        Berichten met een open of geplande concept blijven staan. Facturen worden nooit aangeraakt.
        """
        with self._db:
            expired = """select id from messages where received_at < ?
                          and id not in (select message_id from drafts where status in (?, ?))"""
            params = (cutoff, *_OPEN_STATUSES)
            self._db.execute(f"delete from audit where message_id in ({expired})", params)
            self._db.execute(f"delete from drafts where message_id in ({expired})", params)
            removed = self._db.execute(f"delete from messages where id in ({expired})", params)
        return removed.rowcount
