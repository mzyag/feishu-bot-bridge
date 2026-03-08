from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends

from app.schemas import ReminderRunOut
from app.security import AuthContext, require_roles
from app.services.reminder_runner import run_due_reminders

router = APIRouter(prefix="/api/v1/reminders", tags=["reminders"])


@router.post("/run", response_model=ReminderRunOut)
def run_team_reminders(auth: AuthContext = Depends(require_roles("admin", "editor"))) -> ReminderRunOut:
    run_date = date.today()
    queued_count, subscriptions = run_due_reminders(auth.team_id, run_date)
    return ReminderRunOut(
        team_id=auth.team_id,
        triggered_on=run_date,
        queued_count=queued_count,
        subscriptions=subscriptions,
    )
