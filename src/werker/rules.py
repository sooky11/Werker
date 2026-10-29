from __future__ import annotations

from dataclasses import dataclass, field
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


def apply_rules(msg: Message, config: RuleConfig) -> RuleResult:
    address = sender_address(msg.sender)
    if address in config.own_addresses:
        return RuleResult(skip=True, reason="afzender is een eigen adres")
    if address in config.blocklist:
        return RuleResult(intent=Intent.SPAM, reason="afzender staat op de blocklist")
    if "list-unsubscribe" in msg.headers:
        return RuleResult(intent=Intent.NIEUWSBRIEF, reason="List-Unsubscribe-header aanwezig")
    return RuleResult()
