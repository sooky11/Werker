"""Klanten: berichten van mail, WhatsApp en iMessage worden gekoppeld aan één persoon.

Een klant wordt herkend aan zijn e-mailadres of telefoonnummer. Komt een onbekend adres of nummer
binnen, dan maakt de app een nieuwe klant aan; die kun je later samenvoegen met een andere.
"""

from __future__ import annotations

import re
from email.utils import parseaddr

from werker.models import Message
from werker.store import Store


def normalize(value: str) -> tuple[str, str] | None:
    """Maak van een adres of nummer een vergelijkbare sleutel: (soort, waarde)."""
    cleaned = (value or "").strip().lower()
    if not cleaned or cleaned in {"onbekend", "onbekend <onbekend>"}:
        return None
    if "@" in cleaned:
        return ("email", cleaned)
    digits = re.sub(r"\D", "", cleaned)
    if digits:
        return ("phone", _international(digits))
    return None


def _international(digits: str) -> str:
    """Nederlandse nummers krijgen dezelfde vorm als internationaal: 06..., +31 6... en 0031 6... worden 316...

    Nummers met een andere landcode blijven zoals ze zijn. Al opgeslagen nummers zet migrate_stored_phones om.
    """
    if digits.startswith("0031"):
        return digits[2:]
    if len(digits) == 10 and digits.startswith("0") and not digits.startswith("00"):
        return "31" + digits[1:]
    return digits


def migrate_stored_phones(store: Store) -> dict[str, int]:
    """Zet oude nummers (06...) in de database om naar de internationale vorm, zodat ze weer matchen."""
    return store.normalize_phone_identifiers(_international)


def identifier_for(msg: Message) -> tuple[str, str] | None:
    address = parseaddr(msg.sender)[1] or msg.sender
    return normalize(address)


def display_name(msg: Message) -> str:
    name, address = parseaddr(msg.sender)
    if name.strip():
        return name.strip()
    return address or msg.sender or "Onbekend"


def resolve_client(store: Store, msg: Message) -> int | None:
    identifier = identifier_for(msg)
    if identifier is None:
        return None
    existing = store.find_client_by_identifier(*identifier)
    if existing is not None:
        return existing
    return store.create_client(display_name(msg), [identifier])
