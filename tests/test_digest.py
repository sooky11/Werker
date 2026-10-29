from datetime import datetime, timedelta, timezone

from werker.digest import build_digest, digest_subject
from werker.models import Classification, Draft, Intent, Message
from werker.store import Store

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=24)


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "werker.db"))


def _add(store: Store, n: int, received_at: datetime, intent: Intent | None, urgency: str = "normaal") -> int:
    message_id = store.add_message(
        Message(
            channel="mail",
            external_id=f"<{n}@example.com>",
            thread_id=f"<{n}@example.com>",
            sender=f"klant{n}@example.com",
            subject=f"Onderwerp {n}",
            body=f"Bericht {n}",
            received_at=received_at,
        )
    )
    if intent is not None:
        store.set_classification(
            message_id, Classification(intent=intent, urgency=urgency, language="nl", confidence=0.9, summary=f"Samenvatting {n}")
        )
    return message_id


def test_digest_counts_only_messages_since_cutoff(tmp_path):
    store = _store(tmp_path)
    _add(store, 1, NOW - timedelta(hours=1), Intent.PRIJSVRAAG)
    _add(store, 2, NOW - timedelta(hours=2), Intent.PRIJSVRAAG)
    _add(store, 3, NOW - timedelta(hours=5), Intent.SPAM)
    _add(store, 4, NOW - timedelta(hours=3), None)
    _add(store, 5, NOW - timedelta(days=3), Intent.KLACHT)  # buiten het venster

    text = build_digest(store, SINCE, now=NOW)

    assert "Nieuwe berichten: 4" in text
    assert "  Prijsvragen: 2" in text
    assert "  Spam: 1" in text
    assert "  Klachten: 0" in text
    assert "  Nog niet ingedeeld: 1" in text
    assert "Onderwerp 5" not in text


def test_digest_lists_pending_drafts(tmp_path):
    store = _store(tmp_path)
    message_id = _add(store, 1, NOW - timedelta(hours=1), Intent.PRIJSVRAAG)
    store.add_draft(message_id, Draft(body="Een herenknipbeurt is € 25,00.", needs_human=False, reason=""), [])

    text = build_digest(store, SINCE, now=NOW)

    assert "Wacht op jouw goedkeuring: 1" in text
    assert "#1 klant1@example.com | Onderwerp 1" in text
    assert "Samenvatting 1" in text


def test_digest_lists_urgent_messages(tmp_path):
    store = _store(tmp_path)
    _add(store, 1, NOW - timedelta(hours=1), Intent.KLACHT, urgency="hoog")
    _add(store, 2, NOW - timedelta(hours=1), Intent.OVERIG, urgency="normaal")

    text = build_digest(store, SINCE, now=NOW)

    assert "Urgent: 1" in text
    assert "klant1@example.com | Onderwerp 1" in text
    assert "klant2@example.com" not in text.split("Urgent:")[1]


def test_digest_says_when_nothing_happened(tmp_path):
    store = _store(tmp_path)

    text = build_digest(store, SINCE, now=NOW)

    assert "Er is niets nieuws gebeurd in deze periode." in text
    assert "Nieuwe berichten" not in text


def test_digest_compares_timezones_correctly(tmp_path):
    store = _store(tmp_path)
    amsterdam = timezone(timedelta(hours=1))
    # 01:30 op 29-10 in Amsterdam is 00:30 UTC: binnen het venster (vanaf 00:00 UTC).
    _add(store, 1, datetime(2026, 10, 29, 1, 30, tzinfo=amsterdam), Intent.AFSPRAAK)
    # 00:30 op 29-10 in Amsterdam is 23:30 UTC op 28-10: buiten het venster, ook al is de tekst later.
    _add(store, 2, datetime(2026, 10, 29, 0, 30, tzinfo=amsterdam), Intent.KLACHT)

    text = build_digest(store, datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc), now=NOW)

    assert "Nieuwe berichten: 1" in text
    assert "  Afspraken: 1" in text
    assert "  Klachten: 0" in text


def test_digest_subject_contains_date():
    assert digest_subject(NOW) == "Werker dagoverzicht 29-10-2026"
