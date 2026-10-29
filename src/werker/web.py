from __future__ import annotations

import json
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from werker.connectors.whatsapp import parse_webhook, verify_signature
from werker.pipeline import Pipeline


def create_app(pipeline: Pipeline, app_secret: str, verify_token: str) -> FastAPI:
    app = FastAPI(title="Werker")

    @app.get("/webhook/whatsapp")
    def verify(request: Request) -> PlainTextResponse:
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == verify_token:
            return PlainTextResponse(params.get("hub.challenge", ""))
        raise HTTPException(status_code=403)

    @app.post("/webhook/whatsapp")
    async def receive(request: Request, background: BackgroundTasks) -> dict[str, Any]:
        body = await request.body()
        if not verify_signature(app_secret, body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(status_code=403)
        # Meta verwacht snel een 200; de verwerking gebeurt daarna op de achtergrond.
        for msg in parse_webhook(json.loads(body)):
            background.add_task(pipeline.process, msg)
        return {"status": "ok"}

    return app
