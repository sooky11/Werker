from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from werker.calendar_providers import GoogleCalendarProvider, NoCalendar
from werker.models import Classification, Intent, Message
from werker.pipeline import Pipeline
from werker.policy import PolicyConfig
from werker.prices import PriceBook, load_yaml
from werker.reply import TOOL_NAME, Replier
from werker.rules import RuleConfig
from werker.runtime import _calendar
from werker.scheduling import format_slots_nl, free_slots
from werker.store import Store

TZ = ZoneInfo("Europe/Amsterdam")
EXAMPLE = Path(__file__).resolve().parent.parent / "prices.example.yaml"
MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
SATURDAY = date(2026, 10, 31)
SUNDAY = date(2026, 11, 1)
HALF_HOUR = timedelta(minutes=30)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


# ---------- vrije tijdsloten ----------

def test_free_slots_skip_busy_blocks():
    busy = [(at(TUESDAY, 9), at(TUESDAY, 10)), (at(TUESDAY, 10, 30), at(TUESDAY, 11))]
    slots = free_slots(busy, time(9), time(17), HALF_HOUR, [TUESDAY], at(MONDAY, 12), tz=TZ, limit=3)
    assert [start for start, _ in slots] == [at(TUESDAY, 10), at(TUESDAY, 11), at(TUESDAY, 11, 30)]
    assert all(end - start == HALF_HOUR for start, end in slots)


def test_free_slots_respect_working_hours():
    slots = free_slots([], time(9), time(17), HALF_HOUR, [TUESDAY], at(MONDAY, 12), tz=TZ, limit=100)
    assert len(slots) == 16
    assert slots[0][0] == at(TUESDAY, 9)
    assert slots[-1] == (at(TUESDAY, 16, 30), at(TUESDAY, 17))


def test_free_slots_only_use_the_days_given():
    # Het weekend komt alleen in de slots als het in days staat; zonder dagen is er niets vrij.
    slots = free_slots([], time(9), time(17), HALF_HOUR, [MONDAY], at(SATURDAY, 8), tz=TZ, limit=3)
    assert len(slots) == 3
    assert all(start.date() == MONDAY for start, _ in slots)
    assert free_slots([], time(9), time(17), HALF_HOUR, [], at(SATURDAY, 8), tz=TZ) == []


def test_free_slots_never_start_before_now():
    slots = free_slots([], time(9), time(17), HALF_HOUR, [TUESDAY], at(TUESDAY, 10, 15), tz=TZ, limit=1)
    assert slots[0][0] == at(TUESDAY, 10, 30)


def test_free_slots_returns_fewer_when_day_is_full():
    busy = [(at(TUESDAY, 9), at(TUESDAY, 17))]
    assert free_slots(busy, time(9), time(17), HALF_HOUR, [TUESDAY], at(MONDAY, 12), tz=TZ) == []


# ---------- tekst ----------

def test_format_slots_nl_writes_weekday_and_month_in_dutch():
    slots = [(at(TUESDAY, 10), at(TUESDAY, 10, 30)), (at(SUNDAY, 16, 30), at(SUNDAY, 17))]
    assert format_slots_nl(slots) == ["dinsdag 3 november om 10:00", "zondag 1 november om 16:30"]


# ---------- concept met tijdstippen ----------

class FakeClient:
    """Neemt de aanroep van het model aan en onthoudt die."""

    def __init__(self):
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=TOOL_NAME, input={"body": "Dag", "needs_human": False, "reason": ""})]
        )


def _prices():
    book = PriceBook(lambda: load_yaml(EXAMPLE))
    book.refresh()
    return book.prices


def _message(sender: str = "Jan Jansen <jan@example.com>") -> Message:
    return Message(
        channel="mail",
        external_id="<1@x>",
        thread_id="<1@x>",
        sender=sender,
        subject="Afspraak",
        body="Kunnen we een afspraak maken?",
        received_at=datetime.now(timezone.utc),
    )


def _cls(intent: Intent = Intent.AFSPRAAK) -> Classification:
    return Classification(intent, "normaal", "nl", 0.95, "test")


def test_draft_prompt_contains_slots_when_given():
    client = FakeClient()
    slots = ["dinsdag 3 november om 10:00", "dinsdag 3 november om 11:00"]
    Replier(client).draft(_message(), _cls(), _prices(), slots=slots)
    system = client.calls[0]["system"]
    for slot in slots:
        assert f"- {slot}" in system
    assert "Stel alleen tijdstippen voor uit de onderstaande lijst" in system


