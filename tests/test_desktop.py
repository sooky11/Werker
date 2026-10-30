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


def _fake_desktop(tmp_path, monkeypatch, events, thread_alive=False):
    """Vervang venster, server, runtime en database door neppers, zodat main() zonder GUI draait."""
    import sys
    import threading
    import types

    import werker.runtime
    import werker.store
    import werker.web

    monkeypatch.setenv("WERKER_HOME", str(tmp_path))
    monkeypatch.setenv("WERKER_DB", str(tmp_path / "werker.db"))
    monkeypatch.setattr(desktop.logging, "basicConfig", lambda **kwargs: None)

    class FakeStore:
        def __init__(self, path):
            pass

        def close(self):
            events.append("store.close")

    class FakeRuntime:
        def __init__(self, store, home, env):
            pass

        def reconfigure(self):
            pass

    class FakeThread:
        def join(self, timeout=None):
            assert timeout is not None
            events.append("thread.join")

        def is_alive(self):
            return thread_alive

    def fake_run_background(runtime, stop):
        assert isinstance(stop, threading.Event)
        events.append("run_background")
        return FakeThread()

    class FakeServer:
        url = "http://127.0.0.1:1/"

        def __init__(self, app, port):
            pass

        def start(self):
            pass

        def stop(self):
            events.append("server.stop")

    webview = types.ModuleType("webview")
    webview.create_window = lambda *args, **kwargs: None
    webview.start = lambda: events.append("webview.start")
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setattr(werker.store, "Store", FakeStore)
    monkeypatch.setattr(werker.runtime, "Runtime", FakeRuntime)
    monkeypatch.setattr(werker.runtime, "run_background", fake_run_background)
    monkeypatch.setattr(werker.web, "create_app", lambda runtime: object())
    monkeypatch.setattr(desktop, "LocalServer", FakeServer)


def test_main_stops_background_thread_before_closing_store(tmp_path, monkeypatch):
    events: list[str] = []
    _fake_desktop(tmp_path, monkeypatch, events)

    assert desktop.main() == 0
    assert events == ["run_background", "webview.start", "server.stop", "thread.join", "store.close"]


def test_main_does_not_close_store_while_background_thread_still_runs(tmp_path, monkeypatch):
    events: list[str] = []
    _fake_desktop(tmp_path, monkeypatch, events, thread_alive=True)

    assert desktop.main() == 0
    assert "thread.join" in events
    assert "store.close" not in events
