"""The library: feeds and articles in one JSON file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from feedbox.model import Article, Feed


def _article_to_dict(a: Article) -> dict[str, Any]:
    return {
        "id": a.id,
        "feed": a.feed,
        "title": a.title,
        "link": a.link,
        "published": a.published,
        "body": a.body,
        "read": a.read,
    }


def _article_from_dict(d: dict[str, Any]) -> Article:
    return Article(
        d["id"], d["feed"], d["title"], d["link"], d["published"], d.get("body", ""), d.get("read", False)
    )


class Library:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.feeds: dict[str, Feed] = {}
        self.articles: dict[str, Article] = {}

    @classmethod
    def load(cls, path: Path) -> Library:
        library = cls(path)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            library.feeds = {f["name"]: Feed(f["name"], f["url"], list(f.get("tags", []))) for f in data.get("feeds", [])}
            library.articles = {a["id"]: _article_from_dict(a) for a in data.get("articles", [])}
        return library

    def save(self) -> None:
        data = {
            "feeds": [{"name": f.name, "url": f.url, "tags": f.tags} for f in self.feeds.values()],
            "articles": [_article_to_dict(a) for a in self.articles.values()],
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add_feed(self, name: str, url: str, tags: list[str] = []) -> Feed:
        feed = self.feeds.get(name)
        if feed is None:
            feed = self.feeds[name] = Feed(name, url, tags)
        return feed

    def add_articles(self, articles: list[Article]) -> int:
        """Add the articles that are not in the library yet; returns how many were new."""
        new = [a for a in articles if a.id not in self.articles]
        for article in new:
            self.articles[article.id] = article
        return len(new)

    def mark_read(self, article_id: str) -> None:
        self.articles[article_id].read = True
