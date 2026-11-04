"""Command line interface of feedbox."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

from feedbox import query, render
from feedbox.parser import FeedError, parse_rss
from feedbox.store import Library


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feedbox")
    parser.add_argument("--library", type=Path, default=Path("library.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("import", help="add the articles of a saved RSS file")
    load.add_argument("file", type=Path)
    listing = sub.add_parser("list", help="list articles, newest first")
    listing.add_argument("--feed")
    listing.add_argument("--unread", action="store_true")
    listing.add_argument("--limit", type=int)
    show = sub.add_parser("show", help="print one article")
    show.add_argument("id")
    sub.add_parser("feeds", help="list the feeds")
    return parser


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    library = Library.load(args.library)
    if args.command == "import":
        try:
            feed, articles = parse_rss(args.file.read_text(encoding="utf-8"), args.file.name)
        except (OSError, FeedError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        library.add_feed(feed.name, feed.url)
        added = library.add_articles(articles)
        library.save()
        print(f"{feed.name}: {added} new of {len(articles)}", file=out)
    elif args.command == "list":
        for article in query.select(library.articles.values(), args.feed, args.unread, args.limit):
            print(render.summary_line(article), file=out)
    elif args.command == "show":
        article = library.articles.get(args.id)
        if article is None:
            print(f"no article {args.id}", file=sys.stderr)
            return 1
        print(render.full_text(article), file=out)
        library.mark_read(article.id)
        library.save()
    else:
        for feed in library.feeds.values():
            count = sum(1 for a in library.articles.values() if a.feed == feed.name)
            print(f"{feed.name}  {count} articles", file=out)
    return 0
