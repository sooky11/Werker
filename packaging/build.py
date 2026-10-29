"""Bouwt het bureaubladprogramma voor het systeem waarop dit draait (Windows of macOS).

Gebruik: python packaging/build.py   (met het project geïnstalleerd: pip install -e ".[desktop,gmail]" pyinstaller)
Resultaat: dist/Werker.app op macOS, dist/Werker/ op Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    static = ROOT / "src" / "werker" / "static"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "Werker",
        "--add-data",
        f"{static}{os.pathsep}werker/static",
        "--collect-submodules",
        "werker",
        str(ROOT / "packaging" / "werker_desktop.py"),
    ]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
