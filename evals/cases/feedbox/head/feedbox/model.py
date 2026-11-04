"""Feeds and articles."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Feed:
    name: str
    url: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Article:
    id: str
    feed: str
    title: str
    link: str
    published: str  # ISO 8601 in UTC, e.g. 2026-03-02T12:00:00+00:00; empty when the feed gave none
    body: str = ""
    read: bool = False
    author: str = ""
