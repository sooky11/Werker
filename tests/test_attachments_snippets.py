import base64
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx
from fastapi.testclient import TestClient

from werker import attachments, privacy
from werker.connectors import mail
from werker.connectors.mail import parse_email
from werker.connectors.outlook import OutlookConnector
from werker.runtime import Runtime
from werker.store import Store
from werker.web import create_app

ANNA = {"name": "anna", "role": "beheerder", "password": "anna-wachtwoord"}
JAN = {"name": "jan", "role": "medewerker", "password": "jan-wachtwoord"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _raw(*files: tuple[str, str, bytes], message_id: str = "<1@x>") -> bytes:
    email = EmailMessage()
    email["From"] = "Jan Jansen <jan@example.com>"
    email["To"] = "salon@example.com"
    email["Subject"] = "Foto van mijn haar"
    email["Message-ID"] = message_id
    email["Date"] = "Tue, 27 Oct 2026 09:15:00 +0000"
    email.set_content("Zie bijlage.")
    for filename, content_type, data in files:
        maintype, subtype = content_type.split("/")
        email.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return email.as_bytes()


def _setup(tmp_path, users=None):
    store = Store(str(tmp_path / "w.db"))
    runtime = Runtime(store, tmp_path, {})
    if users is not None:
        runtime.update(users=users)
    return runtime, TestClient(create_app(runtime))


def _store_mail(runtime, *files, client_id=None, message_id="<1@x>") -> int:
    return runtime.store.add_message(parse_email(_raw(*files, message_id=message_id)), client_id=client_id)


def test_attachment_saved_and_listed_after_parsing(tmp_path):
    runtime, client = _setup(tmp_path)
    message_id = _store_mail(runtime, ("foto.png", "image/png", PNG), ("offerte.pdf", "application/pdf", b"%PDF-1.4"))

    listing = client.get(f"/api/messages/{message_id}/attachments").json()

    assert [a["filename"] for a in listing] == ["foto.png", "offerte.pdf"]
    assert listing[0]["size"] == len(PNG) and listing[0]["too_large"] is False
    assert (tmp_path / "bijlagen" / str(message_id) / "foto.png").read_bytes() == PNG
    download = client.get(listing[0]["url"])
    assert download.content == PNG


def test_oversized_attachment_recorded_as_te_groot_without_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "MAX_ATTACHMENT_BYTES", 100)
    runtime, client = _setup(tmp_path)
    message_id = _store_mail(runtime, ("klein.txt", "text/plain", b"a" * 60), ("groot.zip", "application/zip", b"b" * 60))

    small, big = client.get(f"/api/messages/{message_id}/attachments").json()

    assert small["too_large"] is False
    assert big == {**big, "filename": "groot.zip", "size": 60, "too_large": True, "status": "te groot", "url": None}
    assert sorted(p.name for p in (tmp_path / "bijlagen" / str(message_id)).iterdir()) == ["klein.txt"]
    assert client.get(f"/api/attachments/{big['id']}").status_code == 404


def test_filename_sanitizing_stays_inside_folder_and_deduplicates(tmp_path):
    assert attachments.sanitize_filename("../../etc/passwd") == "passwd"
    assert attachments.sanitize_filename("..\\..\\boot.ini") == "boot.ini"
    assert attachments.sanitize_filename(".htaccess") == "htaccess"
    assert attachments.sanitize_filename("...") == "bijlage"
    long = attachments.sanitize_filename("a" * 300 + ".pdf")
    assert len(long) == 120 and long.endswith(".pdf")

    runtime, _ = _setup(tmp_path)
    message_id = _store_mail(runtime, ("../../etc/passwd", "text/plain", b"x"), ("passwd", "text/plain", b"y"))

    rows = runtime.store.list_attachments(message_id)
    folder = (tmp_path / "bijlagen" / str(message_id)).resolve()
    assert [row["filename"] for row in rows] == ["passwd", "passwd (2)"]
    for row in rows:
        assert (tmp_path / "bijlagen" / row["path"]).resolve().parent == folder
    assert not (tmp_path / "etc").exists()
    assert attachments.resolve_file(tmp_path / "bijlagen", "../w.db") is None


def test_html_attachment_is_downloaded_as_octet_stream(tmp_path):
    runtime, client = _setup(tmp_path)
    message_id = _store_mail(
        runtime, ("aanval.html", "text/html", b"<script>alert(1)</script>"), ("logo.svg", "image/svg+xml", b"<svg/>")
    )

    for item in client.get(f"/api/messages/{message_id}/attachments").json():
        response = client.get(item["url"])
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"


