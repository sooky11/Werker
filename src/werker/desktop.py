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
# Zo lang wachten we bij afsluiten tot de achtergrondtaak (bijvoorbeeld een lopende sync) klaar is.
BACKGROUND_JOIN_SECONDS = 15


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


def self_test(url: str) -> bool:
    """Controleer dat de gebundelde app echt draait: de inbox-pagina en de instellingen moeten laden."""
    import httpx

    try:
        page = httpx.get(url, timeout=10)
        settings = httpx.get(url + "api/settings", timeout=10)
    except httpx.HTTPError:
        log.exception("zelftest: de lokale server antwoordt niet")
        return False
    ok = page.status_code == 200 and "<html" in page.text.lower() and settings.status_code == 200
    log.info("zelftest: pagina %s, instellingen %s", page.status_code, settings.status_code)
    return ok


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    testing = "--zelftest" in args  # voor de build: start alles behalve het venster en stop meteen
    home = prepare_environment()
    # Windows en macOS-builds hebben geen console; het logboek staat daarom in een bestand.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(home / "werker.log", encoding="utf-8")],
    )

    # Importeer hier, zodat de instellingen al klaarstaan.
    from werker.config import Settings
    from werker.runtime import Runtime, run_background
    from werker.store import Store
    from werker.web import create_app

    try:
        import webview
    except ImportError:
        log.error("pywebview ontbreekt: installeer met pip install -e \".[desktop]\"")
        return 1

    settings = Settings.from_env()
    store = Store(settings.db_path)
    runtime = Runtime(store, home, os.environ)
    runtime.reconfigure()
    app = create_app(runtime)

    stop = threading.Event()
    background = run_background(runtime, stop)
    server = LocalServer(app, free_port())
    server.start()
    try:
        if testing:
            return 0 if self_test(server.url) else 1
        webview.create_window(WINDOW_TITLE, server.url, width=1280, height=840, min_size=(900, 600))
        webview.start()
    finally:
        stop.set()
        server.stop()
        # Eerst de achtergrondtaak laten stoppen; anders kan die de database nog gebruiken terwijl we hem sluiten.
        background.join(timeout=BACKGROUND_JOIN_SECONDS)
        if background.is_alive():
            log.warning("achtergrondtaak stopte niet op tijd; de database wordt niet expliciet gesloten")
        else:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
