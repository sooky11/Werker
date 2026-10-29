from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping

from werker.connectors.base import Connector
from werker.models import Classification, Draft, Intent, Message
from werker.policy import Action, Decision, PolicyConfig, decide
from werker.prices import PriceBook, validate_prices
from werker.rules import RuleConfig, apply_rules
from werker.store import Store

log = logging.getLogger(__name__)

class SendError(RuntimeError):
    """Het verzenden is mislukt; de reden staat ook in het auditlog."""


DRAFT_INTENTS = frozenset({Intent.PRIJSVRAAG, Intent.AFSPRAAK, Intent.KLACHT, Intent.FACTUUR, Intent.OVERIG})


class Pipeline:
    def __init__(
        self,
        store: Store,
        classifier: Any,
        replier: Any,
        prices: PriceBook,
        policy: PolicyConfig,
        rules: RuleConfig,
        connectors: Mapping[str, Connector],
    ):
        self._store = store
        self._classifier = classifier
        self._replier = replier
        self.prices = prices
        self._policy = policy
        self._rules = rules
        self._connectors = connectors

    def process(self, msg: Message) -> Decision | None:
        if self._store.find_message_id(msg.channel, msg.external_id) is not None:
            return None
        try:
            message_id = self._store.add_message(msg)
        except sqlite3.IntegrityError:
            # Een andere thread heeft dit bericht net toegevoegd.
            return None

        rule = apply_rules(msg, self._rules)
        if rule.skip:
            self._store.set_status(message_id, "overgeslagen")
            self._store.audit(message_id, "overgeslagen", rule.reason)
            return Decision(Action.SKIP, rule.reason)

        if rule.intent is not None:
            cls = Classification(rule.intent, "laag", "", 1.0, rule.reason)
        else:
            cls = self._classifier.classify(msg)
        self._store.set_classification(message_id, cls)

        draft: Draft | None = None
        draft_id: int | None = None
        errors: list[str] = []
        if cls.intent in DRAFT_INTENTS:
            prices = self.prices.prices
            draft = self._replier.draft(msg, cls, prices)
            errors = validate_prices(draft.body, prices)
            draft_id = self._store.add_draft(message_id, draft, errors)

        decision = decide(cls, draft, errors, self._policy)
        self._store.audit(message_id, decision.action.value, decision.reason)

        if decision.action is Action.SEND and draft is not None and draft_id is not None:
            try:
                self._send(message_id, msg, draft_id, draft.body)
            except SendError:
                pass  # al vastgelegd in het auditlog; het concept blijft staan
        elif draft is not None:
            self._store.set_status(message_id, "concept klaar")
        else:
            self._store.set_status(message_id, "gelabeld")
        return decision

    def sync(self) -> int:
        """Ververs de prijzen en verwerk alle nieuwe berichten van de connectors. Geeft het aantal verwerkte berichten."""
        self.prices.refresh()
        processed = 0
        for connector in self._connectors.values():
            for msg in connector.fetch_new():
                if self.process(msg) is not None:
                    processed += 1
        return processed

    def approve(self, draft_id: int, body: str | None = None, force: bool = False) -> None:
        """Verstuur een concept na menselijke goedkeuring. Bij een verzendfout blijft het concept staan."""
        row = self._store.get_draft(draft_id)
        if row is None or row["status"] != "wacht op goedkeuring":
            raise LookupError(f"concept {draft_id} wacht niet op goedkeuring")
        text = body if body is not None else row["body"]
        errors = validate_prices(text, self.prices.prices)
        if errors and not force:
            raise ValueError("prijscontrole mislukt; bevestig om toch te verzenden: " + "; ".join(errors))
        self._store.set_draft_status(draft_id, "wacht op goedkeuring", body=text)
        self._send(row["message_id"], self._store.get_message(row["message_id"]), draft_id, text, by_human=True)

    def reject(self, draft_id: int) -> None:
        row = self._store.get_draft(draft_id)
        if row is None:
            raise LookupError(f"concept {draft_id} bestaat niet")
        self._store.set_draft_status(draft_id, "afgewezen")
        self._store.audit(row["message_id"], "afgewezen", f"concept {draft_id}")

    def _send(self, message_id: int, msg: Message, draft_id: int, body: str, by_human: bool = False) -> None:
        connector = self._connectors.get(msg.channel)
        try:
            if connector is None:
                raise RuntimeError(f"geen connector voor kanaal {msg.channel!r}")
            connector.send(msg, body)
        except Exception as exc:
            log.exception("verzenden mislukt voor bericht %s", message_id)
            self._store.audit(message_id, "verzending mislukt", str(exc))
            self._store.set_status(message_id, "verzending mislukt")
            raise SendError(f"verzenden mislukt: {exc}") from exc
        self._store.set_draft_status(draft_id, "verzonden")
        self._store.set_status(message_id, "beantwoord")
        self._store.audit(message_id, "verzonden", "door mens goedgekeurd" if by_human else "automatisch")
