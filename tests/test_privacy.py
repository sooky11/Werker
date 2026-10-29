from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from werker import privacy
from werker.models import Draft, Message
from werker.runtime import Runtime
from werker.store import SCHEDULED, WAITING, Store
from werker.web import create_app


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "w.db"))


def _msg(external_id: str, received_at: datetime, sender: str = "Jan Jansen <jan@example.com>") -> Message:
    return Message(
        channel="mail",
        external_id=external_id,
        thread_id=external_id,
        sender=sender,
        subject="Prijs?",
        body=f"Bericht {external_id}: wat kost een knipbeurt?",
        received_at=received_at,
    )


def _invoice(store: Store, number: str, client_id: int | None) -> int:
    fields = {
        "issue_date": "2026-01-10",
        "due_date": "2026-02-10",
        "currency": "EUR",
        "total_excl": "100.00",
        "total_vat": "21.00",
        "total_incl": "121.00",
        "buyer_name": "Jan Jansen BV",
        "buyer_endpoint": "0106:12345678",
        "client_id": client_id,
    }
    return store.add_invoice(number, fields, "<Invoice/>", "verzonden")


def _client_with_history(store: Store):
    """Klant met een oud bericht (verzonden concept), een open concept en een factuur."""
    old = datetime.now(timezone.utc) - timedelta(days=400)
    client_id = store.create_client("Jan Jansen", [("email", "jan@example.com")])
    old_id = store.add_message(_msg("<old@x>", old), client_id=client_id)
    draft_id = store.add_draft(old_id, Draft(body="Antwoord", needs_human=False, reason=""), errors=[])
    store.set_draft_status(draft_id, "verzonden")
    store.audit(old_id, "verzonden", "naar jan@example.com")
    invoice_id = _invoice(store, "F-2026-001", client_id)
    return client_id, old_id, invoice_id


def test_export_contains_client_identifiers_messages_drafts_and_audit(tmp_path):
    store = _store(tmp_path)
    client_id, old_id, _ = _client_with_history(store)

    data = privacy.export_client(store, client_id)

    assert data["client"]["name"] == "Jan Jansen"
    assert data["identifiers"] == [{"kind": "email", "value": "jan@example.com"}]
    assert [m["id"] for m in data["messages"]] == [old_id]
    assert data["messages"][0]["body"] == "Bericht <old@x>: wat kost een knipbeurt?"
    assert [d["body"] for d in data["drafts"]] == ["Antwoord"]
    assert [a["event"] for a in data["audit"]] == ["verzonden"]


def test_erase_removes_client_and_messages_but_keeps_invoices_with_buyer_replaced(tmp_path):
    store = _store(tmp_path)
    client_id, old_id, invoice_id = _client_with_history(store)
    other_id = store.create_client("Piet", [("email", "piet@example.com")])
    store.add_message(_msg("<other@x>", datetime.now(timezone.utc)), client_id=other_id)

    counts = privacy.erase_client(store, client_id)

    assert counts == {
        "clients_deleted": 1,
        "identifiers_deleted": 1,
        "messages_deleted": 1,
        "drafts_deleted": 1,
        "audit_deleted": 1,
        "invoices_unlinked": 1,
    }
    assert store.get_client(client_id) is None
    assert store.client_identifiers(client_id) == []
    assert store.list_messages(client_id=client_id) == []
    assert store.drafts_for_message(old_id) == []
    assert store.audit_log(old_id) == []
    # Een andere klant blijft ongemoeid.
    assert store.get_client(other_id) is not None
    assert len(store.list_messages(client_id=other_id)) == 1
    # De factuur blijft, maar koper is onherkenbaar en er hangt geen klant meer aan.
    invoice = store.get_invoice(invoice_id)
    assert invoice is not None
    assert invoice["buyer_name"] == "verwijderd"
    assert invoice["client_id"] is None
    assert invoice["total_incl"] == "121.00"


