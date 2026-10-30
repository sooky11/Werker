from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from werker.calendar_providers import CalendarProvider, NoCalendar
from werker.clients import resolve_client
from werker.connectors.base import Connector
from werker.models import Classification, Draft, Intent, Message
from werker.policy import Action, Decision, PolicyConfig, decide
from werker.prices import Price, PriceBook, validate_prices
from werker.reply import RevisionError
from werker.rules import RuleConfig, apply_rules
from werker.scheduling import format_slots_nl, free_slots
from werker.store import SCHEDULED, WAITING, Store

log = logging.getLogger(__name__)

DRAFT_INTENTS = frozenset({Intent.PRIJSVRAAG, Intent.AFSPRAAK, Intent.KLACHT, Intent.FACTUUR, Intent.OVERIG})

# Afspraken: werkuren en de lengte van een afspraak, in de tijdzone van het bedrijf.
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
WORK_START = time(9, 0)
WORK_END = time(17, 0)
APPOINTMENT_LENGTH = timedelta(minutes=30)
SLOT_COUNT = 3
SLOT_WINDOW_DAYS = 7


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
        payments: Any = None,
        calendar: CalendarProvider | None = None,
        labeler: Any = None,
        signatures: Mapping[str, str] | None = None,
    ):
        self._store = store
        # Ondertekening per kanaal (kanaalnaam -> tekst); wordt bij verzenden onder het bericht gezet.
        self.signatures: dict[str, str] = dict(signatures or {})
        # Agenda voor afspraken. Met NoCalendar worden er geen tijdstippen voorgesteld en geen afspraken gemaakt.
        self.calendar: CalendarProvider = calendar if calendar is not None else NoCalendar()
        # Functie(body, client_id) -> lijst met nummers van facturen die daardoor betaald zijn.
        self._payments = payments
        # Functie(msg, intent) die het bericht in de bronmailbox labelt. Fouten worden gelogd, nooit doorgegeven.
        self._labeler = labeler
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
        payments: Any = None,
        calendar: CalendarProvider | None = None,
        labeler: Any = None,
        signatures: Mapping[str, str] | None = None,
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
        if payments is not None:
            self._payments = payments
        if calendar is not None:
            self.calendar = calendar
        if labeler is not None:
            self._labeler = labeler
        if signatures is not None:
            self.signatures = dict(signatures)

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
        rule = apply_rules(msg, self._rules, self._store.sender_rule_intent)
        if rule.skip:
            self._store.set_status(message_id, "overgeslagen")
            self._store.audit(message_id, "overgeslagen", rule.reason)
            return Decision(Action.SKIP, rule.reason)

        if rule.intent is not None:
            cls = Classification(rule.intent, "laag", "", 1.0, rule.reason)
        else:
            cls = self._classifier.classify(msg)
        self._store.set_classification(message_id, cls)
        self._label(msg, cls.intent)

        if cls.intent is Intent.BETALING:
            return self._handle_payment(message_id, msg, cls)

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
                draft = self._new_draft(msg, cls, prices)
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

    def _new_draft(self, msg: Message, cls: Classification, prices: list[Price]) -> Draft:
        slots = self._proposed_slots() if cls.intent is Intent.AFSPRAAK else None
        if slots is None:
            return self._replier.draft(msg, cls, prices)
        return self._replier.draft(msg, cls, prices, slots=slots)

    def _calendar_configured(self) -> bool:
        return not isinstance(self.calendar, NoCalendar)

    def _proposed_slots(self) -> list[str] | None:
        """Vrije tijdstippen voor de komende week, als tekst. None als er geen agenda is of die niet te lezen is."""
        if not self._calendar_configured():
            return None
        now = datetime.now(timezone.utc)
        today = now.astimezone(LOCAL_TZ).date()
        # Alleen werkdagen: in het weekend worden geen afspraken voorgesteld.
        days: list[date] = [
            day for day in (today + timedelta(days=i) for i in range(SLOT_WINDOW_DAYS)) if day.weekday() < 5
        ]
        try:
            busy = self.calendar.busy(now, now + timedelta(days=SLOT_WINDOW_DAYS + 1))
        except Exception:
            log.exception("agenda niet te lezen; het concept krijgt geen tijdstippen")
            return None
        slots = free_slots(busy, WORK_START, WORK_END, APPOINTMENT_LENGTH, days, now, tz=LOCAL_TZ, limit=SLOT_COUNT)
        return format_slots_nl(slots)
    def _label(self, msg: Message, intent: Intent) -> None:
        if self._labeler is None:
            return
        try:
            self._labeler(msg, intent)
        except Exception:
            log.exception("label zetten mislukt voor %s/%s", msg.channel, msg.external_id)

    def _handle_payment(self, message_id: int, msg: Message, cls: Classification) -> Decision:
        row = self._store.get_message_row(message_id)
        matched = self._payments(msg.body, row["client_id"]) if self._payments is not None else []
        if matched:
            self._store.set_status(message_id, "betaling verwerkt")
            decision = Decision(Action.LABEL, f"factuur {', '.join(matched)} op betaald gezet")
        else:
            self._store.set_status(message_id, "betaling gemeld")
            decision = Decision(Action.LABEL, "betaling gemeld; geen eenduidige factuur")
        self._store.audit(message_id, decision.action.value, decision.reason)
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
        slot_start: datetime | None = None,
        by: str | None = None,
        undo_seconds: int = 0,
    ) -> dict[str, Any]:
        """Geeft {"status", "draft_id", "send_at", "undo_until"}; undo_until is None als er geen bedenktijd is.

        Met undo_seconds > 0 wordt "nu verzenden" zoveel seconden later verstuurd. Tot dan kan het
        met cancel_schedule ongedaan worden gemaakt.
        """
        with self._lock:
            return self._approve(draft_id, body, force, send_at, slot_start, by, undo_seconds)

    def _approve(
        self,
        draft_id: int,
        body: str | None,
        force: bool,
        send_at: datetime | None,
        slot_start: datetime | None,
        by: str | None,
        undo_seconds: int = 0,
    ) -> dict[str, Any]:
        """Keur een concept goed. Met send_at wordt het op dat tijdstip verstuurd, anders meteen.

        Bij een afspraak met slot_start wordt daarna de afspraak in de agenda gezet.
        Bij een verzendfout blijft het concept staan en komt de reden in een SendError.
        by is de ingelogde gebruiker; die komt in het auditlog.
        """
        row = self._store.get_draft(draft_id)
        if row is None or row["status"] not in (WAITING, SCHEDULED):
            raise LookupError(f"concept {draft_id} wacht niet op goedkeuring")
        text = body if body is not None else row["body"]
        errors = validate_prices(text, self.prices.prices)
        if errors and not force:
            raise ValueError("prijscontrole mislukt; bevestig om toch te verzenden: " + "; ".join(errors))
        message_id = row["message_id"]
        msg = self._store.get_message(message_id)
        start = self._appointment_start(message_id, slot_start)
        if start is not None and self._slot_taken(start):
            raise ValueError("dit tijdstip is inmiddels bezet; kies een ander tijdstip")
        suffix = f" door {by}" if by else ""
        now = datetime.now(timezone.utc)
        result: dict[str, Any] = {"draft_id": draft_id, "send_at": None, "undo_until": None}
        if send_at is not None and send_at > now:
            self._store.schedule_draft(draft_id, text, send_at)
            self._store.audit(message_id, "ingepland", send_at.isoformat() + suffix)
            result.update(status=SCHEDULED, send_at=send_at.isoformat())
        elif undo_seconds > 0:
            # Bedenktijd: het bericht gaat over een paar seconden; tot dan kan het ongedaan worden gemaakt.
            until = now + timedelta(seconds=undo_seconds)
            self._store.schedule_draft(draft_id, text, until)
            self._store.audit(
                message_id, "ingepland", f"verzending over {undo_seconds} s, ongedaan maken kan tot {until.isoformat()}{suffix}"
            )
            result.update(status=SCHEDULED, send_at=until.isoformat(), undo_until=until.isoformat())
        else:
            self._store.set_draft_status(draft_id, WAITING, body=text)
            self._send(message_id, msg, draft_id, text, how="door mens goedgekeurd" + suffix)
            result.update(status="verzonden")
        if start is not None:
            self._book(message_id, msg, start)
        return result

    def _appointment_start(self, message_id: int, slot_start: datetime | None) -> datetime | None:
        """Het begin van de afspraak die in de agenda komt, of None als er niets geboekt wordt."""
        if slot_start is None or not self._calendar_configured():
            return None
        row = self._store.get_message_row(message_id)
        if row is None or row["intent"] != Intent.AFSPRAAK.value:
            return None
        if slot_start.tzinfo is None:
            slot_start = slot_start.replace(tzinfo=LOCAL_TZ)
        return slot_start

    def _slot_taken(self, start: datetime) -> bool:
        return bool(self.calendar.busy(start, start + APPOINTMENT_LENGTH))

    def _book(self, message_id: int, msg: Message, start: datetime) -> None:
        """Zet de afspraak in de agenda. Het bericht is dan al verstuurd of ingepland; een fout hier wordt alleen vastgelegd."""
        name, address = parseaddr(msg.sender)
        title = f"Afspraak met {name or address or msg.sender}"
        try:
            event_id = self.calendar.create_event(
                start, start + APPOINTMENT_LENGTH, title, address if "@" in address else None
            )
        except Exception as exc:
            log.exception("afspraak inplannen mislukt voor bericht %s", message_id)
            self._store.audit(message_id, "agenda mislukt", str(exc))
            return
        self._store.audit(message_id, "afspraak ingepland", f"{start.isoformat()} ({event_id})")

    def cancel_schedule(self, draft_id: int, by: str | None = None) -> None:
        with self._lock:
            row = self._store.get_draft(draft_id)
            if row is None or row["status"] != SCHEDULED:
                raise LookupError(f"concept {draft_id} is niet ingepland")
            self._store.unschedule_draft(draft_id)
            detail = f"concept {draft_id}" + (f" door {by}" if by else "")
            self._store.audit(row["message_id"], "planning geannuleerd", detail)

    def reject(self, draft_id: int, by: str | None = None) -> None:
        with self._lock:
            self._reject(draft_id, by)

    def _reject(self, draft_id: int, by: str | None) -> None:
        row = self._store.get_draft(draft_id)
        if row is None:
            raise LookupError(f"concept {draft_id} bestaat niet")
        if row["status"] not in (WAITING, SCHEDULED):
            raise ValueError(f"concept {draft_id} is al {row['status']}; het kan niet meer worden afgewezen")
        self._store.set_draft_status(draft_id, "afgewezen")
        detail = f"concept {draft_id}" + (f" door {by}" if by else "")
        self._store.audit(row["message_id"], "afgewezen", detail)

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
            body = self._with_signature(msg.channel, body)
            connector.send(msg, body)
        except Exception as exc:
            log.exception("verzenden mislukt voor bericht %s", message_id)
            self._store.audit(message_id, "verzending mislukt", str(exc))
            self._store.set_status(message_id, "verzending mislukt")
            raise SendError(f"verzenden mislukt: {exc}") from exc
        self._store.set_draft_status(draft_id, "verzonden", body=body)
        self._store.set_status(message_id, "beantwoord")
        self._store.audit(message_id, "verzonden", how)

    def _with_signature(self, channel: str, body: str) -> str:
        """Zet de ondertekening van dit kanaal onder het bericht, tenzij die er al staat."""
        signature = (self.signatures.get(channel) or "").strip()
        if not signature or body.rstrip().endswith(signature):
            return body
        return body.rstrip() + "\n\n" + signature
