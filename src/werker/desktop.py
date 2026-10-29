"""Werker als gewoon bureaubladprogramma: start de inbox lokaal en toont die in een eigen venster.

Installeren: pip install -e ".[desktop]" en daarna `werker-desktop`.
Instellingen komen uit ~/.werker/werker.env (KEY=VALUE per regel), net als een .env-bestand.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

log = logging.getLogger(__name__)

WINDOW_TITLE = "Werker"


def data_dir() -> Path:
    path = Path(os.environ.get("WERKER_HOME", Path.home() / ".werker"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_env_file(path: Path) -> None:
    """Zet waarden uit werker.env in de omgeving, zonder bestaande variabelen te overschrijven."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def prepare_environment() -> Path:
    """Laad de instellingen en kies een standaardlocatie voor de database."""
    home = data_dir()
    load_env_file(home / "werker.env")
    os.environ.setdefault("WERKER_DB", str(home / "werker.db"))
    os.environ.setdefault("WERKER_PRICES_FILE", str(home / "prices.yaml"))
    return home


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalServer:
    """Draait de FastAPI-app in een achtergrondthread op 127.0.0.1."""

    def __init__(self, app, port: int):
        self.port = port
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self, timeout: float = 15) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("de lokale server kon niet starten")
            time.sleep(0.05)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    prepare_environment()

    # Importeer hier, zodat de instellingen al klaarstaan.
    from werker.cli import build_connectors, build_pipeline
    from werker.config import Settings
    from werker.store import Store
    from werker.web import create_app

    try:
        import webview
    except ImportError:
        print("pywebview ontbreekt: installeer met pip install -e \".[desktop]\"", file=sys.stderr)
        return 1

    settings = Settings.from_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"Zet ANTHROPIC_API_KEY in {data_dir() / 'werker.env'} en start opnieuw.", file=sys.stderr)
        return 1

    store = Store(settings.db_path)
    pipeline = build_pipeline(settings, store, build_connectors(os.environ))
    app = create_app(
        store,
        pipeline,
        app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
        verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
    )

    server = LocalServer(app, free_port())
    server.start()
    try:
        webview.create_window(WINDOW_TITLE, server.url, width=1280, height=840, min_size=(900, 600))
        webview.start()
    finally:
        server.stop()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
