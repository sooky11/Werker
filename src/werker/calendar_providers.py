"""Agenda's: vrije en bezette tijd opvragen en afspraken aanmaken. Google Calendar is de eerste aanbieder."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class CalendarProvider(Protocol):
    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """Bezette blokken tussen start en end."""
        ...

    def create_event(self, start: datetime, end: datetime, title: str, attendee_email: str | None) -> str:
        """Maak een afspraak aan en geef het id ervan terug."""
        ...


def build_service(token_path: str) -> Any:
    """Bouw de Google Calendar-client op basis van een eerder opgeslagen OAuth-token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def login(credentials_path: str, token_path: str) -> None:
    """Eenmalige inlogstap voor de agenda; zelfde opzet als bij Gmail, met het agenda-bereik."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    credentials = flow.run_local_server(port=0)
    Path(token_path).write_text(credentials.to_json(), encoding="utf-8")


class GoogleCalendarProvider:
    def __init__(self, service: Any, calendar_id: str = "primary"):
        self._service = service
        self._calendar_id = calendar_id

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self._calendar_id}],
        }
        response = self._service.freebusy().query(body=body).execute()
        calendar = response.get("calendars", {}).get(self._calendar_id, {})
        return [
            (datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"]))
            for block in calendar.get("busy", [])
        ]

    def create_event(self, start: datetime, end: datetime, title: str, attendee_email: str | None) -> str:
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
        event = (
            self._service.events()
            .insert(
                calendarId=self._calendar_id,
                body=body,
                # Met een klant erbij gaat er een uitnodiging uit; anders blijft het stil.
                sendUpdates="all" if attendee_email else "none",
            )
            .execute()
        )
        return event["id"]


class NoCalendar:
    """Geen agenda ingesteld: er is nooit bezette tijd en afspraken maken kan niet."""

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        return []

    def create_event(self, start: datetime, end: datetime, title: str, attendee_email: str | None) -> str:
        raise RuntimeError("er is geen agenda ingesteld")
