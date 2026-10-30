import base64
import json

import pytest
from fastapi.testclient import TestClient

from werker.auth import LoginThrottle
from werker.invoice_formats import parse_invoice
from werker.peppol import InvoiceError, parse_ubl
from werker.runtime import Runtime
from werker.store import Store
from werker.web import MAX_UPLOAD_BYTES, create_app

ANNA = {"name": "anna", "role": "beheerder", "password": "anna-wachtwoord"}
JAN = {"name": "jan", "role": "medewerker", "password": "jan-wachtwoord"}
ANNA_AUTH = ("anna", "anna-wachtwoord")
JAN_AUTH = ("jan", "jan-wachtwoord")


def _runtime(tmp_path, users=None) -> Runtime:
    runtime = Runtime(Store(str(tmp_path / "w.db")), tmp_path, {})
    if users is not None:
        runtime.update(users=users)
    return runtime


def _client(app, ip="10.0.0.1") -> TestClient:
    return TestClient(app, client=(ip, 50000))


# ---------- inlogpogingen beperken ----------

def test_sixth_wrong_login_is_blocked_even_with_right_password(tmp_path):
    app = create_app(_runtime(tmp_path, users=[ANNA, JAN]))
    attacker = _client(app)

    for _ in range(5):
        assert attacker.get("/api/stats", auth=("anna", "fout-wachtwoord")).status_code == 401
    blocked = attacker.get("/api/stats", auth=("anna", "fout-wachtwoord"))
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 600
    assert "inlogpogingen" in blocked.json()["detail"]

    assert attacker.get("/api/stats", auth=ANNA_AUTH).status_code == 429

    other = _client(app, ip="10.0.0.2")
    assert other.get("/api/stats", auth=ANNA_AUTH).status_code == 200


def test_legacy_password_is_also_throttled(tmp_path):
    app = create_app(_runtime(tmp_path), ui_password="geheim-wachtwoord")
    attacker = _client(app)
    for _ in range(5):
        assert attacker.get("/api/stats", auth=("x", "fout")).status_code == 401
    assert attacker.get("/api/stats", auth=("x", "geheim-wachtwoord")).status_code == 429
    assert _client(app, ip="10.0.0.9").get("/api/stats", auth=("x", "geheim-wachtwoord")).status_code == 200


def test_x_forwarded_for_does_not_escape_the_block(tmp_path):
    app = create_app(_runtime(tmp_path), ui_password="geheim-wachtwoord")
    attacker = _client(app)
    for i in range(5):
        attacker.get("/api/stats", auth=("x", "fout"), headers={"X-Forwarded-For": f"1.2.3.{i}"})
    response = attacker.get("/api/stats", auth=("x", "fout"), headers={"X-Forwarded-For": "9.9.9.9"})
    assert response.status_code == 429


def test_requests_without_credentials_do_not_count(tmp_path):
    app = create_app(_runtime(tmp_path), ui_password="geheim-wachtwoord")
    browser = _client(app)
    for _ in range(10):
        assert browser.get("/").status_code == 401
    assert browser.get("/api/stats", auth=("x", "geheim-wachtwoord")).status_code == 200


def test_throttle_window_passes_and_size_is_bounded():
    now = [1000.0]
    throttle = LoginThrottle(max_failures=5, window=600, limit=3, clock=lambda: now[0])
    for _ in range(5):
        throttle.record_failure("a")
    assert throttle.retry_after("a") == 600
    now[0] += 599
    assert throttle.retry_after("a") == 1
    now[0] += 1
    assert throttle.retry_after("a") == 0

    for ip in ("b", "c", "d", "e"):
        throttle.record_failure(ip)
    assert len(throttle._failures) <= 3


# ---------- beveiligingsheaders ----------

@pytest.mark.parametrize("path", ["/", "/api/stats"])
def test_security_headers_on_every_response(tmp_path, path):
    client = _client(create_app(_runtime(tmp_path)))
    response = client.get(path)
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "img-src 'self' data: blob:" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_security_headers_on_error_responses(tmp_path):
    client = _client(create_app(_runtime(tmp_path), ui_password="geheim-wachtwoord"))
    response = client.get("/api/stats")
    assert response.status_code == 401
    assert response.headers["x-frame-options"] == "DENY"


# ---------- AVG-routes alleen voor beheerders ----------

def _with_client(runtime: Runtime) -> int:
    return runtime.store.create_client("Jan Jansen", [("email", "jan@example.com")])


