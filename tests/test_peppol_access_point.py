import base64
import json

import httpx
import pytest

from tests.test_invoices import ubl
from werker.connectors.peppol import PeppolError, StorecoveAccessPoint, build_access_point, build_payload
from werker.invoicing import SENT, Invoicing
from werker.runtime import Runtime
from werker.store import Store


def _client(handler):
    return httpx.Client(base_url="https://api.storecove.com", transport=httpx.MockTransport(handler))


def test_payload_carries_the_invoice_as_base64_ubl():
    xml = ubl().encode()
    payload = build_payload(xml, "0106:12345678", "42")
    assert payload["legalEntityId"] == 42
    assert payload["routing"]["eIdentifiers"][0]["id"] == "0106:12345678"
    raw = payload["document"]["rawDocument"]
    assert base64.b64decode(raw["document"]) == xml
    assert raw["parseStrategy"] == "ubl"


def test_send_posts_with_bearer_key_and_returns_guid():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"guid": "abc-123"})

    access_point = StorecoveAccessPoint("geheim", "42", client=_client(handler))
    assert access_point.send(ubl().encode(), "0106:12345678") == "abc-123"
    assert seen["url"].endswith("/api/v2/document_submissions")
    assert seen["auth"] == "Bearer geheim"
    assert seen["body"]["routing"]["eIdentifiers"][0]["id"] == "0106:12345678"


def test_rejection_raises_so_the_invoice_goes_to_the_map(tmp_path):
    access_point = StorecoveAccessPoint(
        "geheim", "42", client=_client(lambda r: httpx.Response(422, text="ontvanger onbekend"))
    )
    with pytest.raises(PeppolError, match="ontvanger onbekend"):
        access_point.send(b"<x/>", "0106:1")

    store = Store(str(tmp_path / "w.db"))
    service = Invoicing(store, tmp_path / "map", access_point)
    invoice_id = service.import_xml(ubl())
    assert service.send(invoice_id) == "in map"
    store.close()


def test_invoice_is_sent_through_the_access_point(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    service = Invoicing(store, tmp_path / "map", StorecoveAccessPoint(
        "geheim", "42", client=_client(lambda r: httpx.Response(200, json={"guid": "g"}))))
    invoice_id = service.import_xml(ubl())
    assert service.send(invoice_id) == SENT
    store.close()


def test_no_access_point_without_provider_or_key():
    assert build_access_point({}) is None
    assert build_access_point({"PEPPOL_PROVIDER": "storecove", "PEPPOL_LEGAL_ENTITY_ID": "42"}) is None
    assert isinstance(
        build_access_point({"PEPPOL_PROVIDER": "storecove", "PEPPOL_API_KEY": "k", "PEPPOL_LEGAL_ENTITY_ID": "42"}),
        StorecoveAccessPoint,
    )


def test_runtime_connects_access_point_from_settings(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    runtime = Runtime(store, tmp_path, {})
    runtime.reconfigure()
    assert runtime.invoicing.access_point is None
    runtime.update(values={"PEPPOL_PROVIDER": "storecove", "PEPPOL_LEGAL_ENTITY_ID": "42"},
                   secrets={"PEPPOL_API_KEY": "geheim"})
    assert runtime.invoicing.access_point is not None
    view = runtime.settings_view()
    assert view["peppol_connected"] is True
    assert view["secrets_set"]["PEPPOL_API_KEY"] is True
    assert "geheim" not in json.dumps(view)
    store.close()
