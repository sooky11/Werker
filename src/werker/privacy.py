"""AVG: inzage, verwijdering op verzoek en automatische bewaartermijn voor berichten.

Facturen worden nooit verwijderd: die zijn belastingadministratie. Bij verwijdering van een klant
worden alleen de koper-naam en de koppeling aan de klant aangepast.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from werker.store import Store


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _client_hash(client_id: int) -> str:
    """Het logboek bewaart geen klant-id, alleen een hash daarvan. Let op: id's zijn klein,
    dus de hash is pseudoniem en niet anoniem."""
    return hashlib.sha256(f"klant:{client_id}".encode("utf-8")).hexdigest()


def export_client(store: Store, client_id: int) -> dict[str, Any]:
    """Alles wat de opslag over één klant bewaart, als gewone gegevens (voor inzageverzoeken)."""
    client = store.get_client(client_id)
    if client is None:
        raise LookupError("klant bestaat niet")
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "client": {
            "id": client["id"],
            "name": client["name"],
            "notes": client["notes"],
            "created_at": client["created_at"],
        },
        "identifiers": [{"kind": row["kind"], "value": row["value"]} for row in store.client_identifiers(client_id)],
        "messages": store.privacy_client_messages(client_id),
        "drafts": store.privacy_client_drafts(client_id),
        "audit": store.privacy_client_audit(client_id),
    }


def erase_client(store: Store, client_id: int) -> dict[str, int]:
    """Verwijder een klant, zijn identificaties, berichten, concepten en auditregels.

    Facturen blijven staan met koper 'verwijderd'. Het logboek krijgt een regel met aantallen.
    """
    return store.privacy_erase_client(client_id, _client_hash(client_id))


def purge_expired(store: Store, now: datetime, days: int) -> int:
    """Verwijder berichten ouder dan `days` dagen. Met 0 wordt niets verwijderd.

    Berichten met een open of geplande concept blijven staan; facturen worden nooit geraakt.
    Herhaald aanroepen verwijdert niets nieuws.
    """
    if days <= 0:
        return 0
    cutoff = _utc(now) - timedelta(days=days)
    return store.privacy_purge_messages(cutoff.isoformat())