def test_erase_writes_log_row_without_client_id_or_name(tmp_path):
    store = _store(tmp_path)
    client_id, _, _ = _client_with_history(store)

    privacy.erase_client(store, client_id)

    entries = store.privacy_log_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["messages_deleted"] == 1
    assert entry["drafts_deleted"] == 1
    assert entry["audit_deleted"] == 1
    assert entry["invoices_unlinked"] == 1
    assert entry["client_hash"] == privacy._client_hash(client_id)
    assert len(entry["client_hash"]) == 64
    assert "Jan" not in str(entry)


def test_erase_unknown_client_raises_lookup_error(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(LookupError):
        privacy.erase_client(store, 999)
    assert store.privacy_log_entries() == []


def test_retention_purges_old_messages_but_not_open_drafts_or_invoices(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    client_id = store.create_client("Jan", [])

    sent_id = store.add_message(_msg("<sent@x>", old), client_id=client_id)
    sent_draft = store.add_draft(sent_id, Draft("Antwoord", False, ""), errors=[])
    store.set_draft_status(sent_draft, "verzonden")
    store.audit(sent_id, "verzonden")

    waiting_id = store.add_message(_msg("<waiting@x>", old), client_id=client_id)
    store.add_draft(waiting_id, Draft("Concept", False, ""), errors=[])

    scheduled_id = store.add_message(_msg("<scheduled@x>", old), client_id=client_id)
    scheduled_draft = store.add_draft(scheduled_id, Draft("Gepland", False, ""), errors=[])
    store.schedule_draft(scheduled_draft, "Gepland", now + timedelta(days=1))

    recent_id = store.add_message(_msg("<recent@x>", now), client_id=client_id)
    invoice_id = _invoice(store, "F-2025-009", client_id)

    removed = privacy.purge_expired(store, now, days=30)

    assert removed == 1
    assert store.get_message_row(sent_id) is None
    assert store.drafts_for_message(sent_id) == []
    assert store.audit_log(sent_id) == []
    assert store.get_message_row(waiting_id) is not None
    assert store.get_message_row(scheduled_id) is not None
    assert store.get_message_row(recent_id) is not None
    assert store.get_draft(scheduled_draft)["status"] == SCHEDULED
    assert store.drafts_for_message(waiting_id)[0]["status"] == WAITING
    invoice = store.get_invoice(invoice_id)
    assert invoice is not None
    assert invoice["client_id"] == client_id
    assert invoice["buyer_name"] == "Jan Jansen BV"


def test_retention_purge_is_idempotent(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    store.add_message(_msg("<old@x>", now - timedelta(days=90)))

    assert privacy.purge_expired(store, now, days=30) == 1
    assert privacy.purge_expired(store, now, days=30) == 0


def test_retention_zero_purges_nothing(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    msg_id = store.add_message(_msg("<ancient@x>", now - timedelta(days=5000)))

    assert privacy.purge_expired(store, now, days=0) == 0
    assert store.get_message_row(msg_id) is not None


def test_delete_client_requires_matching_confirm_name(tmp_path):
    store = _store(tmp_path)
    client_id, _, _ = _client_with_history(store)
    runtime = Runtime(store, tmp_path, {})
    client = TestClient(create_app(runtime))

    no_confirm = client.request("DELETE", f"/api/clients/{client_id}", json={})
    wrong_name = client.request("DELETE", f"/api/clients/{client_id}", json={"confirm": "Piet"})
    assert no_confirm.status_code == 400
    assert wrong_name.status_code == 400
    assert store.get_client(client_id) is not None

    ok = client.request("DELETE", f"/api/clients/{client_id}", json={"confirm": "Jan Jansen"})
    assert ok.status_code == 200
    assert ok.json()["counts"]["messages_deleted"] == 1
    assert store.get_client(client_id) is None

    log = client.get("/api/privacy/log")
    assert log.status_code == 200
    assert len(log.json()) == 1


def test_export_endpoint_returns_json_download(tmp_path):
    store = _store(tmp_path)
    client_id, _, _ = _client_with_history(store)
    client = TestClient(create_app(Runtime(store, tmp_path, {})))

    response = client.get(f"/api/clients/{client_id}/export")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.json()["client"]["name"] == "Jan Jansen"
