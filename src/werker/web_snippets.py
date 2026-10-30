"""Eindpunten voor vaste antwoordblokken. Iedereen mag ze lezen, maken en aanpassen; alleen een beheerder verwijdert."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from werker.auth import require_beheerder
from werker.runtime import Runtime

MAX_TITLE = 80
MAX_BODY = 2000


class SnippetIn(BaseModel):
    title: str = ""
    body: str = ""


def _clean(payload: SnippetIn) -> tuple[str, str]:
    title = payload.title.strip()
    body = payload.body.strip()
    if not title:
        raise HTTPException(status_code=400, detail="een antwoordblok heeft een titel nodig")
    if not body:
        raise HTTPException(status_code=400, detail="een antwoordblok heeft tekst nodig")
    if len(title) > MAX_TITLE:
        raise HTTPException(status_code=400, detail=f"de titel mag hoogstens {MAX_TITLE} tekens zijn")
    if len(body) > MAX_BODY:
        raise HTTPException(status_code=400, detail=f"de tekst mag hoogstens {MAX_BODY} tekens zijn")
    return title, body


def _snippet(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "title": row["title"], "body": row["body"], "created_at": row["created_at"]}


def snippets_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/snippets", dependencies=[ui_auth])
    def list_snippets() -> list[dict[str, Any]]:
        return [_snippet(row) for row in runtime.store.list_snippets()]

    @router.post("/api/snippets", dependencies=[ui_auth])
    def create_snippet(payload: SnippetIn) -> dict[str, Any]:
        title, body = _clean(payload)
        snippet_id = runtime.store.add_snippet(title, body)
        return _snippet(runtime.store.get_snippet(snippet_id))

    @router.put("/api/snippets/{snippet_id}", dependencies=[ui_auth])
    def update_snippet(snippet_id: int, payload: SnippetIn) -> dict[str, Any]:
        title, body = _clean(payload)
        if not runtime.store.update_snippet(snippet_id, title, body):
            raise HTTPException(status_code=404, detail="antwoordblok bestaat niet")
        return _snippet(runtime.store.get_snippet(snippet_id))

    @router.delete("/api/snippets/{snippet_id}", dependencies=[ui_auth])
    def delete_snippet(snippet_id: int, user: str | None = ui_auth) -> dict[str, Any]:
        require_beheerder(runtime, user, "alleen een beheerder kan een antwoordblok verwijderen")
        if not runtime.store.delete_snippet(snippet_id):
            raise HTTPException(status_code=404, detail="antwoordblok bestaat niet")
        return {"status": "verwijderd"}

    return router
