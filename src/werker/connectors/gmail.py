from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from werker.connectors.mail import build_reply, parse_email
from werker.models import Intent, Message

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Het Gmail-id van een bericht (anders dan de Message-ID uit de mail) staat in de headers onder deze sleutel.
GMAIL_ID_HEADER = "x-werker-gmail-id"

# Labels die Werker op Gmail-berichten zet, zodat je dezelfde categorieën ook in Gmail ziet.
GMAIL_LABELS: dict[Intent, str] = {
    Intent.PRIJSVRAAG: "Werker/Prijsvraag",
    Intent.AFSPRAAK: "Werker/Afspraak",
    Intent.KLACHT: "Werker/Klacht",
    Intent.FACTUUR: "Werker/Factuur",
    Intent.BETALING: "Werker/Betaling",
    Intent.NIEUWSBRIEF: "Werker/Nieuwsbrief",
    Intent.SPAM: "Werker/Spam",
    Intent.OVERIG: "Werker/Overig",
}


def build_service(token_path: str) -> Any:
    """Bouw de Gmail-client op basis van een eerder opgeslagen OAuth-token."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    credentials = Credentials.from_authorized_user_file(token_path, SCOPES)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def login(credentials_path: str, token_path: str) -> None:
    """Eenmalige inlogstap: opent een browser en slaat het OAuth-token op in token_path.

    credentials_path is het OAuth-clientbestand (type "Desktop app") uit de Google Cloud Console.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    credentials = flow.run_local_server(port=0)
    Path(token_path).write_text(credentials.to_json(), encoding="utf-8")


def _decode_raw(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class GmailConnector:
    name = "mail:gmail"

    def __init__(self, service: Any, sender_address: str, max_results: int = 25):
        self._service = service
        self._sender = sender_address
        self._max_results = max_results
        # Label-naam -> Gmail-id. Eenmaal opgehaald of aangemaakt, daarna onthouden.
        self._label_ids: dict[str, str] = {}

    def fetch_new(self) -> list[Message]:
        users = self._service.users().messages()
        listing = users.list(userId="me", q="is:unread in:inbox", maxResults=self._max_results).execute()
        messages = []
        for ref in listing.get("messages", []):
            raw = users.get(userId="me", id=ref["id"], format="raw").execute()
            parsed = parse_email(_decode_raw(raw["raw"]), channel=self.name)
            messages.append(replace(parsed, headers={**parsed.headers, GMAIL_ID_HEADER: ref["id"]}))
        return messages

    def send(self, msg: Message, body: str) -> None:
        reply = build_reply(msg, body, self._sender)
        encoded = base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii")
        self._service.users().messages().send(userId="me", body={"raw": encoded}).execute()

    def label(self, msg: Message, label_name: str) -> None:
        """Zet het label op het Gmail-bericht. Berichten van andere kanalen worden stil overgeslagen."""
        if msg.channel != self.name:
            return
        gmail_id = msg.headers.get(GMAIL_ID_HEADER)
        if not gmail_id:
            raise ValueError(f"bericht {msg.external_id} heeft geen Gmail-id; label niet gezet")
        label_id = self._label_id(label_name)
        self._service.users().messages().modify(userId="me", id=gmail_id, body={"addLabelIds": [label_id]}).execute()

    def _label_id(self, label_name: str) -> str:
        if label_name not in self._label_ids:
            listing = self._service.users().labels().list(userId="me").execute()
            for existing in listing.get("labels", []):
                self._label_ids[existing["name"]] = existing["id"]
        if label_name not in self._label_ids:
            body = {"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
            created = self._service.users().labels().create(userId="me", body=body).execute()
            self._label_ids[label_name] = created["id"]
        return self._label_ids[label_name]


def intent_labeler(connector: GmailConnector) -> Callable[[Message, Intent], None]:
    """Labeler voor de pipeline: zet bij elke classificatie het Gmail-label van die categorie."""

    def labeler(msg: Message, intent: Intent) -> None:
        connector.label(msg, GMAIL_LABELS[intent])

    return labeler
