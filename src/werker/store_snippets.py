"""Opslag voor vaste antwoordblokken: korte teksten die je in een concept kunt invoegen. Mixin voor Store."""

from __future__ import annotations

import sqlite3
from typing import Any

from werker.store import _locked, _now

SNIPPETS_SCHEMA = """
create table if not exists snippets (
    id integer primary key autoincrement,
    title text not null,
    body text not null,
    created_at text not null
);
"""


class SnippetsStoreMixin:
    _db: Any  # sqlite3.Connection, gezet door Store

    def _ensure_snippets_schema(self) -> None:
        self._db.executescript(SNIPPETS_SCHEMA)

    @_locked
    def list_snippets(self) -> list[sqlite3.Row]:
        return self._db.execute("select * from snippets order by title collate nocase, id").fetchall()

    @_locked
    def get_snippet(self, snippet_id: int) -> sqlite3.Row | None:
        return self._db.execute("select * from snippets where id = ?", (snippet_id,)).fetchone()

    @_locked
    def add_snippet(self, title: str, body: str) -> int:
        with self._db:
            cursor = self._db.execute(
                "insert into snippets (title, body, created_at) values (?, ?, ?)", (title, body, _now())
            )
        return cursor.lastrowid

    @_locked
    def update_snippet(self, snippet_id: int, title: str, body: str) -> bool:
        with self._db:
            cursor = self._db.execute(
                "update snippets set title = ?, body = ? where id = ?", (title, body, snippet_id)
            )
        return cursor.rowcount > 0

    @_locked
    def delete_snippet(self, snippet_id: int) -> bool:
        with self._db:
            cursor = self._db.execute("delete from snippets where id = ?", (snippet_id,))
        return cursor.rowcount > 0
