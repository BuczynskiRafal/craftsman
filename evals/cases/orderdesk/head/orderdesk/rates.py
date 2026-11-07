"""Carrier rate tables: what a parcel costs by carrier, zone and weight."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from orderdesk.zones import ZONES


class RateError(ValueError):
    """Unknown carrier or zone, or a parcel the carrier cannot take."""


@dataclass(frozen=True)
class Tier:
    up_to_g: int
    cents: int


def _check(carrier: str, zone: str, tiers: list[Tier]) -> None:
    where = f"{carrier}/{zone}"
    if zone not in ZONES:
        raise RateError(f"{where}: unknown zone")
    if not tiers:
        raise RateError(f"{where}: no tiers")
    previous = 0
    for tier in tiers:
        if tier.up_to_g <= previous:
            raise RateError(f"{where}: tier limits must increase")
        if tier.cents < 0:
            raise RateError(f"{where}: negative price")
        previous = tier.up_to_g


class RateTable:
    """Tiers per carrier and zone; the first tier whose limit covers the weight applies."""

    def __init__(self, carriers: dict[str, dict[str, list[Tier]]]) -> None:
        for carrier, zones in carriers.items():
            for zone, tiers in zones.items():
                _check(carrier, zone, tiers)
        self._carriers = carriers
        self._quotes: dict[tuple[str, int], int] = {}

    def carriers(self) -> list[str]:
        return sorted(self._carriers)

    def max_weight_g(self, carrier: str) -> int:
        zones = self._carriers.get(carrier)
        if zones is None:
            raise RateError(f"unknown carrier {carrier!r}")
        return min(tiers[-1].up_to_g for tiers in zones.values())

    def quote(self, carrier: str, zone: str, weight_g: int) -> int:
        """Price in cents of one parcel of `weight_g` grams sent to `zone`."""
        key = (carrier, weight_g)
        if key not in self._quotes:
            self._quotes[key] = self._price(self._tiers(carrier, zone), weight_g)
        return self._quotes[key]

    def _tiers(self, carrier: str, zone: str) -> list[Tier]:
        zones = self._carriers.get(carrier)
        if zones is None:
            raise RateError(f"unknown carrier {carrier!r}")
        if zone not in zones:
            raise RateError(f"{carrier} does not serve zone {zone!r}")
        return zones[zone]

    @staticmethod
    def _price(tiers: list[Tier], weight_g: int) -> int:
        if weight_g < 0:
            raise RateError("weight cannot be negative")
        for tier in tiers:
            if weight_g <= tier.up_to_g:
                return tier.cents
        raise RateError(f"parcel of {weight_g} g is over the {tiers[-1].up_to_g} g limit")


_LIMITS = (500, 2000, 10_000, 20_000)


def _tiers(*cents: int) -> list[Tier]:
    return [Tier(limit, price) for limit, price in zip(_LIMITS, cents, strict=True)]


def default_table() -> RateTable:
    return RateTable(
        {
            "post": {
                "domestic": _tiers(450, 700, 1200, 1900),
                "remote": _tiers(750, 1000, 1500, 2200),
                "near": _tiers(900, 1400, 2400, 3600),
                "international": _tiers(1800, 2600, 4200, 6500),
            },
            "express": {
                "domestic": _tiers(900, 1300, 2000, 3000),
                "remote": _tiers(1300, 1700, 2400, 3400),
                "near": _tiers(1600, 2200, 3300, 4800),
                "international": _tiers(2800, 3900, 6000, 8800),
            },
        }
    )


def load_table(path: Path) -> RateTable:
    """Read {"carrier": {"zone": [[limit_g, cents], ...]}} from a JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return RateTable(
        {
            carrier: {zone: [Tier(int(g), int(c)) for g, c in tiers] for zone, tiers in zones.items()}
            for carrier, zones in raw.items()
        }
    )
