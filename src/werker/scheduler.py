from __future__ import annotations

import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


def start_periodic(task: Callable[[], object], seconds: float, name: str) -> threading.Thread:
    """Voer task elke seconds uit in een achtergrondthread. Fouten worden gelogd, de lus stopt niet."""

    def loop() -> None:
        while True:
            try:
                task()
            except Exception:
                log.exception("achtergrondtaak %s mislukt; probeer het later opnieuw", name)
            time.sleep(seconds)

    thread = threading.Thread(target=loop, name=name, daemon=True)
    thread.start()
    return thread
