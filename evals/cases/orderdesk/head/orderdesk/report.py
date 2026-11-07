"""Daily summary and CSV export of orders."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from orderdesk import pricing
from orderdesk.models import Order
from orderdesk.money import format_cents


def daily_summary(orders: Iterable[Order]) -> dict[str, dict[str, int]]:
    """Order count and revenue per day (the date part of created_at); cancelled orders are left out."""
    days: dict[str, dict[str, int]] = {}
    for order in orders:
        if order.status == "cancelled":
            continue
        entry = days.setdefault(order.created_at[:10], {"orders": 0, "revenue_cents": 0})
        entry["orders"] += 1
        entry["revenue_cents"] += pricing.net_cents(order) + pricing.tax_cents(order)
    return days


def to_csv(orders: Iterable[Order]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "id",
            "customer",
            "created_at",
            "status",
            "carrier",
            "tracking",
            "subtotal",
            "discount",
            "shipping",
            "tax",
            "total",
        ]
    )
    for order in orders:
        writer.writerow(
            [
                order.id,
                order.customer,
                order.created_at,
                order.status,
                order.carrier,
                order.tracking,
                format_cents(order.subtotal_cents()),
                format_cents(order.discount_cents),
                format_cents(order.shipping_cents),
                format_cents(pricing.tax_cents(order)),
                format_cents(pricing.total_cents(order)),
            ]
        )
    return buffer.getvalue()