def test_image_is_served_inline(tmp_path):
    runtime, client = _setup(tmp_path)
    message_id = _store_mail(runtime, ('foto "1".png', "image/png", PNG))

    (item,) = client.get(f"/api/messages/{message_id}/attachments").json()
    response = client.get(item["url"])

    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith('inline; filename="foto _1_.png"')
    assert response.headers["x-content-type-options"] == "nosniff"


def test_erase_client_and_retention_remove_attachment_files(tmp_path):
    runtime, _ = _setup(tmp_path)
    store = runtime.store
    client_id = store.create_client("Jan Jansen", [("email", "jan@example.com")])
    message_id = _store_mail(runtime, ("foto.png", "image/png", PNG), client_id=client_id)
    folder = tmp_path / "bijlagen" / str(message_id)
    assert folder.is_dir()

    privacy.erase_client(store, client_id)

    assert not folder.exists()
    assert store.list_attachments(message_id) == []

    old_id = _store_mail(runtime, ("oud.png", "image/png", PNG), message_id="<oud@x>")
    privacy.purge_expired(store, datetime(2027, 12, 1, tzinfo=timezone.utc), days=30)
    assert not (tmp_path / "bijlagen" / str(old_id)).exists()
    assert store.list_attachments(old_id) == []


def test_outlook_fetches_file_attachments():
    item = {
        "id": "G1",
        "internetMessageId": "<o@x>",
        "subject": "Bijlage",
        "from": {"emailAddress": {"address": "jan@example.com"}},
        "receivedDateTime": "2026-10-27T09:15:00Z",
        "body": {"contentType": "text", "content": "Zie bijlage"},
        "hasAttachments": True,
    }
    files = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "foto.png", "contentType": "image/png",
         "size": len(PNG), "isInline": False, "contentBytes": base64.b64encode(PNG).decode()},
        {"@odata.type": "#microsoft.graph.itemAttachment", "name": "doorgestuurd", "size": 10},
        {"@odata.type": "#microsoft.graph.fileAttachment", "name": "film.mp4", "contentType": "video/mp4",
         "size": 50 * 1024 * 1024, "isInline": False, "contentBytes": ""},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachments"):
            assert request.url.path == "/v1.0/me/messages/G1/attachments"
            return httpx.Response(200, json={"value": files})
        return httpx.Response(200, json={"value": [item]})

    connector = OutlookConnector(lambda: "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    (msg,) = connector.fetch_new()

    photo, film = msg.attachments
    assert (photo.filename, photo.data, photo.too_large) == ("foto.png", PNG, False)
    assert (film.filename, film.too_large, film.size) == ("film.mp4", True, 50 * 1024 * 1024)


def test_snippets_crud_with_limits(tmp_path):
    _, client = _setup(tmp_path)

    created = client.post("/api/snippets", json={"title": "Openingstijden", "body": "Wij zijn open van 9 tot 17."})
    assert created.status_code == 200
    snippet_id = created.json()["id"]
    assert client.get("/api/snippets").json()[0]["title"] == "Openingstijden"

    updated = client.put(f"/api/snippets/{snippet_id}", json={"title": "Open", "body": "Van 9 tot 18."})
    assert updated.json()["body"] == "Van 9 tot 18."

    assert client.post("/api/snippets", json={"title": "x" * 81, "body": "tekst"}).status_code == 400
    assert client.post("/api/snippets", json={"title": "x" * 80, "body": "y" * 2001}).status_code == 400
    assert client.post("/api/snippets", json={"title": "", "body": "tekst"}).status_code == 400
    assert client.put("/api/snippets/999", json={"title": "a", "body": "b"}).status_code == 404

    assert client.request("DELETE", f"/api/snippets/{snippet_id}", json={}).status_code == 200
    assert client.get("/api/snippets").json() == []


def test_medewerker_cannot_delete_snippet(tmp_path):
    _, client = _setup(tmp_path, users=[ANNA, JAN])
    jan = ("jan", JAN["password"])
    anna = ("anna", ANNA["password"])
    snippet_id = client.post("/api/snippets", json={"title": "Groet", "body": "Met vriendelijke groet"}, auth=jan).json()["id"]

    denied = client.request("DELETE", f"/api/snippets/{snippet_id}", json={}, auth=jan)
    assert denied.status_code == 403
    assert len(client.get("/api/snippets", auth=jan).json()) == 1

    assert client.request("DELETE", f"/api/snippets/{snippet_id}", json={}, auth=anna).status_code == 200
