from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from werker.clients import resolve_client
from werker.connectors.base import Connector
from werker.models import Classification, Draft, Intent, Message
from werker.policy import Action, Decision, PolicyConfig, decide
from werker.prices import Price, PriceBook, validate_prices
from werker.reply import RevisionError
from werker.rules import RuleConfig, apply_rules
from werker.store import SCHEDULED, WAITING, Store

log = logging.getLogger(__name__)

DRAFT_INTENTS = frozenset({Intent.PRIJSVRAAG, Intent.AFSPRAAK, Intent.KLACHT, Intent.FACTUUR, Intent.OVERIG})


class SendError(RuntimeError):
    """Het verzenden is mislukt; de reden staat ook in het auditlog."""


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
        self._connectors = dict(connectors)
        # Eén slot voor alles wat berichten of concepten verandert: voorkomt dubbel verzenden en dubbele concepten.
        self._lock = threading.RLock()

    def reconfigure(
        self,
        *,
        classifier: Any = None,
        replier: Any = None,
        prices: PriceBook | None = None,
        policy: PolicyConfig | None = None,
        rules: RuleConfig | None = None,
        connectors: Mapping[str, Connector] | None = None,
    ) -> None:
        """Pas instellingen toe zonder de app opnieuw te starten. Alleen de meegegeven onderdelen veranderen."""
        if classifier is not None:
            self._classifier = classifier
        if replier is not None:
            self._replier = replier
        if prices is not None:
            self.prices = prices
        if policy is not None:
            self._policy = policy
        if rules is not None:
            self._rules = rules
        if connectors is not None:
            self._connectors = dict(connectors)

    # ---------- binnenkomende berichten ----------

    def process(self, msg: Message) -> Decision | None:
        with self._lock:
            return self._process(msg)

    def _process(self, msg: Message) -> Decision | None:
        # Eerst mislukte berichten weghalen, zodat ze opnieuw verwerkt worden.
        self._store.reset_failed_message(msg.channel, msg.external_id)
        if self._store.find_message_id(msg.channel, msg.external_id) is not None:
            return None
        client_id = resolve_client(self._store, msg)
        try:
            message_id = self._store.add_message(msg, client_id=client_id)
        except sqlite3.IntegrityError:
            # Een andere thread heeft dit bericht net toegevoegd.
            return None
        try:
            return self._process_stored(message_id, msg)
        except Exception as exc:
            # Het bericht wordt bij de volgende synchronisatie opnieuw geprobeerd.
            self._store.set_status(message_id, "fout")
            self._store.audit(message_id, "fout", str(exc))
            raise

    def _process_stored(self, message_id: int, msg: Message) -> Decision:
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
            previous = self._store.open_draft_for_thread(msg.channel, msg.thread_id)
            change = ""
            if previous is not None:
                draft, change = self._revise(msg, previous["body"], prices)
            if draft is None:
                draft = self._replier.draft(msg, cls, prices)
            errors = validate_prices(draft.body, prices)
            draft_id = self._store.add_draft(
                message_id,
                draft,
                errors,
                revises=previous["id"] if previous is not None else None,
                change_summary=change,
            )
            if previous is not None:
                self._store.set_draft_status(previous["id"], "vervangen")
                self._store.audit(previous["message_id"], "vervangen", f"door concept {draft_id}")

        decision = decide(cls, draft, errors, self._policy)
        self._store.audit(message_id, decision.action.value, decision.reason)

        if decision.action is Action.SEND and draft is not None and draft_id is not None:
            try:
                self._send(message_id, msg, draft_id, draft.body, how="automatisch")
            except SendError:
                pass  # al vastgelegd in het auditlog; het concept blijft staan
        elif draft is not None:
            self._store.set_status(message_id, "concept klaar")
        else:
            self._store.set_status(message_id, "gelabeld")
        return decision

    def _revise(self, msg: Message, current: str, prices: list[Price]) -> tuple[Draft | None, str]:
        """Pas het lopende concept gericht aan. Lukt dat niet, dan (None, "") en wordt er opnieuw geschreven."""
        try:
            revision = self._replier.revise(msg, current, prices)
        except RevisionError as exc:
            log.info("gerichte aanpassing mislukt, nieuw concept: %s", exc)
            return None, ""
        return revision.draft, revision.summary

    def sync(self) -> int:
        """Ververs de prijzen, haal nieuwe berichten op en verstuur geplande berichten die nu aan de beurt zijn."""
        self.prices.refresh()
        processed = 0
        for connector in self._connectors.values():
            try:
                messages = connector.fetch_new()
            except Exception:
                log.exception("ophalen mislukt voor %s", connector.name)
                continue
            for msg in messages:
                try:
                    if self.process(msg) is not None:
                        processed += 1
                except Exception:
                    log.exception("verwerken mislukt voor %s/%s", msg.channel, msg.external_id)
        self.send_due()
        return processed

    # ---------- goedkeuren, inplannen, afwijzen ----------

    def approve(
        self,
        draft_id: int,
        body: str | None = None,
        force: bool = False,
        send_at: datetime | None = None,
    ) -> None:
        with self._lock:
            self._approve(draft_id, body, force, send_at)

    def _approve(self, draft_id: int, body: str | None, force: bool, send_at: datetime | None) -> None:
        """Keur een concept goed. Met send_at wordt het op dat tijdstip verstuurd, anders meteen.

        Bij een verzendfout blijft het concept staan en komt de reden in een SendError.
        """
        row = self._store.get_draft(draft_id)
        if row is None or row["status"] not in (WAITING, SCHEDULED):
            raise LookupError(f"concept {draft_id} wacht niet op goedkeuring")
        text = body if body is not None else row["body"]
        errors = validate_prices(text, self.prices.prices)
        if errors and not force:
            raise ValueError("prijscontrole mislukt; bevestig om toch te verzenden: " + "; ".join(errors))
        message_id = row["message_id"]
        if send_at is not None and send_at > datetime.now(timezone.utc):
            self._store.schedule_draft(draft_id, text, send_at)
            self._store.audit(message_id, "ingepland", send_at.isoformat())
            return
        self._store.set_draft_status(draft_id, WAITING, body=text)
        self._send(message_id, self._store.get_message(message_id), draft_id, text, how="door mens goedgekeurd")

    def cancel_schedule(self, draft_id: int) -> None:
        with self._lock:
            row = self._store.get_draft(draft_id)
            if row is None or row["status"] != SCHEDULED:
                raise LookupError(f"concept {draft_id} is niet ingepland")
            self._store.unschedule_draft(draft_id)
            self._store.audit(row["message_id"], "planning geannuleerd", f"concept {draft_id}")

    def reject(self, draft_id: int) -> None:
        with self._lock:
            self._reject(draft_id)

    def _reject(self, draft_id: int) -> None:
        row = self._store.get_draft(draft_id)
        if row is None:
            raise LookupError(f"concept {draft_id} bestaat niet")
        if row["status"] not in (WAITING, SCHEDULED):
            raise ValueError(f"concept {draft_id} is al {row['status']}; het kan niet meer worden afgewezen")
        self._store.set_draft_status(draft_id, "afgewezen")
        self._store.audit(row["message_id"], "afgewezen", f"concept {draft_id}")

    def send_due(self, now: datetime | None = None) -> int:
        """Verstuur ingeplande berichten waarvan het tijdstip is aangebroken. Geeft het aantal verstuurde berichten."""
        with self._lock:
            return self._send_due(now)

    def _send_due(self, now: datetime | None) -> int:
        sent = 0
        for row in self._store.due_drafts(now or datetime.now(timezone.utc)):
            # Opnieuw ophalen: het concept kan net zijn geannuleerd of al verzonden.
            current = self._store.get_draft(row["id"])
            if current is None or current["status"] != SCHEDULED:
                continue
            message = self._store.get_message(row["message_id"])
            try:
                self._send(row["message_id"], message, row["id"], current["body"], how="ingepland verzonden")
                sent += 1
            except SendError:
                # Terug naar de wachtrij, zodat je het zelf bekijkt in plaats van dat het steeds opnieuw mislukt.
                self._store.unschedule_draft(row["id"])
                self._store.audit(row["message_id"], "planning mislukt", "terug naar wacht op goedkeuring")
        return sent

    # ---------- verzenden ----------

    def _send(self, message_id: int, msg: Message, draft_id: int, body: str, how: str) -> None:
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
        self._store.set_draft_status(draft_id, "verzonden", body=body)
        self._store.set_status(message_id, "beantwoord")
        self._store.audit(message_id, "verzonden", how)
