from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from werker.connectors.mail import _strip_quotes
from werker.models import Message

AUTHORITY = "https://login.microsoftonline.com/common"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read", "Mail.Send", "offline_access"]
INBOX_FIELDS = "id,internetMessageId,conversationId,subject,from,receivedDateTime,body"


def _load_cache(path: Path) -> Any:
    """Lees de tokencache van schijf. Een ontbrekend bestand is een lege cache."""
    import msal

    cache = msal.SerializableTokenCache()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def _save_cache(path: Path, cache: Any) -> None:
    """Schrijf de tokencache. Tokens zijn geheim: het bestand is alleen voor de eigenaar leesbaar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(cache.serialize())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows kent geen Unix-rechten.


def login(client_id: str, token_cache_path: str) -> None:
    """Eenmalige inlogstap met de apparaatcode-flow. Slaat de tokens op in token_cache_path."""
    import msal

    path = Path(token_cache_path)
    cache = _load_cache(path)
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(
            "Microsoft kon geen inlogcode aanvragen: "
            f"{flow.get('error_description') or flow.get('error') or 'onbekende fout'}. "
            "Controleer de client-ID en of de app 'openbare client' en apparaatcode-inloggen toestaat."
        )
    print(f"Ga naar {flow['verification_uri']} en voer de code {flow['user_code']} in.")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(
            "Inloggen bij Microsoft is mislukt: "
            f"{result.get('error_description') or result.get('error') or 'onbekende fout'}"
        )
    _save_cache(path, cache)


def token_provider_from_cache(client_id: str, token_cache_path: str) -> Callable[[], str]:
    """Geef een functie die telkens een geldig toegangstoken teruggeeft, vernieuwd uit de tokencache."""

    def provider() -> str:
        import msal

        path = Path(token_cache_path)
        cache = _load_cache(path)
        app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
        accounts = app.get_accounts()
        if not accounts:
            raise RuntimeError("Outlook is nog niet ingelogd. Draai 'werker outlook-login'.")
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if cache.has_state_changed:
            _save_cache(path, cache)  # msal roteert de refresh-token bij elke vernieuwing.
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description") or "geen geldig token"
            raise RuntimeError(f"Outlook-token vernieuwen is mislukt ({detail}). Draai 'werker outlook-login' opnieuw.")
        return result["access_token"]

    return provider


class OutlookConnector:
    """Leest ongelezen mail uit de Outlook-inbox en antwoordt via Microsoft Graph."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        name: str = "mail:outlook",
        client: httpx.Client | None = None,
    ):
        self._token_provider = token_provider
        self.name = name
        self._client = client or httpx.Client(timeout=30)

    def fetch_new(self) -> list[Message]:
        params = {"$filter": "isRead eq false", "$top": "25", "$select": INBOX_FIELDS}
        data = self._request("GET", "/me/mailFolders/inbox/messages", params=params)
        return [self._to_message(item) for item in data.get("value", [])]

    def send(self, msg: Message, body: str) -> None:
        graph_id = msg.headers.get("x-graph-id") or self._find_graph_id(msg.external_id)
        if not graph_id:
            raise RuntimeError(f"Het bericht {msg.external_id!r} is in Outlook niet meer terug te vinden; er is niet verstuurd.")
        self._request("POST", f"/me/messages/{quote(graph_id, safe='')}/reply", json={"comment": body})

    def send_new(self, to: str, subject: str, body: str) -> None:
        """Verstuur een nieuwe mail (geen antwoord op een bericht), bijvoorbeeld een betalingsherinnering."""
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        self._request("POST", "/me/sendMail", json={"message": message, "saveToSentItems": True})

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider()}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, f"{GRAPH_BASE}{path}", headers=self._headers(), **kwargs)
        if not response.is_success:
            raise RuntimeError(f"Microsoft Graph gaf status {response.status_code}: {_graph_error(response)}")
        return response.json() if response.content else {}

    def _find_graph_id(self, internet_message_id: str) -> str | None:
        # De opslag bewaart geen headers, dus bij goedkeuren zoeken we het Graph-id op via de Message-ID.
        if not internet_message_id:
            return None
        escaped = internet_message_id.replace("'", "''")
        data = self._request("GET", "/me/messages", params={"$filter": f"internetMessageId eq '{escaped}'", "$select": "id", "$top": "1"})
        values = data.get("value", [])
        return values[0]["id"] if values else None

    def _to_message(self, item: dict[str, Any]) -> Message:
        graph_id = item["id"]
        external_id = item.get("internetMessageId") or graph_id
        body = item.get("body") or {}
        content = body.get("content") or ""
        text = content if body.get("contentType") == "text" else _html_to_text(content)
        return Message(
            channel=self.name,
            external_id=external_id,
            thread_id=item.get("conversationId") or external_id,
            sender=_format_sender(item.get("from")),
            subject=item.get("subject") or "",
            body=_strip_quotes(text),
            received_at=_received_at(item.get("receivedDateTime")),
            headers={"x-graph-id": graph_id},
        )


def _graph_error(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:200]


def _html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1\s*>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [re.sub(r"[ \t\r\f\v\xa0]+", " ", html.unescape(line)).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _format_sender(from_field: dict[str, Any] | None) -> str:
    address_book = (from_field or {}).get("emailAddress") or {}
    address = address_book.get("address") or ""
    name = address_book.get("name") or ""
    if name and address:
        return f"{name} <{address}>"
    return address or name


def _received_at(value: str | None) -> datetime:
    if value:
        # Graph levert soms meer decimalen dan Python leest; die kappen we af op microseconden.
        cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
