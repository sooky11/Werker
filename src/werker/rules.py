from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from email.utils import parseaddr

from werker.models import Intent, Message


@dataclass(frozen=True)
class RuleConfig:
    blocklist: frozenset[str] = field(default_factory=frozenset)
    own_addresses: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class RuleResult:
    skip: bool = False
    intent: Intent | None = None
    reason: str = ""


def sender_address(sender: str) -> str:
    return (parseaddr(sender)[1] or sender).strip().lower()


def apply_rules(
    msg: Message, config: RuleConfig, sender_intent: Callable[[str], str | None] | None = None
) -> RuleResult:
    """sender_intent zoekt een onthouden categorie voor een afzenderadres (zie de afzenderregels in de opslag)."""
    address = sender_address(msg.sender)
    if address in config.own_addresses:
        return RuleResult(skip=True, reason="afzender is een eigen adres")
    if address in config.blocklist:
        return RuleResult(intent=Intent.SPAM, reason="afzender staat op de blocklist")
    remembered = sender_intent(address) if sender_intent is not None and address else None
    if remembered:
        try:
            return RuleResult(intent=Intent(remembered), reason="onthouden categorie voor deze afzender")
        except ValueError:
            pass  # onbekende categorie in de opslag: gewoon laten classificeren
    if "list-unsubscribe" in msg.headers:
        return RuleResult(intent=Intent.NIEUWSBRIEF, reason="List-Unsubscribe-header aanwezig")
    return RuleResult()
