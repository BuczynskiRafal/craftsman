"""Selecting and ordering articles."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from feedbox import tags as tag_tools
from feedbox.model import Article, Feed


def select(
    articles: Iterable[Article],
    feed: str | None = None,
    unread: bool = False,
    limit: int | None = None,
    since: str | None = None,
    tags: Iterable[str] = (),
    feeds: Mapping[str, Feed] | None = None,
) -> list[Article]:
    """Articles of a feed (all feeds when None), newest first; ties by id.

    `since` is a YYYY-MM-DD date: only articles published on that day or later (undated ones
    never match). `tags` keeps the articles of feeds that have the tags; the feeds are
    looked up in `feeds`.
    """
    wanted = {tag_tools.normalize(t) for t in tags}
    known = feeds or {}
    chosen = []
    for a in articles:
        if feed is not None and a.feed != feed:
            continue
        if unread and a.read:
            continue
        if since is not None and a.published[:10] < since:
            continue
        if wanted:
            owner = known.get(a.feed)
            if owner is None or not wanted & set(owner.tags):
                continue
        chosen.append(a)
    chosen.sort(key=lambda a: a.id)
    chosen.sort(key=lambda a: a.published, reverse=True)
    return chosen if limit is None else chosen[:limit]
