"""HTTP-routes voor AVG-functies: klantgegevens exporteren, een klant verwijderen en het verwijderlogboek tonen."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from werker import privacy
from werker.auth import require_beheerder
from werker.runtime import Runtime


_ONLY_BEHEERDER = "alleen een beheerder kan klantgegevens exporteren, verwijderen of het privacylogboek inzien"


class EraseIn(BaseModel):
    confirm: str | None = None


def privacy_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    """ui_auth is de Depends(...) uit web.py; die levert de gebruikersnaam voor de rolcontrole."""
    router = APIRouter()

    @router.get("/api/clients/{client_id}/export", dependencies=[ui_auth])
    def export(client_id: int, user: str | None = ui_auth) -> JSONResponse:
        require_beheerder(runtime, user, _ONLY_BEHEERDER)
        try:
            data = privacy.export_client(runtime.store, client_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse(
            content=data,
            headers={"Content-Disposition": f'attachment; filename="klant-{client_id}-export.json"'},
        )

    @router.delete("/api/clients/{client_id}", dependencies=[ui_auth])
    def erase(
        client_id: int, payload: EraseIn = Body(default=EraseIn()), user: str | None = ui_auth
    ) -> dict[str, Any]:
        require_beheerder(runtime, user, _ONLY_BEHEERDER)
        client = runtime.store.get_client(client_id)
        if client is None:
            raise HTTPException(status_code=404, detail="klant bestaat niet")
        # Voorkomt een ongeluk: alleen wie de naam van de klant intypt, verwijdert.
        if (payload.confirm or "").strip() != client["name"].strip():
            raise HTTPException(
                status_code=400,
                detail="ter bevestiging moet de naam van de klant worden meegestuurd als \"confirm\"",
            )
        try:
            counts = privacy.erase_client(runtime.store, client_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"status": "verwijderd", "counts": counts}

    @router.get("/api/privacy/log", dependencies=[ui_auth])
    def log(user: str | None = ui_auth) -> list[dict[str, Any]]:
        require_beheerder(runtime, user, _ONLY_BEHEERDER)
        return runtime.store.privacy_log_entries()

    return router
