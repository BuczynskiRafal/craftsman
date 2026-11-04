"""Order data model and its JSON form."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Address:
    name: str
    line1: str
    city: str
    postcode: str
    country: str = "US"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "line1": self.line1,
            "city": self.city,
            "postcode": self.postcode,
            "country": self.country,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Address:
        return cls(
            data["name"], data["line1"], data["city"], data["postcode"], data.get("country", "US")
        )


@dataclass
class Line:
    sku: str
    qty: int
    unit_cents: int
    weight_g: int = 0

    def total_cents(self) -> int:
        return self.qty * self.unit_cents


@dataclass
class Order:
    id: str
    customer: str
    address: Address
    lines: list[Line] = field(default_factory=list)
    created_at: str = ""
    status: str = "new"
    discount_cents: int = 0

    def subtotal_cents(self) -> int:
        return sum(line.total_cents() for line in self.lines)

    def weight_g(self) -> int:
        return sum(line.qty * line.weight_g for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer": self.customer,
            "address": self.address.to_dict(),
            "lines": [
                {"sku": l.sku, "qty": l.qty, "unit_cents": l.unit_cents, "weight_g": l.weight_g}
                for l in self.lines
            ],
            "created_at": self.created_at,
            "status": self.status,
            "discount_cents": self.discount_cents,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Order:
        return cls(
            id=data["id"],
            customer=data["customer"],
            address=Address.from_dict(data["address"]),
            lines=[
                Line(l["sku"], l["qty"], l["unit_cents"], l.get("weight_g", 0))
                for l in data.get("lines", [])
            ],
            created_at=data.get("created_at", ""),
            status=data.get("status", "new"),
            discount_cents=data.get("discount_cents", 0),
        )
