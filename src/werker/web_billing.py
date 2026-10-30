"""Eindpunten voor betalingsherinneringen en het overzicht. Wordt in create_app aan de app gehangen."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from werker.invoicing import WAITING_PAYMENT, ReminderSendError
from werker.reports import summary
from werker.runtime import Runtime


class ReminderApproveIn(BaseModel):
    body: str | None = None


def _reminder(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "invoice_id": row["invoice_id"],
        "number": row["number"],
        "buyer_name": row["buyer_name"],
        "amount": row["total_incl"],
        "currency": row["currency"],
        "due_date": row["due_date"],
        "status": row["status"],
        "recipient": row["recipient"],
        "subject": row["subject"],
        "body": row["body"],
        "created_at": row["created_at"],
        "last_error": row["last_error"],
    }


def billing_router(runtime: Runtime, ui_auth: Any) -> APIRouter:
    router = APIRouter()

    @router.get("/api/reminders", dependencies=[ui_auth])
    def reminders() -> list[dict[str, Any]]:
        """Herinneringen die op goedkeuring wachten, ook die zonder e-mailadres van de klant."""
        return [_reminder(row) for row in runtime.store.pending_reminders(WAITING_PAYMENT)]

    @router.post("/api/reminders/{reminder_id}/approve", dependencies=[ui_auth])
    def approve_reminder(
        reminder_id: int, payload: ReminderApproveIn = Body(default=ReminderApproveIn())
    ) -> dict[str, Any]:
        try:
            runtime.invoicing.approve_reminder(reminder_id, body=payload.body)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ReminderSendError as exc:
            raise HTTPException(status_code=502, detail=f"{exc}. De herinnering blijft staan.")
        return {"status": "verzonden"}

    @router.post("/api/reminders/{reminder_id}/reject", dependencies=[ui_auth])
    def reject_reminder(reminder_id: int) -> dict[str, Any]:
        try:
            runtime.invoicing.reject_reminder(reminder_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"status": "afgewezen"}

    @router.get("/api/reports", dependencies=[ui_auth])
    def reports(days: int = Query(30, ge=1, le=366)) -> dict[str, Any]:
        env = runtime.env()
        return summary(
            runtime.store,
            datetime.now(timezone.utc),
            days=days,
            price_input=env.get("WERKER_AI_PRICE_INPUT"),
            price_output=env.get("WERKER_AI_PRICE_OUTPUT"),
        )

    return router
