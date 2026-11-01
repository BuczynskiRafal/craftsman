"""Product catalog queries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int


def list_products(products: list[Product], query: str = "") -> list[Product]:
    """Return products whose name contains `query` (case-insensitive), sorted by name."""
    needle = query.strip().lower()
    matches = [p for p in products if needle in p.name.lower()]
    return sorted(matches, key=lambda p: p.name)
