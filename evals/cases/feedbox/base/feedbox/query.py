"""Selecting and ordering articles."""

from __future__ import annotations

from collections.abc import Iterable

from feedbox.model import Article


def select(
    articles: Iterable[Article],
    feed: str | None = None,
    unread: bool = False,
    limit: int | None = None,
) -> list[Article]:
    """Articles of a feed (all feeds when None), newest first; ties by id."""
    chosen = [
        a for a in articles if (feed is None or a.feed == feed) and not (unread and a.read)
    ]
    chosen.sort(key=lambda a: a.id)
    chosen.sort(key=lambda a: a.published, reverse=True)
    return chosen if limit is None else chosen[:limit]
