"""Back-up en herstel van de database met de backup-API van sqlite3.

De backup-API maakt een samenhangende kopie, ook terwijl de app draait en schrijft.
Geheimen (secrets.json) gaan alleen mee als daar uitdrukkelijk om wordt gevraagd; ze staan dan
in een eigen tabel in het back-upbestand, dat dan alleen voor de eigenaar leesbaar is.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

SECRETS_TABLE = "werker_backup_secrets"
REQUIRED_TABLES = ("messages", "app_settings")


class BackupError(RuntimeError):
    pass


def _create_private(path: Path) -> None:
    """Maak een leeg bestand dat meteen alleen voor de eigenaar leesbaar is."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows kent geen Unix-rechten.


def copy_database(source: Callable[[sqlite3.Connection], None] | sqlite3.Connection, target: Path) -> None:
    """Schrijf een kopie naar een nieuw bestand `target`. `source` is een verbinding of een functie die
    de kopie in een gegeven verbinding schrijft (zoals Store.backup_into)."""
    if target.exists():
        raise BackupError(f"{target} bestaat al; kies een andere bestandsnaam")
    target.parent.mkdir(parents=True, exist_ok=True)
    _create_private(target)
    destination = sqlite3.connect(target)
    try:
        if isinstance(source, sqlite3.Connection):
            source.backup(destination)
        else:
            source(destination)
    except Exception:
        destination.close()
        target.unlink(missing_ok=True)
        raise
    destination.close()


def backup(db_path: Path, target: Path, secrets_path: Path | None = None) -> None:
    """Back-up van de database naar `target`. Met `secrets_path` gaan ook de geheimen mee."""
    if not db_path.exists():
        raise BackupError(f"database {db_path} bestaat niet")
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        copy_database(source, target)
    finally:
        source.close()
    if secrets_path is None:
        return
    secrets = json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {}
    copy = sqlite3.connect(target)
    try:
        copy.execute(f"create table {SECRETS_TABLE} (key text primary key, value text not null)")
        copy.executemany(f"insert into {SECRETS_TABLE} (key, value) values (?, ?)", list(secrets.items()))
        copy.commit()
    finally:
        copy.close()


def _check_backup(path: Path) -> bool:
    """Controleer dat het bestand een geldige Werker-database is. Geeft terug of er geheimen in staan."""
    if not path.is_file():
        raise BackupError(f"{path} bestaat niet")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if connection.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise BackupError(f"{path} is beschadigd")
            tables = {row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")}
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path} is geen geldige back-up: {exc}") from exc
    missing = [name for name in REQUIRED_TABLES if name not in tables]
    if missing:
        raise BackupError(f"{path} is geen Werker-back-up (tabel {', '.join(missing)} ontbreekt)")
    return SECRETS_TABLE in tables


def restore(backup_path: Path, db_path: Path, secrets_path: Path, now: datetime | None = None) -> dict[str, str | None]:
    """Zet een back-up terug. Maakt eerst een veiligheidskopie van de huidige database (en van de
    geheimen als de back-up die bevat). Geeft de paden van de veiligheidskopieën terug."""
    has_secrets = _check_backup(backup_path)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    safety_db = None
    if db_path.exists():
        safety_db = db_path.with_name(f"{db_path.name}.voor-herstel-{stamp}")
        current = sqlite3.connect(db_path)
        try:
            copy_database(current, safety_db)
        finally:
            current.close()
    safety_secrets = None
    if has_secrets and secrets_path.exists():
        safety_secrets = secrets_path.with_name(f"{secrets_path.name}.voor-herstel-{stamp}")
        _create_private(safety_secrets)
        safety_secrets.write_bytes(secrets_path.read_bytes())

    source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    destination = sqlite3.connect(db_path)
    try:
        source.backup(destination)
        if has_secrets:
            secrets = dict(destination.execute(f"select key, value from {SECRETS_TABLE}").fetchall())
            # De geheimen horen niet in de database; overschrijf de vrijgekomen ruimte bij het verwijderen.
            destination.execute("pragma secure_delete = on")
            destination.execute(f"drop table {SECRETS_TABLE}")
            destination.commit()
            destination.execute("vacuum")
    finally:
        source.close()
        destination.close()
    if has_secrets:
        _write_secrets(secrets_path, secrets)
    return {
        "database": str(safety_db) if safety_db else None,
        "geheimen": str(safety_secrets) if safety_secrets else None,
    }


def _write_secrets(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
