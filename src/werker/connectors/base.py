from __future__ import annotations

from typing import Protocol

from werker.models import Message


class Connector(Protocol):
    name: str

    def fetch_new(self) -> list[Message]:
        """Geef berichten die nog niet verwerkt zijn. Webhook-kanalen geven een lege lijst."""
        ...

    def send(self, msg: Message, body: str) -> None:
        """Verstuur body als antwoord op msg."""
        ...
