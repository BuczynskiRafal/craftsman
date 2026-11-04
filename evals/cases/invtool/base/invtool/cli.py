"""Command line interface for the inventory."""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

from invtool import store


def format_item(item: store.Item) -> str:
    return f"{item.sku:<8} {item.name:<20} {item.qty:>5} {item.price:>9}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invtool")
    parser.add_argument("--file", type=Path, default=Path("inventory.csv"), help="CSV file to use")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print every item")
    add = sub.add_parser("add", help="add an item")
    add.add_argument("sku")
    add.add_argument("name")
    add.add_argument("qty", type=int)
    add.add_argument("price")
    return parser


def parse_price(text: str) -> Decimal | None:
    try:
        price = Decimal(text)
    except InvalidOperation:
        return None
    return price if price.is_finite() else None


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    items = store.load(args.file)
    if args.command == "add":
        price = parse_price(args.price)
        if price is None:
            print(f"invalid price: {args.price}", file=sys.stderr)
            return 2
        try:
            items = store.add(items, store.Item(args.sku, args.name, args.qty, price))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        store.save(args.file, items)
        return 0
    for item in items:
        print(format_item(item), file=out)
    return 0
