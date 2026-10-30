"""Eindpunten voor bijlagen: de lijst bij een bericht en het downloaden van één bijlage.

Bijlagen komen van buiten en zijn niet te vertrouwen. Alleen afbeeldingen (png, jpeg, gif, webp) en PDF
worden in de browser getoond; al het andere, ook HTML en SVG, komt als application/octet-stream en als download.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from werker.attachments import FOLDER, TOO_LARGE, resolve_file
from werker.runtime import Runtime

PREVIEW_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}


def _attachment(row: Any) -> dict[str, Any]:
    too_large = bool(row["too_large"])
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "too_large": too_large,
        "status": TOO_LARGE if too_large else "opgeslagen",
        "url": None if too_large else f"/api/attachments/{row['id']}",
    }


def _disposition(kind: str, filename: str) -> str:
    """Content-Disposition met een ASCII-naam zonder aanhalingstekens of regeleinden, plus de UTF-8-naam."""
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\;]', "_", filename).strip() or "bijlage"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def attachments_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/messages/{message_id}/attachments", dependencies=[ui_auth])
    def list_attachments(message_id: int) -> list[dict[str, Any]]:
        if runtime.store.get_message_row(message_id) is None:
            raise HTTPException(status_code=404, detail="bericht bestaat niet")
        return [_attachment(row) for row in runtime.store.list_attachments(message_id)]

    @router.get("/api/attachments/{attachment_id}", dependencies=[ui_auth])
    def download(attachment_id: int) -> FileResponse:
        row = runtime.store.get_attachment(attachment_id)
        if row is None:
            raise HTTPException(status_code=404, detail="bijlage bestaat niet")
        if row["too_large"]:
            raise HTTPException(status_code=404, detail="deze bijlage was te groot en is niet bewaard")
        path = resolve_file(runtime.home / FOLDER, row["path"])
        if path is None:
            raise HTTPException(status_code=404, detail="het bestand van deze bijlage ontbreekt")
        content_type = str(row["content_type"]).split(";")[0].strip().lower()
        inline = content_type in PREVIEW_TYPES
        headers = {
            "Content-Disposition": _disposition("inline" if inline else "attachment", row["filename"]),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if content_type != "application/pdf":
            # Ook als een afbeelding toch iets anders blijkt: geen scripts, geen andere bronnen.
            headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return FileResponse(
            path,
            media_type=content_type if inline else "application/octet-stream",
            headers=headers,
        )

    return router
