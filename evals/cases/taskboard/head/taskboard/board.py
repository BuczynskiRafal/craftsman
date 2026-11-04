"""The board: a list of tasks that can be saved and exported."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import TextIO

from taskboard.model import DEFAULT_PRIORITY, PRIORITIES, Task


class Board:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.tasks = tasks if tasks is not None else []

    def add(self, title: str, priority: int = DEFAULT_PRIORITY) -> Task:
        if not title.strip():
            raise ValueError("title must not be empty")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be 0-3, got {priority}")
        next_id = max((t.id for t in self.tasks), default=0) + 1
        task = Task(next_id, title.strip(), priority=priority)
        self.tasks.append(task)
        return task

    def complete(self, task_id: int) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                task.done = True
                return task
        raise KeyError(task_id)

    def by_priority(self) -> list[Task]:
        return sorted(self.tasks, key=lambda t: (t.priority, t.id))

    def save(self, path: Path) -> None:
        payload = [t.to_dict() for t in self.tasks]
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Board:
        if not path.exists():
            return cls()
        return cls([Task.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))])

    def export_csv(self, out: TextIO) -> None:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["id", "title", "done"])
        for task in self.tasks:
            writer.writerow([task.id, task.title, "yes" if task.done else "no"])
