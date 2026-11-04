"""A short overview of what is new."""

from __future__ import annotations

from collections.abc import Iterable

from feedbox import query
from feedbox.model import Article


def build_digest(articles: Iterable[Article], since: str | None = None, per_feed: int = 3) -> str:
    """Per feed: how many articles are unread and the titles of the newest ones."""
    by_feed: dict[str, list[Article]] = {}
    for article in query.select(articles, unread=True, since=since):
        by_feed.setdefault(article.feed, []).append(article)
    if not by_feed:
        return "Nothing new.\n"
    lines: list[str] = []
    for name in sorted(by_feed):
        items = by_feed[name]
        lines.append(f"{name}: {len(items)} unread")
        lines += [f"  - {a.title}" for a in items[:per_feed]]
        if len(items) > per_feed:
            lines.append(f"  ... and {len(items) - per_feed} more")
    return "\n".join(lines) + "\n"
