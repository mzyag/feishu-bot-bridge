from __future__ import annotations

from datetime import date, timedelta


def build_reminder_schedule(renewal_date: date) -> list[date]:
    offsets = [7, 3, 1]
    reminders = [renewal_date - timedelta(days=offset) for offset in offsets]
    reminders.append(renewal_date)
    return reminders
