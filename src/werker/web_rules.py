"""HTTP-routes voor het corrigeren van een categorie en de afzenderregels die daaruit volgen."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from werker.auth import require_beheerder
from werker.models import Intent
from werker.rules import sender_address
from werker.runtime import Runtime

_ONLY_BEHEERDER = "alleen een beheerder kan afzenderregels bekijken of verwijderen"


class IntentIn(BaseModel):
    intent: str
    remember: bool = False


def rules_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    """ui_auth is de Depends(...) uit web.py; die levert de gebruikersnaam voor audit en rolcontrole."""
    router = APIRouter()

    @router.patch("/api/messages/{message_id}/intent", dependencies=[ui_auth])
    def correct_intent(message_id: int, payload: IntentIn, user: str | None = ui_auth) -> dict[str, Any]:
        try:
            intent = Intent(payload.intent.strip().lower())
        except ValueError:
            raise HTTPException(status_code=422, detail=f"onbekende categorie: {payload.intent!r}")
        store = runtime.store
        row = store.get_message_row(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="bericht bestaat niet")
        old = row["intent"] or "onbekend"
        store.set_message_intent(message_id, intent.value)
        suffix = f" door {user}" if user else ""
        store.audit(message_id, "categorie aangepast", f"van {old} naar {intent.value}{suffix}")
        rule_id = None
        if payload.remember:
            address = sender_address(row["sender"])
            if not address:
                raise HTTPException(status_code=422, detail="dit bericht heeft geen afzenderadres om te onthouden")
            rule_id = store.set_sender_rule(address, intent.value, created_by=user)
            store.audit(message_id, "afzenderregel opgeslagen", f"{address} -> {intent.value}{suffix}")
        return {"id": message_id, "intent": intent.value, "rule_id": rule_id}

    @router.get("/api/sender-rules", dependencies=[ui_auth])
    def list_rules(user: str | None = ui_auth) -> list[dict[str, Any]]:
        require_beheerder(runtime, user, _ONLY_BEHEERDER)
        return runtime.store.list_sender_rules()

    @router.delete("/api/sender-rules/{rule_id}", dependencies=[ui_auth])
    def delete_rule(rule_id: int, user: str | None = ui_auth) -> dict[str, Any]:
        require_beheerder(runtime, user, _ONLY_BEHEERDER)
        if not runtime.store.delete_sender_rule(rule_id):
            raise HTTPException(status_code=404, detail="afzenderregel bestaat niet")
        runtime.store.audit(None, "afzenderregel verwijderd", f"regel {rule_id}" + (f" door {user}" if user else ""))
        return {"status": "verwijderd"}

    return router
