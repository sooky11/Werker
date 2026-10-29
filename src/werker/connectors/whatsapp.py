from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from werker.models import Message

CHANNEL = "whatsapp"
GRAPH_URL = "https://graph.facebook.com/v21.0"
SERVICE_WINDOW = timedelta(hours=24)


class WindowClosedError(RuntimeError):
    pass


def verify_signature(app_secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_webhook(payload: dict[str, Any]) -> list[Message]:
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c["wa_id"]: c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for item in value.get("messages", []):
                if item.get("type") != "text":
                    continue
                sender_id = item["from"]
                name = names.get(sender_id, "")
                messages.append(
                    Message(
                        channel=CHANNEL,
                        external_id=item["id"],
                        thread_id=sender_id,
                        sender=f"{name} <{sender_id}>".strip(),
                        subject="",
                        body=item["text"]["body"],
                        received_at=datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc),
                    )
                )
    return messages


class WhatsAppConnector:
    name = CHANNEL

    def __init__(self, phone_number_id: str, access_token: str, client: httpx.Client | None = None):
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._client = client or httpx.Client(timeout=20)

    def fetch_new(self) -> list[Message]:
        # Berichten komen binnen via de webhook; er is niets om op te halen.
        return []

    def send(self, msg: Message, body: str) -> None:
        if datetime.now(timezone.utc) - msg.received_at > SERVICE_WINDOW:
            raise WindowClosedError("servicevenster van 24 uur is verlopen; gebruik een goedgekeurd template")
        response = self._client.post(
            f"{GRAPH_URL}/{self._phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"messaging_product": "whatsapp", "to": msg.thread_id, "type": "text", "text": {"body": body}},
        )
        response.raise_for_status()
