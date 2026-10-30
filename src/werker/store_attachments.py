"""Opslag voor bijlagen: één rij per bijlage, de inhoud staat als bestand in <datamap>/bijlagen.

Dit is een mixin voor Store. `path` is relatief ten opzichte van `attachments_root`; te grote bijlagen
hebben geen bestand (path is leeg en too_large is 1).

Daarnaast kan Store na het opslaan van een bericht functies aanroepen (add_message_hook); zo slaat
werker.attachments de bijlagen op zonder dat de pipeline daarvoor hoeft te veranderen.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable

from werker.models import Message
from werker.store import _locked, _now

log = logging.getLogger(__name__)

ATTACHMENTS_SCHEMA = """
create table if not exists attachments (
    id integer primary key autoincrement,
    message_id integer not null references messages(id),
    filename text not null,
    content_type text not null,
    size integer not null,
    path text not null default '',
    too_large integer not null default 0,
    created_at text not null
);
create index if not exists attachments_message on attachments (message_id);
"""

MessageHook = Callable[[int, Message], None]


class AttachmentsStoreMixin:
    _db: Any  # sqlite3.Connection, gezet door Store
    # De map met bijlagen; gezet door werker.attachments.enable. Zonder map worden geen bijlagen opgeslagen.
    attachments_root: Path | None = None

    def _ensure_attachments_schema(self) -> None:
        self._message_hooks: list[MessageHook] = []
        self._db.executescript(ATTACHMENTS_SCHEMA)

    def add_message_hook(self, hook: MessageHook) -> None:
        """hook(message_id, msg) draait direct nadat een bericht is opgeslagen. Fouten worden gelogd."""
        self._message_hooks.append(hook)

    def _run_message_hooks(self, message_id: int, msg: Message) -> None:
        for hook in self._message_hooks:
            try:
                hook(message_id, msg)
            except Exception:
                log.exception("verwerking na opslaan van bericht %s mislukt", message_id)

    @_locked
    def add_attachment(
        self, message_id: int, filename: str, content_type: str, size: int, path: str, too_large: bool
    ) -> int:
        with self._db:
            cursor = self._db.execute(
                """insert into attachments (message_id, filename, content_type, size, path, too_large, created_at)
                   values (?, ?, ?, ?, ?, ?, ?)""",
                (message_id, filename, content_type, size, path, 1 if too_large else 0, _now()),
            )
        return cursor.lastrowid

    @_locked
    def list_attachments(self, message_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "select * from attachments where message_id = ? order by id", (message_id,)
        ).fetchall()

    @_locked
    def get_attachment(self, attachment_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from attachments where id = ?", (attachment_id,)).fetchone()

    @_locked
    def orphan_attachments(self) -> list[sqlite3.Row]:
        """Bijlagen waarvan het bericht niet meer bestaat (verwijderd voor de AVG of de bewaartermijn)."""
        return self._db.execute(
            "select * from attachments where message_id not in (select id from messages) order by id"
        ).fetchall()

    @_locked
    def delete_attachment_rows(self, attachment_ids: list[int]) -> int:
        if not attachment_ids:
            return 0
        placeholders = ", ".join("?" for _ in attachment_ids)
        with self._db:
            cursor = self._db.execute(f"delete from attachments where id in ({placeholders})", attachment_ids)
        return cursor.rowcount
