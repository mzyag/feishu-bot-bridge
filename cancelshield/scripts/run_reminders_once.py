from __future__ import annotations

from datetime import date

from app.db import init_db
from app.services.reminder_runner import run_due_reminders_for_all_teams


def main() -> None:
    init_db()
    today = date.today()
    results = run_due_reminders_for_all_teams(today)

    if not results:
        print("no teams")
        return

    for team_id, (queued_count, subscriptions) in results.items():
        print(
            f"team={team_id} date={today.isoformat()} queued={queued_count} subscriptions={subscriptions}"
        )


if __name__ == "__main__":
    main()
