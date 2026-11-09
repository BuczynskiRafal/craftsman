"""One JSON file per order in a directory."""

from __future__ import annotations

import json
from pathlib import Path

from orderdesk.models import Order


class OrderStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, order_id: str) -> Path:
        return self.root / f"{order_id}.json"

    def save(self, order: Order) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(order.id).write_text(json.dumps(order.to_dict(), indent=2), encoding="utf-8")

    def load(self, order_id: str) -> Order:
        try:
            text = self._path(order_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise KeyError(order_id) from None
        return Order.from_dict(json.loads(text))

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))

    def all(self) -> list[Order]:
        return [self.load(order_id) for order_id in self.ids()]

    def next_id(self) -> str:
        numbers = [int(i[1:]) for i in self.ids() if i[1:].isdigit()]
        return f"A{max(numbers, default=1000) + 1}"
