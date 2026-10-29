from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from werker.connectors.mail import build_reply, parse_email
from werker.models import Message

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


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

    def fetch_new(self) -> list[Message]:
        users = self._service.users().messages()
        listing = users.list(userId="me", q="is:unread in:inbox", maxResults=self._max_results).execute()
        messages = []
        for ref in listing.get("messages", []):
            raw = users.get(userId="me", id=ref["id"], format="raw").execute()
            messages.append(parse_email(_decode_raw(raw["raw"]), channel=self.name))
        return messages

    def send(self, msg: Message, body: str) -> None:
        reply = build_reply(msg, body, self._sender)
        encoded = base64.urlsafe_b64encode(reply.as_bytes()).decode("ascii")
        self._service.users().messages().send(userId="me", body={"raw": encoded}).execute()
