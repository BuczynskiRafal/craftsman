"""The task record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_PRIORITY = 2
PRIORITIES = range(4)


@dataclass
class Task:
    id: int
    title: str
    done: bool = False
    priority: int = DEFAULT_PRIORITY

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "done": self.done, "priority": self.priority}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            int(data["id"]),
            str(data["title"]),
            bool(data.get("done", False)),
            int(data.get("priority") or DEFAULT_PRIORITY),
        )
