"""Tracking numbers and the events a carrier reports for them."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from orderdesk.models import Order
from orderdesk.shipping import advance

PREFIXES = {"post": "94", "express": "JD"}
EVENT_STATUS = {"picked_up": "shipped", "in_transit": "shipped", "delivered": "delivered"}


@dataclass(frozen=True)
class TrackingEvent:
    tracking: str
    status: str
    at: str


def _check_digit(body: str) -> int:
    total = sum((3 if i % 2 else 1) * (ord(c) % 10) for i, c in enumerate(body))
    return (10 - total % 10) % 10


def new_tracking_number(carrier: str, serial: int) -> str:
    body = f"{PREFIXES[carrier]}{serial:010d}"
    return f"{body}{_check_digit(body)}"


def is_valid(number: str) -> bool:
    return len(number) > 2 and number[-1].isdigit() and _check_digit(number[:-1]) == int(number[-1])


def parse_event(raw: dict[str, Any]) -> TrackingEvent:
    """A TrackingEvent from a carrier payload; KeyError or ValueError when it is malformed."""
    status = EVENT_STATUS[raw["status"]]
    datetime.fromisoformat(raw["at"])
    return TrackingEvent(str(raw["tracking"]), status, raw["at"])


def apply_events(order: Order, raw_events: Iterable[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """Apply carrier events to the order. Returns (events applied, raw events rejected)."""
    applied = 0
    rejected: list[dict[str, Any]] = []
    for raw in raw_events:
        try:
            event = parse_event(raw)
        except (KeyError, ValueError):
            continue
        if event.tracking != order.tracking:
            rejected.append(raw)
            continue
        if event.status != order.status:
            try:
                advance(order, event.status, f"carrier event at {event.at}")
            except ValueError:
                rejected.append(raw)
                continue
        applied += 1
    return applied, rejected
