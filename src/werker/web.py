from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from werker.connectors.whatsapp import parse_webhook, verify_signature
from werker.peppol import InvoiceError
from werker.invoicing import validate_only
from werker.pipeline import SendError
from werker.runtime import Runtime
from werker.store import WAITING, Store
from werker.web_privacy import privacy_router

STATIC_DIR = Path(__file__).parent / "static"


class ApproveIn(BaseModel):
    body: str | None = None
    force: bool = False
    send_at: datetime | None = None
    slot_start: datetime | None = None  # bij een afspraak: het gekozen tijdstip


class ClientIn(BaseModel):
    name: str | None = None
    notes: str | None = None


class MergeIn(BaseModel):
    into: int


class InvoiceIn(BaseModel):
    xml: str


class ImportIn(BaseModel):
    xml: str
    client_id: int | None = None


class BankIn(BaseModel):
    csv: str


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


def _invoice_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "number": row["number"],
        "issue_date": row["issue_date"],
        "due_date": row["due_date"],
        "total_incl": row["total_incl"],
        "currency": row["currency"],
        "buyer_name": row["buyer_name"],
        "buyer_endpoint": row["buyer_endpoint"],
        "status": row["status"],
        "paid_at": row["paid_at"],
        "sent_at": row["sent_at"],
        "last_error": row["last_error"],
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

    @app.middleware("http")
    async def require_json_for_writes(request: Request, call_next):
        # Een andere website kan alleen een formulier of tekst naar deze app sturen, geen JSON.
        # Voor JSON is vooraf toestemming nodig, die deze app niet geeft. Zo kan een bezoek aan
        # een andere pagina geen goedkeuring of verzending uitlokken.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
            if not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "schrijven vereist application/json"}, status_code=415)
        return await call_next(request)

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
            pipeline().approve(
                draft_id,
                body=payload.body,
                force=payload.force,
                send_at=send_at,
                slot_start=payload.slot_start,
            )
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

    # ---------- Peppol-facturen ----------

    @app.get("/api/invoices", dependencies=[ui_auth])
    def invoices(status: str | None = None) -> list[dict[str, Any]]:
        return [_invoice_summary(row) for row in runtime.store.list_invoices(status=status)]

    @app.get("/api/invoices/{invoice_id}", dependencies=[ui_auth])
    def invoice(invoice_id: int) -> dict[str, Any]:
        row = runtime.store.get_invoice(invoice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="factuur bestaat niet")
        return {**_invoice_summary(row), "xml": row["xml"]}

    @app.post("/api/invoices/import", dependencies=[ui_auth])
    def import_invoice(payload: ImportIn) -> dict[str, Any]:
        try:
            invoice_id = runtime.invoicing.import_xml(payload.xml, client_id=payload.client_id)
        except InvoiceError as exc:
            raise HTTPException(status_code=422, detail={"problems": exc.problems})
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"id": invoice_id}

    @app.post("/api/invoices/validate", dependencies=[ui_auth])
    def validate_invoice(payload: InvoiceIn) -> dict[str, Any]:
        return validate_only(payload.xml)

    @app.post("/api/invoices/bank", dependencies=[ui_auth])
    def bank_import(payload: BankIn) -> dict[str, Any]:
        try:
            matched = runtime.invoicing.match_bank_file(payload.csv)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"matched": matched}

    @app.post("/api/invoices/{invoice_id}/mark-paid", dependencies=[ui_auth])
    def mark_paid(invoice_id: int) -> dict[str, Any]:
        try:
            return {"status": runtime.invoicing.mark_paid(invoice_id)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.post("/api/invoices/{invoice_id}/send", dependencies=[ui_auth])
    def send_invoice(invoice_id: int) -> dict[str, Any]:
        try:
            return {"status": runtime.invoicing.send(invoice_id)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/api/peppol/map", dependencies=[ui_auth])
    def peppol_map() -> dict[str, Any]:
        return {"folder": str(runtime.invoicing.folder), "items": runtime.invoicing.map_items()}

    # ---------- WhatsApp-webhook ----------

    @app.get("/webhook/whatsapp")
    def verify(request: Request) -> PlainTextResponse:
        token = runtime.env().get("WHATSAPP_VERIFY_TOKEN", "")
        params = request.query_params
        given = params.get("hub.verify_token", "")
        if token and params.get("hub.mode") == "subscribe" and secrets.compare_digest(given.encode(), token.encode()):
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

    app.include_router(privacy_router(runtime, ui_auth))
    return app
