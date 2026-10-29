import json
import os
from pathlib import Path

import httpx
from fastapi import FastAPI

from werker import desktop
from werker.desktop import LocalServer, free_port, load_env_file, prepare_environment


def test_load_env_file_sets_values_without_overwriting(tmp_path, monkeypatch):
    env_file = tmp_path / "werker.env"
    env_file.write_text(
        "# commentaar\n\nWERKER_TEST_NEW=\"hallo\"\nWERKER_TEST_KEEP=uit-bestand\nBROKEN\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("WERKER_TEST_NEW", raising=False)
    monkeypatch.setenv("WERKER_TEST_KEEP", "uit-omgeving")
    load_env_file(env_file)
    assert os.environ["WERKER_TEST_NEW"] == "hallo"
    assert os.environ["WERKER_TEST_KEEP"] == "uit-omgeving"
    monkeypatch.delenv("WERKER_TEST_NEW", raising=False)


def test_prepare_environment_puts_data_in_werker_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WERKER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("WERKER_DB", raising=False)
    monkeypatch.delenv("WERKER_PRICES_FILE", raising=False)
    prepare_environment()
    assert Path(os.environ["WERKER_DB"]) == tmp_path / "home" / "werker.db"
    assert Path(os.environ["WERKER_PRICES_FILE"]) == tmp_path / "home" / "prices.yaml"
    monkeypatch.delenv("WERKER_DB", raising=False)
    monkeypatch.delenv("WERKER_PRICES_FILE", raising=False)


def test_free_port_returns_usable_port():
    port = free_port()
    assert 1024 < port < 65536


def test_local_server_serves_app_on_localhost_and_stops():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    port = free_port()
    server = LocalServer(app, port)
    server.start()
    try:
        assert server.url == f"http://127.0.0.1:{port}/"
        assert httpx.get(f"http://127.0.0.1:{port}/ping").json() == {"ok": True}
    finally:
        server.stop()
    assert not server._thread.is_alive()


def test_web_manifest_is_served_for_install(tmp_path):
    from tests.test_web import _build

    client, _, _ = _build(tmp_path)
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    manifest = json.loads(response.text)
    assert manifest["name"] == "Werker"
    assert manifest["display"] == "standalone"
    assert client.get("/icon.svg").headers["content-type"].startswith("image/svg+xml")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in client.get("/").text


def test_desktop_module_exposes_entry_point():
    assert callable(desktop.main)
