"""Opslag van het AI-verbruik: één rij per aanroep van het model, met de tokens uit response.usage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from werker.store import _as_utc, _locked

USAGE_SCHEMA = """
create table if not exists ai_usage (
    id integer primary key autoincrement,
    at text not null,
    model text not null,
    purpose text not null,
    input_tokens integer not null default 0,
    output_tokens integer not null default 0
);
create index if not exists ai_usage_at on ai_usage (at);
"""


class UsageStoreMixin:
    def _ensure_usage_schema(self) -> None:
        self._db.executescript(USAGE_SCHEMA)

    @_locked
    def record_ai_usage(
        self,
        purpose: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        at: datetime | None = None,
    ) -> None:
        moment = _as_utc(at or datetime.now(timezone.utc))
        self._db.execute(
            "insert into ai_usage (at, model, purpose, input_tokens, output_tokens) values (?, ?, ?, ?, ?)",
            (moment.isoformat(), model, purpose, int(input_tokens), int(output_tokens)),
        )
        self._db.commit()

    @_locked
    def ai_usage_per_model(self, since: datetime, until: datetime | None = None) -> list[sqlite3.Row]:
        """Aanroepen en tokens per model tussen `since` en `until` (standaard: tot nu)."""
        params: list[str] = [_as_utc(since).isoformat()]
        query = "select model, count(*) as calls, sum(input_tokens) as input_tokens, sum(output_tokens) as output_tokens from ai_usage where at >= ?"
        if until is not None:
            query += " and at <= ?"
            params.append(_as_utc(until).isoformat())
        return self._db.execute(query + " group by model order by model", params).fetchall()
