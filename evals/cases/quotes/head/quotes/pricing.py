"""Price quotes for order lines."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

BASE = "USD"
CENT = Decimal("0.01")
BULK_QTY = 10
BULK_FACTOR = Decimal("0.95")

RateSource = Callable[[str], Decimal]


class UnknownSku(KeyError):
    pass


class RateUnavailable(Exception):
    def __init__(self, currency: str) -> None:
        super().__init__(f"no exchange rate for {currency}")
        self.currency = currency


@dataclass(frozen=True)
class Line:
    sku: str
    qty: int


@dataclass(frozen=True)
class Summary:
    total: Decimal
    currency: str
    unknown: list[str]


class Quoter:
    def __init__(self, prices: Mapping[str, Decimal], rates: RateSource) -> None:
        self._prices = prices
        self._rates = rates
        self._cache: dict[tuple[str, int], Decimal] = {}

    def quote(self, sku: str, qty: int, currency: str = BASE) -> Decimal:
        key = (sku, qty)
        if key not in self._cache:
            self._cache[key] = self._convert(self._compute(sku, qty), currency)
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

    def _convert(self, amount: Decimal, currency: str) -> Decimal:
        if currency == BASE:
            return amount
        try:
            rate = self._rates(currency)
        except (KeyError, OSError):
            return amount
        return (amount * rate).quantize(CENT, ROUND_HALF_EVEN)

    def total(self, lines: Sequence[Line], currency: str = BASE) -> Decimal:
        return sum((self.quote(line.sku, line.qty, currency) for line in lines), Decimal("0"))

    def summarize(
        self, lines: Sequence[Line], currency: str = BASE, unknown: list[str] = []
    ) -> Summary:
        priced = []
        for line in lines:
            try:
                priced.append(self.quote(line.sku, line.qty, currency))
            except UnknownSku:
                unknown.append(line.sku)
        return Summary(sum(priced, Decimal("0")), currency, unknown)
