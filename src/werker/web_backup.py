"""Back-up downloaden: alleen de database, nooit de geheimen. Wordt in create_app aan de app gehangen."""

from __future__ import annotations

import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from werker.auth import require_beheerder
from werker.backup import copy_database
from werker.runtime import Runtime

_ONLY_BEHEERDER = "alleen een beheerder kan een back-up downloaden"


def backup_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/backup", dependencies=[ui_auth])
    def download_backup(user: str | None = ui_auth) -> FileResponse:
        require_beheerder(runtime, user, _ONLY_BEHEERDER)
        folder = Path(tempfile.mkdtemp(prefix="werker-backup-"))
        target = folder / "werker.db"
        try:
            copy_database(runtime.store.backup_into, target)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        runtime.store.audit(None, "back-up gedownload", f"door {user}" if user else "")
        return FileResponse(
            target,
            media_type="application/vnd.sqlite3",
            filename=f"werker-backup-{date.today().isoformat()}.db",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True),
        )

    return router
