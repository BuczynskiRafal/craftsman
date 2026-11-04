"""CSV-backed inventory storage."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

FIELDS = ["sku", "name", "qty", "price"]


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    qty: int
    price: Decimal


def load(path: Path) -> list[Item]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [
            Item(row["sku"], row["name"], int(row["qty"]), Decimal(row["price"]))
            for row in csv.DictReader(fh)
        ]


def save(path: Path, items: list[Item]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELDS)
        for item in sorted(items, key=lambda i: i.sku):
            writer.writerow([item.sku, item.name, item.qty, item.price])


def add(items: list[Item], item: Item) -> list[Item]:
    if any(existing.sku == item.sku for existing in items):
        raise ValueError(f"duplicate sku: {item.sku}")
    return [*items, item]