def test_draft_prompt_has_no_slots_when_not_given():
    client = FakeClient()
    Replier(client).draft(_message(), _cls(), _prices())
    system = client.calls[0]["system"]
    assert "Vrije tijdstippen" not in system
    assert "Afspraken:" not in system


def test_draft_says_so_when_no_slots_are_free():
    client = FakeClient()
    Replier(client).draft(_message(), _cls(), _prices(), slots=[])
    system = client.calls[0]["system"]
    assert "geen vrije tijdstippen" in system
    assert "Vrije tijdstippen" not in system


class FakeCalendar:
    def __init__(self, busy=()):
        self._busy = list(busy)
        self.created: list[tuple] = []
        self.busy_calls: list[tuple] = []

    def busy(self, start, end):
        self.busy_calls.append((start, end))
        return [(b_start, b_end) for b_start, b_end in self._busy if b_start < end and start < b_end]

    def create_event(self, start, end, title, attendee_email):
        self.created.append((start, end, title, attendee_email))
        return f"evt-{len(self.created)}"


class FakeClassifier:
    def __init__(self, intent: Intent):
        self.intent = intent

    def classify(self, msg):
        return Classification(self.intent, "normaal", "nl", 0.95, "test")


class RecordingReplier:
    def __init__(self):
        self.slot_calls: list[object] = []

    def draft(self, msg, cls, prices, **kwargs):
        self.slot_calls.append(kwargs.get("slots", "geen"))
        return SimpleNamespace(body="Dag", needs_human=False, reason="")


class FakeConnector:
    name = "mail"

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def fetch_new(self):
        return []

    def send(self, msg, body):
        self.sent.append((msg.external_id, body))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "werker.db"))
    yield s
    s.close()


def _pipeline(store, intent, replier=None, calendar=None, connector=None):
    prices = PriceBook(lambda: load_yaml(EXAMPLE))
    prices.refresh()
    return Pipeline(
        store,
        FakeClassifier(intent),
        replier or RecordingReplier(),
        prices,
        PolicyConfig(autosend_enabled=False, max_auto_amount=Decimal("500")),
        RuleConfig(),
        {"mail": connector or FakeConnector()},
        calendar=calendar,
    )


def test_pipeline_passes_slots_for_afspraak_when_calendar_configured(store):
    replier = RecordingReplier()
    pipeline = _pipeline(store, Intent.AFSPRAAK, replier=replier, calendar=FakeCalendar())
    pipeline.process(_message())
    assert len(replier.slot_calls) == 1
    slots = replier.slot_calls[0]
    assert isinstance(slots, list) and 0 < len(slots) <= 3


def test_pipeline_passes_no_slots_without_calendar(store):
    replier = RecordingReplier()
    pipeline = _pipeline(store, Intent.AFSPRAAK, replier=replier)
    pipeline.process(_message())
    assert replier.slot_calls == ["geen"]


def test_pipeline_passes_no_slots_for_other_intents(store):
    replier = RecordingReplier()
    pipeline = _pipeline(store, Intent.PRIJSVRAAG, replier=replier, calendar=FakeCalendar())
    pipeline.process(_message())
    assert replier.slot_calls == ["geen"]


def _approved_afspraak(store, calendar, connector=None):
    pipeline = _pipeline(store, Intent.AFSPRAAK, calendar=calendar, connector=connector)
    pipeline.process(_message())
    draft_id = store.pending_drafts()[0]["id"]
    return pipeline, draft_id


def test_approval_with_slot_start_creates_exactly_one_event(store):
    calendar = FakeCalendar()
    connector = FakeConnector()
    pipeline, draft_id = _approved_afspraak(store, calendar, connector)
    pipeline.approve(draft_id, slot_start=at(TUESDAY, 10))
    assert len(connector.sent) == 1
    assert calendar.created == [(at(TUESDAY, 10), at(TUESDAY, 10, 30), "Afspraak met Jan Jansen", "jan@example.com")]


def test_approval_without_slot_start_creates_no_event(store):
    calendar = FakeCalendar()
    connector = FakeConnector()
    pipeline, draft_id = _approved_afspraak(store, calendar, connector)
    pipeline.approve(draft_id)
    assert len(connector.sent) == 1
    assert calendar.created == []


