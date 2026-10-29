from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from werker.connectors.whatsapp import parse_webhook, verify_signature
from werker.pipeline import SendError
from werker.runtime import Runtime
from werker.store import WAITING, Store

STATIC_DIR = Path(__file__).parent / "static"


class ApproveIn(BaseModel):
    body: str | None = None
    force: bool = False
    send_at: datetime | None = None


class ClientIn(BaseModel):
    name: str | None = None
    notes: str | None = None


class MergeIn(BaseModel):
    into: int


class SettingsIn(BaseModel):
    values: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None
    mailboxes: list[dict[str, Any]] | None = None


def _auth_dependency(password: str) -> Callable[..., None]:
    """Zonder wachtwoord is de pagina open; met wachtwoord vraagt de browser om inloggegevens."""
    if not password:
        return lambda: None
    basic = HTTPBasic()

    def check(credentials: HTTPBasicCredentials = Depends(basic)) -> None:
        if not secrets.compare_digest(credentials.password.encode(), password.encode()):
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})

    return check


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _summary(row: Any) -> dict[str, Any]:
    keys = row.keys()
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
        "draft_id": row["draft_id"] if "draft_id" in keys else None,
        "client_id": row["client_id"] if "client_id" in keys else None,
    }


def _draft(store: Store, row: Any) -> dict[str, Any]:
    previous_body = None
    if row["revises"] is not None:
        previous = store.get_draft(row["revises"])
        previous_body = previous["body"] if previous is not None else None
    return {
        "id": row["id"],
        "body": row["body"],
        "errors": [line for line in row["errors"].split("\n") if line],
        "reason": row["reason"],
        "status": row["status"],
        "scheduled_for": row["scheduled_for"],
        "change_summary": row["change_summary"],
        "previous_body": previous_body,
    }


