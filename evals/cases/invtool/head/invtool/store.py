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


def search(
    items: list[Item],
    term: str,
    min_qty: int | None = None,
    max_price: Decimal | None = None,
) -> list[Item]:
    """Items whose name contains ``term`` and that pass the optional limits."""
    needle = term.strip().lower()
    found = []
    for item in items:
        if needle not in item.name:
            continue
        if min_qty is not None and item.qty < min_qty:
            continue
        if max_price is not None and item.price > max_price:
            continue
        found.append(item)
    return found
