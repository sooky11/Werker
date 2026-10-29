"""Alleen-lezen zoekopdrachten voor het overzicht (werker.reports)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from werker.store import _as_utc, _locked


class ReportsStoreMixin:
    @_locked
    def first_sent_per_message(self, since: datetime) -> list[sqlite3.Row]:
        """Per bericht sinds `since` het tijdstip van het eerste verzonden antwoord uit het auditlog."""
        return self._db.execute(
            """select m.id as message_id, m.received_at, min(a.at) as sent_at
               from messages m join audit a on a.message_id = m.id and a.event = 'verzonden'
               where m.received_at >= ? group by m.id""",
            (_as_utc(since).isoformat(),),
        ).fetchall()

    @_locked
    def answered_intent_counts(self, since: datetime) -> dict[str, int]:
        """Hoeveel berichten per intentie sinds `since` een verzonden antwoord kregen."""
        rows = self._db.execute(
            """select m.intent, count(*) as n from messages m
               where m.received_at >= ? and m.intent is not null
                 and exists (select 1 from audit a where a.message_id = m.id and a.event = 'verzonden')
               group by m.intent""",
            (_as_utc(since).isoformat(),),
        ).fetchall()
        return {row["intent"]: row["n"] for row in rows}
