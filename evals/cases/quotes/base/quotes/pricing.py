"""Price quotes for order lines."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")
BULK_QTY = 10
BULK_FACTOR = Decimal("0.95")


class UnknownSku(KeyError):
    pass


@dataclass(frozen=True)
class Line:
    sku: str
    qty: int


class Quoter:
    def __init__(self, prices: Mapping[str, Decimal]) -> None:
        self._prices = prices
        self._cache: dict[tuple[str, int], Decimal] = {}

    def quote(self, sku: str, qty: int) -> Decimal:
        key = (sku, qty)
        if key not in self._cache:
            self._cache[key] = self._compute(sku, qty)
        return self._cache[key]

    def _compute(self, sku: str, qty: int) -> Decimal:
        if qty < 1:
            raise ValueError(f"quantity must be at least 1, got {qty}")
        try:
            unit = self._prices[sku]
        except KeyError:
            raise UnknownSku(sku) from None
        total = unit * qty
        if qty >= BULK_QTY:
            total *= BULK_FACTOR
        return total.quantize(CENT, ROUND_HALF_EVEN)

    def total(self, lines: Sequence[Line]) -> Decimal:
        return sum((self.quote(line.sku, line.qty) for line in lines), Decimal("0"))
