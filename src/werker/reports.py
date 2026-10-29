"""Overzicht over de laatste dagen: berichten, responstijd, prijsvragen en openstaande facturen."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from werker.invoicing import TO_SEND, WAITING_PAYMENT, is_overdue
from werker.models import Intent
from werker.store import Store


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _total(rows) -> str:
    return f"{sum((Decimal(row['total_incl']) for row in rows), Decimal('0')):.2f}"


def summary(store: Store, now: datetime, days: int = 30) -> dict[str, Any]:
    """Samenvatting van de laatste `days` dagen. Facturen tellen mee zoals ze nu staan, niet alleen in de periode."""
    now = _utc(now)
    since = now - timedelta(days=days)

    messages = store.messages_since(since)
    per_channel = Counter(row["channel"] for row in messages)
    per_intent = Counter(row["intent"] or "onbekend" for row in messages)

    # Responstijd: van ontvangst tot het eerste verzonden antwoord uit het auditlog.
    delays = []
    for row in store.first_sent_per_message(since):
        received = datetime.fromisoformat(row["received_at"])
        sent = datetime.fromisoformat(row["sent_at"])
        delays.append((sent - received).total_seconds())
    if delays:
        response = {
            "antwoorden": len(delays),
            "gemiddeld_minuten": round(sum(delays) / len(delays) / 60, 1),
            "toelichting": "Gemiddelde tijd tot het eerste verzonden antwoord per bericht.",
        }
    else:
        response = {
            "antwoorden": 0,
            "gemiddeld_minuten": None,
            "toelichting": "Geen verzonden antwoorden in deze periode.",
        }

    answered = store.answered_intent_counts(since)

    waiting = store.list_invoices(status=WAITING_PAYMENT)
    to_send = store.list_invoices(status=TO_SEND)
    overdue = [invoice for invoice in waiting if is_overdue(invoice, now.date())]
    open_rows = waiting + to_send

    return {
        "vanaf": since.isoformat(),
        "tot": now.isoformat(),
        "dagen": days,
        "berichten": {
            "totaal": len(messages),
            "per_kanaal": dict(per_channel),
            "per_intentie": dict(per_intent),
        },
        "responstijd": response,
        "prijsvragen_beantwoord": answered.get(Intent.PRIJSVRAAG.value, 0),
        "openstaand": {"aantal": len(open_rows), "totaal": _total(open_rows)},
        "verlopen": {"aantal": len(overdue), "totaal": _total(overdue)},
    }