def test_medewerker_cannot_export_erase_or_read_privacy_log(tmp_path):
    runtime = _runtime(tmp_path, users=[ANNA, JAN])
    client_id = _with_client(runtime)
    client = _client(create_app(runtime))

    assert client.get(f"/api/clients/{client_id}/export", auth=JAN_AUTH).status_code == 403
    erase = client.request("DELETE", f"/api/clients/{client_id}", json={"confirm": "Jan Jansen"}, auth=JAN_AUTH)
    assert erase.status_code == 403
    assert "beheerder" in erase.json()["detail"]
    assert runtime.store.get_client(client_id) is not None
    assert client.get("/api/privacy/log", auth=JAN_AUTH).status_code == 403


def test_beheerder_can_export_erase_and_read_privacy_log(tmp_path):
    runtime = _runtime(tmp_path, users=[ANNA, JAN])
    client_id = _with_client(runtime)
    client = _client(create_app(runtime))

    assert client.get(f"/api/clients/{client_id}/export", auth=ANNA_AUTH).status_code == 200
    erase = client.request("DELETE", f"/api/clients/{client_id}", json={"confirm": "Jan Jansen"}, auth=ANNA_AUTH)
    assert erase.status_code == 200
    log = client.get("/api/privacy/log", auth=ANNA_AUTH)
    assert log.status_code == 200
    assert len(log.json()) == 1


def test_legacy_mode_keeps_privacy_routes_open(tmp_path):
    runtime = _runtime(tmp_path)
    client_id = _with_client(runtime)
    client = _client(create_app(runtime, ui_password="geheim-wachtwoord"))
    auth = ("x", "geheim-wachtwoord")

    assert client.get(f"/api/clients/{client_id}/export", auth=auth).status_code == 200
    assert client.get("/api/privacy/log", auth=auth).status_code == 200
    erase = client.request("DELETE", f"/api/clients/{client_id}", json={"confirm": "Jan Jansen"}, auth=auth)
    assert erase.status_code == 200


# ---------- XML-aanvallen ----------

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">&lol4;</Invoice>"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE Invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"><cbc:ID>&xxe;</cbc:ID></Invoice>"""


@pytest.mark.parametrize("document", [BILLION_LAUGHS, EXTERNAL_ENTITY])
def test_dangerous_xml_is_refused_as_invoice_error(document):
    with pytest.raises(InvoiceError) as ubl:
        parse_ubl(document)
    assert "niet toegestaan" in ubl.value.problems[0]
    with pytest.raises(InvoiceError) as any_format:
        parse_invoice(document, filename="factuur.xml")
    assert "niet toegestaan" in any_format.value.problems[0]


def test_dangerous_xml_via_web_is_422(tmp_path):
    client = _client(create_app(_runtime(tmp_path)))
    response = client.post("/api/invoices/import", json={"xml": EXTERNAL_ENTITY.decode()})
    assert response.status_code == 422
    assert "niet toegestaan" in response.json()["detail"]["problems"][0]


# ---------- maximale grootte ----------

BIG = MAX_UPLOAD_BYTES + 1024 * 1024  # 11 MB


def test_eleven_mb_import_is_refused(tmp_path):
    client = _client(create_app(_runtime(tmp_path)))
    response = client.post("/api/invoices/import", json={"xml": "<a>" + "x" * BIG + "</a>"})
    assert response.status_code == 413
    assert "10 MB" in response.json()["detail"]


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/invoices/validate", {"xml": "x" * BIG}),
        ("/api/invoices/bank", {"csv": "x" * BIG}),
        ("/api/invoices/import-file", {"filename": "f.xml", "content_base64": base64.b64encode(b"x" * BIG).decode()}),
    ],
)
def test_eleven_mb_on_other_upload_routes_is_refused(tmp_path, path, payload):
    client = _client(create_app(_runtime(tmp_path)))
    assert client.post(path, json=payload).status_code == 413


def test_declared_content_length_over_limit_is_refused_before_reading(tmp_path):
    client = _client(create_app(_runtime(tmp_path)))
    response = client.post(
        "/api/invoices/import",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": str(100 * 1024 * 1024)},
    )
    assert response.status_code == 413


def test_streamed_body_without_content_length_is_limited(tmp_path):
    client = _client(create_app(_runtime(tmp_path)))
    chunk = b"x" * (1024 * 1024)

    def body():
        yield b'{"xml": "'
        for _ in range(30):
            yield chunk
        yield b'"}'

    response = client.post("/api/invoices/import", content=body(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_small_upload_still_reaches_the_route(tmp_path):
    client = _client(create_app(_runtime(tmp_path)))
    response = client.post("/api/invoices/validate", content=json.dumps({"xml": "<a/>"}),
                           headers={"content-type": "application/json"})
    assert response.status_code == 200
