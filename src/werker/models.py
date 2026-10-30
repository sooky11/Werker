from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Intent(StrEnum):
    PRIJSVRAAG = "prijsvraag"
    AFSPRAAK = "afspraak"
    KLACHT = "klacht"
    FACTUUR = "factuur"
    BETALING = "betaling"
    NIEUWSBRIEF = "nieuwsbrief"
    SPAM = "spam"
    OVERIG = "overig"


@dataclass(frozen=True)
class Attachment:
    """Een bijlage bij een bericht. Te grote bijlagen hebben geen inhoud, alleen naam en grootte."""

    filename: str
    content_type: str
    data: bytes = field(default=b"", repr=False)
    size: int = 0
    too_large: bool = False


@dataclass(frozen=True)
class Message:
    channel: str
    external_id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    headers: dict[str, str] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class Classification:
    intent: Intent
    urgency: str
    language: str
    confidence: float
    summary: str


@dataclass(frozen=True)
class Draft:
    body: str
    needs_human: bool
    reason: str
