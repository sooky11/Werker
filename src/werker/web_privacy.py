"""HTTP-routes voor AVG-functies: klantgegevens exporteren, een klant verwijderen en het verwijderlogboek tonen."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from werker import privacy
from werker.runtime import Runtime


class EraseIn(BaseModel):
    confirm: str | None = None


def privacy_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/clients/{client_id}/export", dependencies=[ui_auth])
    def export(client_id: int) -> JSONResponse:
        try:
            data = privacy.export_client(runtime.store, client_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return JSONResponse(
            content=data,
            headers={"Content-Disposition": f'attachment; filename="klant-{client_id}-export.json"'},
        )

    @router.delete("/api/clients/{client_id}", dependencies=[ui_auth])
    def erase(client_id: int, payload: EraseIn = Body(default=EraseIn())) -> dict[str, Any]:
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
    def log() -> list[dict[str, Any]]:
        return runtime.store.privacy_log_entries()

    return router
