import json
import os
import stat
from datetime import datetime, timezone

import httpx
import pytest

from werker.cli import build_connectors, outlook_connector
from werker.connectors import outlook
from werker.connectors.outlook import OutlookConnector, token_provider_from_cache
from werker.models import Message

GRAPH_HTML_MESSAGE = {
    "id": "AAMkAGI2TG93AAA=",
    "internetMessageId": "<abc123@mail.example.com>",
    "conversationId": "AAQkAGI2CONV=",
    "subject": "Wat kost kleuren?",
    "from": {"emailAddress": {"name": "Jan Jansen", "address": "jan@example.com"}},
    "receivedDateTime": "2026-10-27T09:15:00Z",
    "body": {
        "contentType": "html",
        "content": (
            "<html><head><style>p {color:red}</style></head><body>"
            "<p>Wat kost kleuren?&nbsp;Graag&amp;snel.</p><br>"
            "<div>Groeten,<br>Jan</div>"
            "<div>Op di 27 okt schreef Salon &lt;info@salon.nl&gt;:</div>"
            "<blockquote>Ons tarief is 65,00</blockquote></body></html>"
        ),
    },
}


def _connector(handler, token: str = "tok-123") -> OutlookConnector:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return OutlookConnector(lambda: token, client=client)


def _graph_message(**overrides) -> Message:
    base = dict(
        channel="mail:outlook",
        external_id="<abc123@mail.example.com>",
        thread_id="AAQkAGI2CONV=",
        sender="Jan Jansen <jan@example.com>",
        subject="Wat kost kleuren?",
        body="Wat kost kleuren?",
        received_at=datetime(2026, 10, 27, 9, 15, tzinfo=timezone.utc),
        headers={"x-graph-id": "AAMkAGI2TG93AAA="},
    )
    base.update(overrides)
    return Message(**base)


def test_fetch_new_maps_graph_message_to_werker_message():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"value": [GRAPH_HTML_MESSAGE]})

    messages = _connector(handler).fetch_new()

    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/v1.0/me/mailFolders/inbox/messages"
    assert request.url.params["$filter"] == "isRead eq false"
    assert request.url.params["$top"] == "25"
    assert request.headers["Authorization"] == "Bearer tok-123"

    (msg,) = messages
    assert msg.channel == "mail:outlook"
    assert msg.external_id == "<abc123@mail.example.com>"
    assert msg.thread_id == "AAQkAGI2CONV="
    assert msg.sender == "Jan Jansen <jan@example.com>"
    assert msg.subject == "Wat kost kleuren?"
    assert msg.received_at == datetime(2026, 10, 27, 9, 15, tzinfo=timezone.utc)
    assert msg.headers["x-graph-id"] == "AAMkAGI2TG93AAA="
    # HTML is platte tekst, de style-regels zijn weg en het citaat is afgeknipt.
    assert msg.body == "Wat kost kleuren? Graag&snel.\n\nGroeten,\nJan"


def test_fetch_new_prefers_plain_text_body_and_strips_quotes():
    text_message = {
        **GRAPH_HTML_MESSAGE,
        "id": "AAMkTEXT=",
        "internetMessageId": "<text@example.com>",
        "body": {
            "contentType": "text",
            "content": "Hoeveel kost knippen?\n\n> Ons tarief is 25,00\nOp di 27 okt schreef X:\n> oud",
        },
    }
    (msg,) = _connector(lambda r: httpx.Response(200, json={"value": [text_message]})).fetch_new()
    assert msg.body == "Hoeveel kost knippen?"


def test_fetch_new_falls_back_to_graph_id_without_internet_message_id():
    item = {**GRAPH_HTML_MESSAGE, "internetMessageId": None, "conversationId": None}
    (msg,) = _connector(lambda r: httpx.Response(200, json={"value": [item]})).fetch_new()
    assert msg.external_id == "AAMkAGI2TG93AAA="
    assert msg.thread_id == "AAMkAGI2TG93AAA="


def test_send_posts_reply_with_body_and_bearer_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    _connector(handler, token="tok-send").send(_graph_message(), "Dat is € 25,00.")

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == "/v1.0/me/messages/AAMkAGI2TG93AAA=/reply"
    assert json.loads(request.content) == {"comment": "Dat is € 25,00."}
    assert request.headers["Authorization"] == "Bearer tok-send"


def test_send_new_posts_a_fresh_mail_via_sendmail():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    _connector(handler).send_new("klant@x.nl", "Betalingsherinnering factuur 1", "Graag betalen.")

    (request,) = seen
    assert request.url.path == "/v1.0/me/sendMail"
    payload = json.loads(request.content)["message"]
    assert payload["subject"] == "Betalingsherinnering factuur 1"
    assert payload["toRecipients"] == [{"emailAddress": {"address": "klant@x.nl"}}]


def test_send_looks_up_graph_id_when_headers_were_not_stored():
    # De opslag bewaart geen headers: na goedkeuring is alleen de Message-ID nog bekend.
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"value": [{"id": "AAMkFOUND="}]})
        return httpx.Response(202)

    msg = _graph_message(headers={})
    _connector(handler).send(msg, "Antwoord")

    lookup, reply = seen
    assert lookup.url.path == "/v1.0/me/messages"
    assert lookup.url.params["$filter"] == "internetMessageId eq '<abc123@mail.example.com>'"
    assert reply.url.path == "/v1.0/me/messages/AAMkFOUND=/reply"


