"""Command line interface of the order desk."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from orderdesk import pricing, report, shipping, tracking
from orderdesk.catalog import build_line
from orderdesk.models import Address, Order
from orderdesk.money import format_cents, to_cents
from orderdesk.rates import RateError, RateTable, default_table, load_table
from orderdesk.store import OrderStore
from orderdesk.zones import delivery_days


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orderdesk")
    parser.add_argument("--dir", type=Path, default=Path("orders"))
    parser.add_argument("--rates", type=Path, help="JSON rate table instead of the built-in one")
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create an order")
    new.add_argument("customer")
    new.add_argument("--ship-to", nargs=4, metavar=("NAME", "LINE1", "CITY", "POSTCODE"), required=True)
    new.add_argument("--country", default="US")
    new.add_argument("--item", action="append", default=[], metavar="SKU:QTY")
    new.add_argument("--discount", default="0")
    show = sub.add_parser("show", help="print one order")
    show.add_argument("id")
    sub.add_parser("list", help="list the orders")
    summary = sub.add_parser("report", help="daily summary or CSV export")
    summary.add_argument("--csv", action="store_true")
    quote = sub.add_parser("quote", help="shipping price of an order with every carrier")
    quote.add_argument("id")
    ship = sub.add_parser("ship", help="choose the carrier, price the shipping and number the parcel")
    ship.add_argument("id")
    ship.add_argument("--carrier", required=True)
    ship.add_argument("--override", help="fixed shipping amount set by staff; 0 waives it")
    move = sub.add_parser("advance", help="move an order to its next status")
    move.add_argument("id")
    move.add_argument("status")
    track = sub.add_parser("track", help="apply carrier events from a JSON file")
    track.add_argument("id")
    track.add_argument("events", type=Path)
    return parser


def _show(order: Order, out: TextIO) -> None:
    print(f"{order.id}  {order.customer}  [{order.status}]", file=out)
    for line in order.lines:
        print(f"  {line.qty} x {line.sku}  {format_cents(line.total_cents())}", file=out)
    print(f"  subtotal {format_cents(order.subtotal_cents())}", file=out)
    if order.discount_cents:
        print(f"  discount -{format_cents(order.discount_cents)}", file=out)
    if order.carrier:
        print(f"  shipping {format_cents(order.shipping_cents)} ({order.carrier})", file=out)
    print(f"  tax      {format_cents(pricing.tax_cents(order))}", file=out)
    print(f"  total    {format_cents(pricing.total_cents(order))}", file=out)
    if order.tracking:
        print(f"  tracking {order.tracking}", file=out)
    country, postcode = order.address.country, order.address.postcode
    print(f"  delivery about {delivery_days(country, postcode)} days after dispatch", file=out)


def _table(path: Path | None) -> RateTable:
    return load_table(path) if path is not None else default_table()


def _ship(order: Order, table: RateTable, args: argparse.Namespace, out: TextIO) -> None:
    if args.override is not None:
        order.shipping_override_cents = to_cents(args.override)
    cost = shipping.apply_shipping(order, table, args.carrier)
    order.tracking = tracking.new_tracking_number(args.carrier, int(order.id[1:]))
    print(f"{order.id}  {args.carrier}  shipping {format_cents(cost)}  tracking {order.tracking}", file=out)


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    store = OrderStore(args.dir)
    if args.command == "new":
        order = Order(
            id=store.next_id(),
            customer=args.customer,
            address=Address(*args.ship_to, country=args.country),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            discount_cents=to_cents(args.discount),
        )
        for item in args.item:
            sku, _, qty = item.partition(":")
            order.lines.append(build_line(sku, int(qty or "1")))
        store.save(order)
        print(order.id, file=out)
    elif args.command in ("show", "quote", "ship", "advance", "track"):
        try:
            order = store.load(args.id)
        except KeyError:
            print(f"no order {args.id}", file=sys.stderr)
            return 1
        try:
            if args.command == "show":
                _show(order, out)
            elif args.command == "quote":
                table = _table(args.rates)
                for carrier in table.carriers():
                    print(f"{carrier}  {format_cents(shipping.quote_shipping(order, table, carrier))}", file=out)
            elif args.command == "ship":
                _ship(order, _table(args.rates), args, out)
                store.save(order)
            elif args.command == "advance":
                shipping.advance(order, args.status)
                store.save(order)
            else:
                events = json.loads(args.events.read_text(encoding="utf-8"))
                applied, rejected = tracking.apply_events(order, events)
                store.save(order)
                print(f"applied {applied}, rejected {len(rejected)}", file=out)
        except (ValueError, RateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    elif args.command == "list":
        for order in store.all():
            print(f"{order.id}  {order.customer}  {format_cents(pricing.total_cents(order))}", file=out)
    else:
        orders = store.all()
        if args.csv:
            out.write(report.to_csv(orders))
        else:
            for day, entry in sorted(report.daily_summary(orders).items()):
                print(f"{day}  {entry['orders']} orders  {format_cents(entry['revenue_cents'])}", file=out)
    return 0
