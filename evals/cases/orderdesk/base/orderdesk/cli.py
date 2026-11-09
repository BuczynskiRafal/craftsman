"""Command line interface of the order desk."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from orderdesk import pricing, report
from orderdesk.catalog import build_line
from orderdesk.models import Address, Order
from orderdesk.money import format_cents, to_cents
from orderdesk.store import OrderStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orderdesk")
    parser.add_argument("--dir", type=Path, default=Path("orders"))
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
    return parser


def _show(order: Order, out: TextIO) -> None:
    print(f"{order.id}  {order.customer}  [{order.status}]", file=out)
    for line in order.lines:
        print(f"  {line.qty} x {line.sku}  {format_cents(line.total_cents())}", file=out)
    print(f"  subtotal {format_cents(order.subtotal_cents())}", file=out)
    if order.discount_cents:
        print(f"  discount -{format_cents(order.discount_cents)}", file=out)
    print(f"  tax      {format_cents(pricing.tax_cents(order))}", file=out)
    print(f"  total    {format_cents(pricing.total_cents(order))}", file=out)


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
    elif args.command == "show":
        try:
            _show(store.load(args.id), out)
        except KeyError:
            print(f"no order {args.id}", file=sys.stderr)
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
