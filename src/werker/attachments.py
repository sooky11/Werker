"""Bijlagen van binnenkomende berichten bewaren in <datamap>/bijlagen/<bericht-id>/<naam>.

enable(store, home) laat Store na elk opgeslagen bericht save_attachments aanroepen. Bestandsnamen
komen van de afzender en zijn dus niet te vertrouwen: sanitize_filename maakt er een veilige naam van,
en resolve_file controleert bij het downloaden dat het pad binnen de bijlagenmap blijft.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from werker.models import Message

log = logging.getLogger(__name__)

FOLDER = "bijlagen"
MAX_NAME = 120
TOO_LARGE = "te groot"
_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')


def enable(store: Any, home: Path) -> None:
    """Bewaar voortaan de bijlagen van elk nieuw bericht onder home/bijlagen. Nogmaals aanroepen verplaatst alleen de map."""
    first = store.attachments_root is None
    store.attachments_root = Path(home) / FOLDER
    if first:

        def hook(message_id: int, msg: Message) -> None:
            save_attachments(store, store.attachments_root.parent, message_id, msg)

        store.add_message_hook(hook)


def sanitize_filename(name: str) -> str:
    """Alleen het laatste stuk van de naam, zonder padtekens, voorloop-punten of stuurtekens, max. 120 tekens."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.split(r"[/\\]", name)[-1]
    name = _UNSAFE.sub("_", name).strip().lstrip(". ").rstrip(". ")
    if not name:
        name = "bijlage"
    if len(name) > MAX_NAME:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10 and stem:
            name = stem[: MAX_NAME - len(ext) - 1].rstrip(". ") + "." + ext
        else:
            name = name[:MAX_NAME].rstrip(". ")
    return name or "bijlage"


def _unique(folder: Path, name: str) -> Path:
    candidate = folder / name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    number = 2
    while candidate.exists():
        suffix = f" ({number})"
        base = stem[: MAX_NAME - len(suffix) - (len(ext) + 1 if ext else 0)]
        candidate = folder / (f"{base}{suffix}.{ext}" if ext else f"{base}{suffix}")
        number += 1
    return candidate


def save_attachments(store: Any, home: Path, message_id: int, msg: Message) -> int:
    """Schrijf de bijlagen van msg weg en noteer ze in de tabel. Geeft het aantal genoteerde bijlagen."""
    if not msg.attachments:
        return 0
    root = Path(home) / FOLDER
    folder = root / str(int(message_id))
    for attachment in msg.attachments:
        filename = sanitize_filename(attachment.filename)
        content_type = (attachment.content_type or "application/octet-stream").lower()[:100]
        if attachment.too_large:
            store.add_attachment(message_id, filename, content_type, attachment.size, "", True)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        target = _unique(folder, filename)
        # O_EXCL: nooit een bestaand bestand (of symlink) overschrijven.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(attachment.data)
        relative = target.relative_to(root).as_posix()
        store.add_attachment(message_id, target.name, content_type, len(attachment.data), relative, False)
    return len(msg.attachments)


def resolve_file(root: Path, relative: str) -> Path | None:
    """Het bestand van een bijlage, alleen als het echt binnen root ligt en bestaat."""
    if not relative:
        return None
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path == base or not path.is_file():
        return None
    return path


def remove_orphans(store: Any) -> int:
    """Verwijder bijlagen (bestand en rij) van berichten die niet meer bestaan. Geeft het aantal rijen."""
    rows = store.orphan_attachments()
    root = store.attachments_root
    folders: set[Path] = set()
    for row in rows:
        if root is None:
            continue
        path = resolve_file(root, row["path"])
        if path is not None:
            try:
                path.unlink()
            except OSError:
                log.warning("bijlage %s kon niet worden verwijderd", row["id"])
        folders.add(Path(root) / str(int(row["message_id"])))
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        if folder.resolve().parent == Path(root).resolve():
            shutil.rmtree(folder, ignore_errors=True)
    return store.delete_attachment_rows([row["id"] for row in rows])
