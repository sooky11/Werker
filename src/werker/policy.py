from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from werker.models import Classification, Draft, Intent
from werker.prices import find_amounts


class Action(StrEnum):
    SKIP = "skip"
    LABEL = "label"
    DRAFT = "draft"
    SEND = "send"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


@dataclass(frozen=True)
class PolicyConfig:
    autosend_enabled: bool = False
    autosend_intents: frozenset[Intent] = frozenset({Intent.PRIJSVRAAG})
    min_confidence: float = 0.85
    max_auto_amount: Decimal = Decimal("500")


ALWAYS_HUMAN = frozenset({Intent.KLACHT, Intent.FACTUUR})


def decide(
    cls: Classification,
    draft: Draft | None,
    price_errors: list[str],
    config: PolicyConfig,
) -> Decision:
    if draft is None:
        return Decision(Action.LABEL, "geen concept nodig")
    if cls.intent in ALWAYS_HUMAN:
        return Decision(Action.DRAFT, f"{cls.intent.value}: altijd handmatig")
    if cls.confidence < config.min_confidence:
        return Decision(Action.DRAFT, "lage zekerheid van de classificatie")
    if price_errors:
        return Decision(Action.DRAFT, "; ".join(price_errors))
    if draft.needs_human:
        return Decision(Action.DRAFT, draft.reason or "model vraagt om een mens")
    if max(find_amounts(draft.body), default=Decimal(0)) > config.max_auto_amount:
        return Decision(Action.DRAFT, "bedrag boven de automatische drempel")
    if not config.autosend_enabled:
        return Decision(Action.DRAFT, "auto-verzenden staat uit")
    if cls.intent not in config.autosend_intents:
        return Decision(Action.DRAFT, f"auto-verzenden niet ingeschakeld voor {cls.intent.value}")
    return Decision(Action.SEND, f"{cls.intent.value} met gevalideerde prijs")
