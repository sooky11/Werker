"""Opslag voor afzenderregels: een afzender die je eenmaal goed hebt gecategoriseerd, krijgt voortaan die categorie.

Dit is een mixin voor Store. Alle methoden gebruiken dezelfde verbinding en vergrendeling als Store.
"""

from __future__ import annotations

from typing import Any

from werker.store import _locked, _now

RULES_SCHEMA = """
create table if not exists sender_rules (
    id integer primary key autoincrement,
    address text not null unique,
    intent text not null,
    created_by text not null default '',
    created_at text not null
);
"""


class RulesStoreMixin:
    _db: Any  # sqlite3.Connection, gezet door Store

    def _ensure_rules_schema(self) -> None:
        with self._db:
            self._db.execute(RULES_SCHEMA.strip())

    @_locked
    def set_message_intent(self, message_id: int, intent: str) -> None:
        with self._db:
            self._db.execute("update messages set intent = ? where id = ?", (intent, message_id))

    @_locked
    def set_sender_rule(self, address: str, intent: str, created_by: str | None = None) -> int:
        """Leg vast welke categorie berichten van dit adres krijgen. Een bestaande regel wordt overschreven."""
        address = address.strip().lower()
        with self._db:
            self._db.execute(
                """insert into sender_rules (address, intent, created_by, created_at) values (?, ?, ?, ?)
                   on conflict(address) do update set intent = excluded.intent,
                   created_by = excluded.created_by, created_at = excluded.created_at""",
                (address, intent, created_by or "", _now()),
            )
        row = self._db.execute("select id from sender_rules where address = ?", (address,)).fetchone()
        return int(row["id"])

    @_locked
    def sender_rule_intent(self, address: str) -> str | None:
        row = self._db.execute(
            "select intent from sender_rules where address = ?", (address.strip().lower(),)
        ).fetchone()
        return row["intent"] if row is not None else None

    @_locked
    def list_sender_rules(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "select id, address, intent, created_by, created_at from sender_rules order by address"
        ).fetchall()
        return [dict(row) for row in rows]

    @_locked
    def delete_sender_rule(self, rule_id: int) -> bool:
        with self._db:
            cursor = self._db.execute("delete from sender_rules where id = ?", (rule_id,))
        return cursor.rowcount > 0
