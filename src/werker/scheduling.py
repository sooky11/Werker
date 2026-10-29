"""Vrije tijdsloten zoeken en die in gewone Nederlandse tekst zetten. Puur rekenwerk, zonder netwerk."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

WEEKDAYS_NL = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
MONTHS_NL = (
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
)


def free_slots(
    busy: list[tuple[datetime, datetime]],
    day_start: time,
    day_end: time,
    duration: timedelta,
    days: list[date],
    now: datetime,
    step: timedelta = timedelta(minutes=30),
    tz: tzinfo | None = None,
    limit: int = 3,
) -> list[tuple[datetime, datetime]]:
    """Geef de eerste `limit` vrije slots van `duration` binnen de werkuren, na `now`.

    Slots beginnen op `day_start` en lopen daarna in stappen van `step`. Een slot telt niet mee
    als het overlapt met een bezet blok of als het na `day_end` eindigt. Dagen die niet in `days`
    staan (zoals weekenden) worden overgeslagen. `busy` en `now` moeten timezone-aware zijn.
    """
    if duration <= timedelta(0) or step <= timedelta(0):
        raise ValueError("duur en stap moeten positief zijn")
    slots: list[tuple[datetime, datetime]] = []
    for day in sorted(set(days)):
        if len(slots) >= limit:
            break
        window_start = datetime.combine(day, day_start, tzinfo=tz)
        window_end = datetime.combine(day, day_end, tzinfo=tz)
        start = window_start
        while start + duration <= window_end and len(slots) < limit:
            end = start + duration
            if start >= now and not any(b_start < end and start < b_end for b_start, b_end in busy):
                slots.append((start, end))
            start += step
    return slots


def format_slot_nl(start: datetime) -> str:
    """Bijvoorbeeld 'dinsdag 3 november om 10:00'. Gebruikt de klok van het tijdstip zelf."""
    return (
        f"{WEEKDAYS_NL[start.weekday()]} {start.day} {MONTHS_NL[start.month - 1]}"
        f" om {start.hour:02d}:{start.minute:02d}"
    )


def format_slots_nl(slots: list[tuple[datetime, datetime]]) -> list[str]:
    return [format_slot_nl(start) for start, _ in slots]
