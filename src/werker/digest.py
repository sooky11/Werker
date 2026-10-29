from __future__ import annotations

from datetime import datetime

from werker.store import Store

# Volgorde en labels van de intenties in het dagoverzicht; None is "nog niet ingedeeld".
INTENT_LABELS: list[tuple[str | None, str]] = [
    ("prijsvraag", "Prijsvragen"),
    ("afspraak", "Afspraken"),
    ("klacht", "Klachten"),
    ("factuur", "Facturen"),
    ("betaling", "Betalingen gemeld"),
    ("overig", "Overig"),
    ("nieuwsbrief", "Nieuwsbrieven"),
    ("spam", "Spam"),
    (None, "Nog niet ingedeeld"),
]

SUMMARY_LENGTH = 80


def digest_subject(now: datetime) -> str:
    return f"Werker dagoverzicht {now:%d-%m-%Y}"


def build_digest(store: Store, since: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    messages = store.messages_since(since)
    pending = store.pending_drafts()
    urgent = [m for m in messages if m["urgency"] == "hoog"]

    lines = [digest_subject(now), f"Periode: sinds {since:%d-%m-%Y %H:%M}", ""]

    if not messages and not pending and not urgent:
        lines.append("Er is niets nieuws gebeurd in deze periode.")
        return "\n".join(lines)

    counts = {intent: 0 for intent, _ in INTENT_LABELS}
    for m in messages:
        counts[m["intent"]] = counts.get(m["intent"], 0) + 1
    lines.append(f"Nieuwe berichten: {len(messages)}")
    for intent, label in INTENT_LABELS:
        lines.append(f"  {label}: {counts[intent]}")
    lines.append("")

    lines.append(f"Wacht op jouw goedkeuring: {len(pending)}")
    for row in pending:
        summary = row["summary"] or _shorten(row["body"])
        lines.append(f"  #{row['id']} {row['sender']} | {row['subject'] or '(geen onderwerp)'}")
        lines.append(f"      {_shorten(summary)}")
    lines.append("")

    lines.append(f"Urgent: {len(urgent)}")
    for m in urgent:
        lines.append(f"  {m['sender']} | {m['subject'] or '(geen onderwerp)'}")

    return "\n".join(lines)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SUMMARY_LENGTH:
        return text
    return text[: SUMMARY_LENGTH - 1].rstrip() + "…"