def test_send_without_any_graph_id_raises_clearly():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("er mag geen verzoek naar Graph gaan")

    with pytest.raises(RuntimeError, match="niet verstuurd"):
        _connector(handler).send(_graph_message(headers={}, external_id=""), "Antwoord")


@pytest.mark.parametrize("status", [401, 403])
def test_graph_auth_errors_raise_runtime_error_with_message(status):
    error = {"error": {"code": "InvalidAuthenticationToken", "message": "Access token has expired."}}

    connector = _connector(lambda r: httpx.Response(status, json=error))
    with pytest.raises(RuntimeError, match=rf"status {status}: Access token has expired"):
        connector.fetch_new()
    with pytest.raises(RuntimeError, match=rf"status {status}: Access token has expired"):
        connector.send(_graph_message(), "Antwoord")


def test_token_provider_raises_when_not_logged_in(monkeypatch, tmp_path):
    msal = pytest.importorskip("msal")

    class NoAccountApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            pass

        def get_accounts(self):
            return []

    monkeypatch.setattr(msal, "PublicClientApplication", NoAccountApp)
    provider = token_provider_from_cache("client-123", str(tmp_path / "token.json"))
    with pytest.raises(RuntimeError, match="werker outlook-login"):
        provider()


def test_token_provider_returns_silent_access_token(monkeypatch, tmp_path):
    msal = pytest.importorskip("msal")

    class SilentApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            pass

        def get_accounts(self):
            return [{"username": "jan@example.com"}]

        def acquire_token_silent(self, scopes, account=None):
            assert scopes == outlook.SCOPES
            return {"access_token": "tok-silent"}

    monkeypatch.setattr(msal, "PublicClientApplication", SilentApp)
    provider = token_provider_from_cache("client-123", str(tmp_path / "token.json"))
    assert provider() == "tok-silent"


def test_login_raises_when_msal_rejects_device_flow(monkeypatch, tmp_path):
    msal = pytest.importorskip("msal")

    class RejectingApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            assert authority == "https://login.microsoftonline.com/common"

        def initiate_device_flow(self, scopes):
            return {"error": "unauthorized_client", "error_description": "AADSTS700016: app not found"}

    monkeypatch.setattr(msal, "PublicClientApplication", RejectingApp)
    with pytest.raises(RuntimeError, match="AADSTS700016"):
        outlook.login("client-123", str(tmp_path / "token.json"))


def test_login_raises_when_user_does_not_complete_sign_in(monkeypatch, tmp_path, capsys):
    msal = pytest.importorskip("msal")

    class FailingApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            pass

        def initiate_device_flow(self, scopes):
            return {"user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow):
            return {"error": "authorization_declined", "error_description": "The user declined."}

    monkeypatch.setattr(msal, "PublicClientApplication", FailingApp)
    with pytest.raises(RuntimeError, match="Inloggen bij Microsoft is mislukt"):
        outlook.login("client-123", str(tmp_path / "token.json"))
    assert "ABCD-EFGH" in capsys.readouterr().out


def test_login_writes_token_cache_readable_by_owner_only(monkeypatch, tmp_path):
    msal = pytest.importorskip("msal")

    class SucceedingApp:
        def __init__(self, client_id, authority=None, token_cache=None):
            pass

        def initiate_device_flow(self, scopes):
            return {"user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow):
            return {"access_token": "tok-login"}

    monkeypatch.setattr(msal, "PublicClientApplication", SucceedingApp)
    path = tmp_path / "werker-outlook-token.json"
    outlook.login("client-123", str(path))
    assert path.exists()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_client_id_means_no_outlook_connector(tmp_path):
    assert outlook_connector({"WERKER_DB": str(tmp_path / "werker.db")}) is None


def test_client_id_without_token_cache_means_no_outlook_connector(tmp_path):
    env = {"WERKER_DB": str(tmp_path / "werker.db"), "WERKER_OUTLOOK_CLIENT_ID": "client-123"}
    assert outlook_connector(env) is None


def test_token_cache_defaults_next_to_database_and_registers_connector(tmp_path):
    (tmp_path / "werker-outlook-token.json").write_text("{}", encoding="utf-8")
    env = {
        "WERKER_DB": str(tmp_path / "werker.db"),
        "WERKER_OUTLOOK_CLIENT_ID": "client-123",
        "WERKER_IMESSAGE": "false",
    }
    connector = outlook_connector(env)
    assert isinstance(connector, OutlookConnector)
    assert connector.name == "mail:outlook"

    connectors = build_connectors(env)
    assert isinstance(connectors["mail:outlook"], OutlookConnector)


def test_explicit_token_cache_path_is_used(tmp_path):
    cache = tmp_path / "custom" / "tokens.json"
    cache.parent.mkdir()
    cache.write_text("{}", encoding="utf-8")
    env = {"WERKER_OUTLOOK_CLIENT_ID": "client-123", "WERKER_OUTLOOK_TOKEN_CACHE": str(cache)}
    assert outlook_connector(env) is not None