def create_app(runtime: Runtime, ui_password: str = "") -> FastAPI:
    app = FastAPI(title="Werker")
    ui_auth = Depends(_auth_dependency(ui_password))

    def store() -> Store:
        return runtime.store

    def pipeline():
        if runtime.pipeline is None:
            raise HTTPException(status_code=503, detail="de app start nog, probeer het zo opnieuw")
        return runtime.pipeline

    @app.get("/", response_class=HTMLResponse, dependencies=[ui_auth])
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/manifest.webmanifest")
    def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg")
    def icon() -> FileResponse:
        return FileResponse(STATIC_DIR / "icon.svg", media_type="image/svg+xml")

    # ---------- inbox ----------

    @app.get("/api/stats", dependencies=[ui_auth])
    def stats() -> dict[str, Any]:
        return {
            "intents": store().intent_counts(),
            "pending": store().pending_count(),
            "scheduled": store().scheduled_count(),
        }

    @app.get("/api/messages", dependencies=[ui_auth])
    def messages(
        intent: str | None = None,
        pending: bool = False,
        scheduled: bool = False,
        q: str | None = None,
        client_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = store().list_messages(intent=intent, pending=pending, scheduled=scheduled, query=q, client_id=client_id)
        return [_summary(row) for row in rows]

    @app.get("/api/messages/{message_id}", dependencies=[ui_auth])
    def message(message_id: int) -> dict[str, Any]:
        row = store().get_message_row(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="bericht bestaat niet")
        return {
            **_summary(row),
            "thread_id": row["thread_id"],
            "body": row["body"],
            "drafts": [_draft(store(), d) for d in store().drafts_for_message(message_id)],
            "log": [dict(entry) for entry in store().audit_log(message_id)],
        }

    @app.post("/api/drafts/{draft_id}/approve", dependencies=[ui_auth])
    def approve(draft_id: int, payload: ApproveIn = Body(default=ApproveIn())) -> dict[str, Any]:
        send_at = _utc(payload.send_at)
        try:
            pipeline().approve(draft_id, body=payload.body, force=payload.force, send_at=send_at)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except SendError as exc:
            raise HTTPException(status_code=502, detail=f"{exc}. Het concept blijft staan.")
        scheduled = send_at is not None and send_at > datetime.now(timezone.utc)
        return {"status": "ingepland" if scheduled else "verzonden"}

    @app.post("/api/drafts/{draft_id}/cancel-schedule", dependencies=[ui_auth])
    def cancel_schedule(draft_id: int) -> dict[str, Any]:
        try:
            pipeline().cancel_schedule(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"status": WAITING}

    @app.post("/api/drafts/{draft_id}/reject", dependencies=[ui_auth])
    def reject(draft_id: int) -> dict[str, Any]:
        try:
            pipeline().reject(draft_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"status": "afgewezen"}

    @app.post("/api/sync", dependencies=[ui_auth])
    def sync() -> dict[str, Any]:
        return {"processed": pipeline().sync()}

    # ---------- klanten ----------

    @app.get("/api/clients", dependencies=[ui_auth])
    def clients(q: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "message_count": row["message_count"],
                "last_received": row["last_received"],
                "channels": sorted(set((row["channels"] or "").split(","))) if row["channels"] else [],
            }
            for row in store().list_clients(query=q)
        ]

    @app.get("/api/clients/{client_id}", dependencies=[ui_auth])
    def client(client_id: int) -> dict[str, Any]:
        row = store().get_client(client_id)
        if row is None:
            raise HTTPException(status_code=404, detail="klant bestaat niet")
        return {
            "id": row["id"],
            "name": row["name"],
            "notes": row["notes"],
            "identifiers": [{"kind": i["kind"], "value": i["value"]} for i in store().client_identifiers(client_id)],
            "messages": [_summary(m) for m in store().list_messages(client_id=client_id, limit=500)],
        }

    @app.patch("/api/clients/{client_id}", dependencies=[ui_auth])
    def update_client(client_id: int, payload: ClientIn) -> dict[str, Any]:
        if store().get_client(client_id) is None:
            raise HTTPException(status_code=404, detail="klant bestaat niet")
        store().update_client(client_id, name=payload.name, notes=payload.notes)
        return {"status": "opgeslagen"}

    @app.post("/api/clients/{client_id}/merge", dependencies=[ui_auth])
    def merge_client(client_id: int, payload: MergeIn) -> dict[str, Any]:
        if client_id == payload.into:
            raise HTTPException(status_code=400, detail="kies een andere klant om mee samen te voegen")
        if store().get_client(client_id) is None or store().get_client(payload.into) is None:
            raise HTTPException(status_code=404, detail="klant bestaat niet")
        store().merge_clients(client_id, payload.into)
        return {"status": "samengevoegd", "into": payload.into}

    # ---------- instellingen ----------

    @app.get("/api/settings", dependencies=[ui_auth])
    def get_settings() -> dict[str, Any]:
        return runtime.settings_view()

    @app.put("/api/settings", dependencies=[ui_auth])
    def put_settings(payload: SettingsIn) -> dict[str, Any]:
        try:
            runtime.update(values=payload.values, secrets=payload.secrets, mailboxes=payload.mailboxes)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return runtime.settings_view()

    # ---------- WhatsApp-webhook ----------

    @app.get("/webhook/whatsapp")
    def verify(request: Request) -> PlainTextResponse:
        token = runtime.env().get("WHATSAPP_VERIFY_TOKEN", "")
        params = request.query_params
        if token and params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == token:
            return PlainTextResponse(params.get("hub.challenge", ""))
        raise HTTPException(status_code=403)

    @app.post("/webhook/whatsapp")
    async def receive(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        body = await request.body()
        app_secret = runtime.env().get("WHATSAPP_APP_SECRET", "")
        # Zonder app secret is de handtekening niet te controleren; dan accepteren we niets.
        if not app_secret or not verify_signature(app_secret, body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(status_code=403)
        # Meta verwacht snel een 200; de verwerking (met Claude-aanroepen) gebeurt op de achtergrond.
        for msg in parse_webhook(json.loads(body)):
            background.add_task(pipeline().process, msg)
        return {"status": "ok"}

    return app
