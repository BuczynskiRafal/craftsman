"""Reminders kept in memory, ordered by due time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Reminder:
    id: str
    message: str
    due: datetime


class Agenda:
    def __init__(self, tz: tzinfo = timezone.utc) -> None:
        self._items: dict[str, Reminder] = {}
        self._tz = tz

    def add(self, reminder: Reminder) -> None:
        if reminder.due.tzinfo is None:
            raise ValueError("due must be timezone-aware")
        if reminder.id in self._items:
            raise ValueError(f"duplicate reminder: {reminder.id}")
        self._items[reminder.id] = reminder

    def add_local(self, reminder_id: str, message: str, text: str) -> Reminder:
        due = datetime.fromisoformat(text)
        if due.tzinfo is None:
            due = due.replace(tzinfo=self._tz)
        reminder = Reminder(reminder_id, message, due)
        self.add(reminder)
        return reminder

    def remove(self, reminder_id: str) -> bool:
        return self._items.pop(reminder_id, None) is not None

    def due_between(self, start: datetime, end: datetime) -> list[Reminder]:
        found = [r for r in self._items.values() if start <= r.due < end]
        return sorted(found, key=lambda r: r.due)

    def overdue(self, now: datetime) -> list[Reminder]:
        return self.due_between(EARLIEST, now)

    def digest(self, days: int, now: datetime | None = None) -> dict[str, list[Reminder]]:
        start = now or datetime.now()
        end = start + timedelta(days=days)
        groups: dict[str, list[Reminder]] = {}
        for reminder in sorted(self._items.values(), key=lambda r: r.due):
            if start <= reminder.due <= end:
                day = reminder.due.astimezone(timezone.utc).date().isoformat()
                groups.setdefault(day, []).append(reminder)
        return groups
