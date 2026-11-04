"""Command line interface of feedbox."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TextIO

from feedbox import digest, exporter, query, render, tags
from feedbox.parser import FeedError, parse_rss
from feedbox.store import Library

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date(text: str) -> str:
    if not DATE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a date like 2026-03-01")
    return text


def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--feed")
    parser.add_argument("--unread", action="store_true")
    parser.add_argument("--tag", action="append", default=[], help="only feeds that have every given tag")
    parser.add_argument("--since", type=_date, help="only articles published on or after DATE")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feedbox")
    parser.add_argument("--library", type=Path, default=Path("library.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("import", help="add the articles of a saved RSS file")
    load.add_argument("files", type=Path, nargs="+")
    listing = sub.add_parser("list", help="list articles, newest first")
    _add_filters(listing)
    listing.add_argument("--limit", type=int)
    show = sub.add_parser("show", help="print one article")
    show.add_argument("id")
    sub.add_parser("feeds", help="list the feeds")
    summary = sub.add_parser("digest", help="unread articles per feed")
    summary.add_argument("--since", type=_date, help="only articles published on or after DATE")
    summary.add_argument("--per-feed", type=int, default=3, help="titles shown for each feed")
    tag = sub.add_parser("tag", help="add or remove a tag of a feed")
    tag.add_argument("feed")
    tag.add_argument("tag")
    tag.add_argument("--remove", action="store_true")
    export = sub.add_parser("export", help="write articles as files into a folder")
    export.add_argument("dest", type=Path)
    _add_filters(export)
    export.add_argument("--format", choices=exporter.FORMATS, default="html", help="md (the default) or html")
    return parser


def main(argv: list[str] | None = None, out: TextIO = sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    library = Library.load(args.library)
    if args.command == "import":
        failed = False
        for path in args.files:
            try:
                feed, articles = parse_rss(path.read_text(encoding="utf-8"), path.name)
            except (OSError, FeedError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                failed = True
                continue
            library.add_feed(feed.name, feed.url)
            added = library.add_articles(articles)
            print(f"{feed.name}: {added} new of {len(articles)}", file=out)
        library.save()
        return 1 if failed else 0
    elif args.command in ("list", "export"):
        chosen = query.select(
            library.articles.values(),
            args.feed,
            args.unread,
            getattr(args, "limit", None),
            args.since,
            args.tag,
            library.feeds,
        )
        if args.command == "list":
            for article in chosen:
                print(render.summary_line(article), file=out)
        else:
            written = exporter.export(chosen, args.dest, args.format)
            print(f"wrote {len(written)} files to {args.dest}", file=out)
    elif args.command == "show":
        article = library.articles.get(args.id)
        if article is None:
            print(f"no article {args.id}", file=sys.stderr)
            return 1
        print(render.full_text(article), file=out)
        library.mark_read(article.id)
        library.save()
    elif args.command == "digest":
        out.write(digest.build_digest(library.articles.values(), args.since, args.per_feed))
    elif args.command == "tag":
        feed = library.feeds.get(args.feed)
        if feed is None:
            print(f"no feed {args.feed}", file=sys.stderr)
            return 1
        changed = tags.remove_tag(feed, args.tag) if args.remove else tags.add_tag(feed, args.tag)
        library.save()
        print(f"{feed.name}: {', '.join(feed.tags) or '(no tags)'}" + ("" if changed else " (unchanged)"), file=out)
    else:
        for feed in library.feeds.values():
            count = sum(1 for a in library.articles.values() if a.feed == feed.name)
            print(f"{feed.name}  {count} articles  [{', '.join(feed.tags)}]", file=out)
    return 0