def test_approval_refuses_a_slot_that_is_already_taken(store):
    calendar = FakeCalendar(busy=[(at(TUESDAY, 10), at(TUESDAY, 11))])
    connector = FakeConnector()
    pipeline, draft_id = _approved_afspraak(store, calendar, connector)
    with pytest.raises(ValueError):
        pipeline.approve(draft_id, slot_start=at(TUESDAY, 10))
    assert connector.sent == []
    assert calendar.created == []
    assert store.get_draft(draft_id)["status"] != "verzonden"


def test_slot_start_is_ignored_for_non_afspraak(store):
    calendar = FakeCalendar()
    pipeline = _pipeline(store, Intent.PRIJSVRAAG, calendar=calendar)
    pipeline.process(_message())
    pipeline.approve(store.pending_drafts()[0]["id"], slot_start=at(TUESDAY, 10))
    assert calendar.created == []


def test_failed_booking_does_not_undo_the_sent_reply(store):
    class BrokenCalendar(FakeCalendar):
        def create_event(self, *args):
            raise RuntimeError("agenda offline")

    connector = FakeConnector()
    pipeline, draft_id = _approved_afspraak(store, BrokenCalendar(), connector)
    pipeline.approve(draft_id, slot_start=at(TUESDAY, 10))
    assert len(connector.sent) == 1
    assert store.get_draft(draft_id)["status"] == "verzonden"


# ---------- Google Calendar (met een nep-service, geen netwerk) ----------

class _Request:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeCalendarService:
    def __init__(self, busy=()):
        self._busy = list(busy)
        self.queries: list[dict] = []
        self.inserts: list[tuple[str, dict, str]] = []

    def freebusy(self):
        return self

    def query(self, body):
        self.queries.append(body)
        return _Request({"calendars": {"agenda@example.com": {"busy": self._busy}}})

    def events(self):
        return self

    def insert(self, calendarId, body, sendUpdates):
        self.inserts.append((calendarId, body, sendUpdates))
        return _Request({"id": "evt-42"})


def test_google_busy_parses_blocks_and_queries_the_calendar():
    service = FakeCalendarService(busy=[{"start": "2026-11-03T09:00:00Z", "end": "2026-11-03T10:00:00+01:00"}])
    provider = GoogleCalendarProvider(service, calendar_id="agenda@example.com")
    blocks = provider.busy(at(TUESDAY, 8), at(TUESDAY, 18))
    assert blocks == [(datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc), datetime(2026, 11, 3, 10, 0, tzinfo=TZ))]
    assert service.queries[0]["items"] == [{"id": "agenda@example.com"}]


def test_google_create_event_sends_invitation_only_with_attendee():
    service = FakeCalendarService()
    provider = GoogleCalendarProvider(service, calendar_id="agenda@example.com")
    event_id = provider.create_event(at(TUESDAY, 10), at(TUESDAY, 10, 30), "Afspraak met Jan", "jan@example.com")
    assert event_id == "evt-42"
    calendar_id, body, send_updates = service.inserts[0]
    assert calendar_id == "agenda@example.com"
    assert body["start"] == {"dateTime": at(TUESDAY, 10).isoformat()}
    assert body["end"] == {"dateTime": at(TUESDAY, 10, 30).isoformat()}
    assert body["attendees"] == [{"email": "jan@example.com"}]
    assert send_updates == "all"

    service.inserts.clear()
    provider.create_event(at(TUESDAY, 10), at(TUESDAY, 10, 30), "Intern", None)
    _, body, send_updates = service.inserts[0]
    assert "attendees" not in body
    assert send_updates == "none"


def test_no_calendar_is_empty_and_refuses_to_book():
    calendar = NoCalendar()
    assert calendar.busy(at(TUESDAY, 8), at(TUESDAY, 18)) == []
    with pytest.raises(RuntimeError):
        calendar.create_event(at(TUESDAY, 10), at(TUESDAY, 10, 30), "x", None)


def test_runtime_uses_no_calendar_without_id_or_token(tmp_path):
    assert isinstance(_calendar({}), NoCalendar)
    assert isinstance(_calendar({"WERKER_CALENDAR_ID": "agenda@example.com"}), NoCalendar)
    assert isinstance(_calendar({"WERKER_CALENDAR_TOKEN": str(tmp_path / "x.json")}), NoCalendar)


def test_runtime_falls_back_to_no_calendar_when_token_is_unreadable(tmp_path):
    env = {"WERKER_CALENDAR_ID": "agenda@example.com", "WERKER_CALENDAR_TOKEN": str(tmp_path / "ontbreekt.json")}
    assert isinstance(_calendar(env), NoCalendar)
