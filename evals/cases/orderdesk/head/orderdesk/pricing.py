"""Tax and totals of an order."""

from __future__ import annotations

from orderdesk.models import Order
from orderdesk.money import percent_of

TAX_BASIS_POINTS = {"US": 700, "CA": 500}


def net_cents(order: Order) -> int:
    return max(order.subtotal_cents() - order.discount_cents, 0)


def tax_cents(order: Order) -> int:
    return percent_of(net_cents(order), TAX_BASIS_POINTS.get(order.address.country, 0))


def total_cents(order: Order) -> int:
    """Net amount, tax and shipping. Shipping is not taxed."""
    return net_cents(order) + tax_cents(order) + order.shipping_cents
