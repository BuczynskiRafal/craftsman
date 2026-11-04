"""The product catalog and order line construction."""

from __future__ import annotations

from dataclasses import dataclass

from orderdesk.models import Line


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit_cents: int
    weight_g: int


PRODUCTS = {
    p.sku: p
    for p in (
        Product("MUG-01", "Enamel mug", 1200, 350),
        Product("TEA-05", "Loose leaf tea, 250 g", 950, 290),
        Product("KETTLE-2", "Stovetop kettle", 4900, 1400),
        Product("BOARD-L", "Oak serving board", 6400, 2600),
    )
}


def build_line(sku: str, qty: int) -> Line:
    if qty < 1:
        raise ValueError("quantity must be at least 1")
    try:
        product = PRODUCTS[sku]
    except KeyError:
        raise ValueError(f"unknown sku {sku!r}") from None
    return Line(product.sku, qty, product.unit_cents, product.weight_g)
