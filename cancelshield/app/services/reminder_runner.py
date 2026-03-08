from __future__ import annotations

from datetime import date

from app.db import get_conn
from app.services.notifier import dispatch_team_notification
from app.services.reminder import build_reminder_schedule


def run_due_reminders(team_id: int, run_date: date) -> tuple[int, list[int]]:
    queued_count = 0
    touched: set[int] = set()

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, vendor, owner_email, renewal_date
            FROM subscriptions
            WHERE team_id = ?
            ORDER BY renewal_date ASC
            """,
            (team_id,),
        ).fetchall()

        for row in rows:
            subscription_id = int(row["id"])
            renewal_date = date.fromisoformat(row["renewal_date"])
            schedule = build_reminder_schedule(renewal_date)
            if run_date not in schedule:
                continue

            exists = conn.execute(
                """
                SELECT id FROM reminder_events
                WHERE subscription_id = ?
                  AND reminder_date = ?
                  AND channel = 'system'
                """,
                (subscription_id, run_date.isoformat()),
            ).fetchone()
            if exists:
                continue

            detail = f"renewal reminder: {row['vendor']} owner={row['owner_email']}"
            conn.execute(
                """
                INSERT INTO reminder_events (
                    subscription_id, reminder_date, channel, status, detail
                ) VALUES (?, ?, 'system', 'queued', ?)
                """,
                (
                    subscription_id,
                    run_date.isoformat(),
                    detail,
                ),
            )
            queued_count += 1
            touched.add(subscription_id)

    for subscription_id in sorted(touched):
        dispatch_team_notification(
            team_id=team_id,
            event_type="renewal_due",
            title="CancelShield Reminder",
            message=f"subscription #{subscription_id} has a reminder on {run_date.isoformat()}",
            metadata={"subscription_id": subscription_id, "run_date": run_date.isoformat()},
        )

    return queued_count, sorted(touched)


def run_due_reminders_for_all_teams(run_date: date) -> dict[int, tuple[int, list[int]]]:
    results: dict[int, tuple[int, list[int]]] = {}
    with get_conn() as conn:
        teams = conn.execute("SELECT id FROM teams ORDER BY id ASC").fetchall()

    for team in teams:
        team_id = int(team["id"])
        results[team_id] = run_due_reminders(team_id, run_date)

    return results
