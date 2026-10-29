"""Peppol-aansluiting via een access point (Storecove). Verstuurt UBL-facturen met de API van de aanbieder.

Let op: de JSON-velden in build_payload volgen de documentatie van Storecove zoals die bekend is, maar zijn
nog niet getest tegen de live API. Controleer ze met een testaccount voordat je echte facturen verstuurt.
Zonder ingestelde API-sleutel en legal entity wordt er niets verstuurd; de factuur blijft dan in de map.
"""

from __future__ import annotations

import base64
from typing import Any, Mapping

import httpx

API_BASE = "https://api.storecove.com"
ISO6523_SCHEME = "iso6523-actorid-upis"


class PeppolError(RuntimeError):
    pass


def build_payload(xml: bytes, recipient: str, legal_entity_id: str) -> dict[str, Any]:
    """Bouw het verzoek voor een UBL-factuur. recipient is het Peppol-adres, bijvoorbeeld 0106:12345678."""
    return {
        "legalEntityId": int(legal_entity_id),
        "routing": {"eIdentifiers": [{"scheme": ISO6523_SCHEME, "id": recipient}]},
        "document": {
            "documentType": "invoice",
            "rawDocument": {
                "document": base64.b64encode(xml).decode("ascii"),
                "parse": True,
                "parseStrategy": "ubl",
            },
        },
    }


class StorecoveAccessPoint:
    def __init__(self, api_key: str, legal_entity_id: str, client: httpx.Client | None = None):
        self._api_key = api_key
        self._legal_entity_id = legal_entity_id
        self._client = client or httpx.Client(base_url=API_BASE, timeout=30)

    def send(self, xml: bytes, recipient: str) -> str:
        response = self._client.post(
            "/api/v2/document_submissions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=build_payload(xml, recipient, self._legal_entity_id),
        )
        if response.status_code >= 400:
            raise PeppolError(f"access point weigerde de factuur ({response.status_code}): {response.text[:200]}")
        return str(response.json().get("guid", ""))


def build_access_point(env: Mapping[str, str]) -> StorecoveAccessPoint | None:
    """Maak de aansluiting uit de instellingen. Geeft None als er nog geen aanbieder of sleutel is."""
    if env.get("PEPPOL_PROVIDER", "") != "storecove":
        return None
    api_key = env.get("PEPPOL_API_KEY", "")
    legal_entity_id = env.get("PEPPOL_LEGAL_ENTITY_ID", "")
    if not api_key or not legal_entity_id:
        return None
    return StorecoveAccessPoint(api_key, legal_entity_id)
