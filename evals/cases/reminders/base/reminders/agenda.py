"""Reminders kept in memory, ordered by due time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Reminder:
    id: str
    message: str
    due: datetime


class Agenda:
    def __init__(self) -> None:
        self._items: dict[str, Reminder] = {}

    def add(self, reminder: Reminder) -> None:
        if reminder.due.tzinfo is None:
            raise ValueError("due must be timezone-aware")
        if reminder.id in self._items:
            raise ValueError(f"duplicate reminder: {reminder.id}")
        self._items[reminder.id] = reminder

    def remove(self, reminder_id: str) -> bool:
        return self._items.pop(reminder_id, None) is not None

    def due_between(self, start: datetime, end: datetime) -> list[Reminder]:
        found = [r for r in self._items.values() if start <= r.due < end]
        return sorted(found, key=lambda r: r.due)

    def overdue(self, now: datetime) -> list[Reminder]:
        return self.due_between(EARLIEST, now)
