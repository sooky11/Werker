from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from werker.connectors.whatsapp import parse_webhook, verify_signature
from werker.pipeline import Pipeline, SendError
from werker.store import Store

STATIC_DIR = Path(__file__).parent / "static"


class ApproveIn(BaseModel):
    body: str | None = None
    force: bool = False


def _auth_dependency(password: str) -> Callable[..., None]:
    """Zonder wachtwoord is de pagina open; met wachtwoord vraagt de browser om inloggegevens."""
    if not password:
        return lambda: None
    basic = HTTPBasic()

    def check(credentials: HTTPBasicCredentials = Depends(basic)) -> None:
        if not secrets.compare_digest(credentials.password.encode(), password.encode()):
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})

    return check


def _summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "channel": row["channel"],
        "sender": row["sender"],
        "subject": row["subject"],
        "received_at": row["received_at"],
        "intent": row["intent"],
        "urgency": row["urgency"],
        "summary": row["summary"],
        "status": row["status"],
        "draft_id": row["draft_id"] if "draft_id" in row.keys() else None,
    }


def _draft(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "body": row["body"],
        "errors": [line for line in row["errors"].split("\n") if line],
        "reason": row["reason"],
        "status": row["status"],
    }


def create_app(
    store: Store,
    pipeline: Pipeline,
    app_secret: str,
    verify_token: str,
    ui_password: str = "",
) -> FastAPI:
    app = FastAPI(title="Werker")
    ui_auth = Depends(_auth_dependency(ui_password))

    @app.get("/", response_class=HTMLResponse, dependencies=[ui_auth])
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/stats", dependencies=[ui_auth])
    def stats() -> dict[str, Any]:
        return {"intents": store.intent_counts(), "pending": store.pending_count()}

    @app.get("/api/messages", dependencies=[ui_auth])
    def messages(intent: str | None = None, pending: bool = False, q: str | None = None) -> list[dict[str, Any]]:
        return [_summary(row) for row in store.list_messages(intent=intent, pending=pending, query=q)]

    @app.get("/api/messages/{message_id}", dependencies=[ui_auth])
    def message(message_id: int) -> dict[str, Any]:
        row = store.get_message_row(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="bericht bestaat niet")
        return {
            **_summary(row),
            "thread_id": row["thread_id"],
            "body": row["body"],
            "drafts": [_draft(d) for d in store.drafts_for_message(message_id)],
        }

    @app.post("/api/drafts/{draft_id}/approve", dependencies=[ui_auth])
    def approve(draft_id: int, payload: ApproveIn = Body(default=ApproveIn())) -> dict[str, Any]:
        try:
            pipeline.approve(draft_id, body=payload.body, force=payload.force)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except SendError as exc:
            raise HTTPException(status_code=502, detail=f"{exc}. Het concept blijft staan.")
        return {"status": "verzonden"}

    @app.post("/api/drafts/{draft_id}/reject", dependencies=[ui_auth])
    def reject(draft_id: int) -> dict[str, Any]:
        try:
            pipeline.reject(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"status": "afgewezen"}

    @app.post("/api/sync", dependencies=[ui_auth])
    def sync() -> dict[str, Any]:
        return {"processed": pipeline.sync()}

    @app.get("/webhook/whatsapp")
    def verify(request: Request) -> PlainTextResponse:
        params = request.query_params
        if verify_token and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
            return PlainTextResponse(params.get("hub.challenge", ""))
        raise HTTPException(status_code=403)

    @app.post("/webhook/whatsapp")
    async def receive(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        body = await request.body()
        # Zonder app secret is de handtekening niet te controleren; dan accepteren we niets.
        if not app_secret or not verify_signature(app_secret, body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(status_code=403)
        # Meta verwacht snel een 200; de verwerking (met Claude-aanroepen) gebeurt op de achtergrond.
        for msg in parse_webhook(json.loads(body)):
            background.add_task(pipeline.process, msg)
        return {"status": "ok"}

    return app
