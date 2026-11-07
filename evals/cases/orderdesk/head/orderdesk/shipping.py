"""Shipping cost of an order and the status flow of a shipment."""

from __future__ import annotations

from orderdesk import pricing
from orderdesk.models import Order
from orderdesk.rates import RateError, RateTable
from orderdesk.zones import zone_for

FREE_SHIPPING_CENTS = 7500
HANDLING_CENTS = 150
MAX_PARCEL_G = 20_000

STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "new": ("packed", "cancelled"),
    "packed": ("shipped", "cancelled"),
    "shipped": ("delivered",),
    "delivered": (),
    "cancelled": (),
}


def parcel_count(weight_g: int) -> int:
    """Number of parcels an order needs; a parcel holds at most MAX_PARCEL_G."""
    return max(-(-weight_g // MAX_PARCEL_G), 1)


def quote_shipping(order: Order, table: RateTable, carrier: str) -> int:
    """Shipping in cents: the staff override if there is one, free above the threshold, else the rate."""
    if carrier not in table.carriers():
        raise RateError(f"unknown carrier {carrier!r}")
    override = order.shipping_override_cents
    if override:
        return override
    if pricing.net_cents(order) > FREE_SHIPPING_CENTS:
        return 0
    zone = zone_for(order.address.country, order.address.postcode)
    parcels = parcel_count(order.weight_g())
    per_parcel_g = -(-order.weight_g() // parcels)
    return parcels * (table.quote(carrier, zone, per_parcel_g) + HANDLING_CENTS)


def apply_shipping(order: Order, table: RateTable, carrier: str) -> int:
    """Price the order with `carrier`, store the cost and the carrier on it and return the cost."""
    if order.status not in ("new", "packed"):
        raise ValueError(f"cannot change the shipping of a {order.status} order")
    order.shipping_cents = quote_shipping(order, table, carrier)
    order.carrier = carrier
    return order.shipping_cents


def advance(order: Order, status: str, note: str = "") -> None:
    """Move the order along STATUS_FLOW and record the step in its history."""
    if status not in STATUS_FLOW.get(order.status, ()):
        raise ValueError(f"an order cannot go from {order.status} to {status}")
    if status == "shipped" and not order.tracking:
        raise ValueError("an order needs a tracking number before it ships")
    order.status = status
    order.history.append({"status": status, "note": note})
