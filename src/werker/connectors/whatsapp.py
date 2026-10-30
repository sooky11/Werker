from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from werker.models import Message

CHANNEL = "whatsapp"
GRAPH_URL = "https://graph.facebook.com/v21.0"
SERVICE_WINDOW = timedelta(hours=24)
# Wat send() teruggeeft als buiten het venster het goedgekeurde sjabloon is verstuurd in plaats van de tekst.
TEMPLATE_SENT = "sjabloon"
TEMPLATE_NOTICE = "sjabloon verstuurd; de klant moet eerst reageren"


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

    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        client: httpx.Client | None = None,
        template_name: str = "",
        template_lang: str = "nl",
        on_template: Callable[[Message, str], None] | None = None,
    ):
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._client = client or httpx.Client(timeout=20)
        self.template_name = template_name.strip()
        self.template_lang = template_lang.strip() or "nl"
        # Wordt aangeroepen (bericht, sjabloonnaam) als het sjabloon in plaats van de tekst is verstuurd.
        self.on_template = on_template

    def fetch_new(self) -> list[Message]:
        # Berichten komen binnen via de webhook; er is niets om op te halen.
        return []

    def _post(self, payload: dict[str, Any]) -> None:
        response = self._client.post(
            f"{GRAPH_URL}/{self._phone_number_id}/messages",
            headers={"Authorization": f"Bearer {self._token}"},
            json={"messaging_product": "whatsapp", **payload},
        )
        response.raise_for_status()

    def send(self, msg: Message, body: str) -> str | None:
        """Verstuur de tekst. Buiten het venster van 24 uur gaat het sjabloon, als dat is ingesteld.

        Geeft TEMPLATE_SENT terug als het sjabloon is verstuurd; de tekst zelf is dan niet verzonden.
        """
        if datetime.now(timezone.utc) - msg.received_at <= SERVICE_WINDOW:
            self._post({"to": msg.thread_id, "type": "text", "text": {"body": body}})
            return None
        if not self.template_name:
            raise WindowClosedError(
                "servicevenster van 24 uur is verlopen; stel een goedgekeurd sjabloon in (WHATSAPP_TEMPLATE_NAME)"
            )
        self._post(
            {
                "to": msg.thread_id,
                "type": "template",
                "template": {"name": self.template_name, "language": {"code": self.template_lang}},
            }
        )
        if self.on_template is not None:
            self.on_template(msg, self.template_name)
        return TEMPLATE_SENT
