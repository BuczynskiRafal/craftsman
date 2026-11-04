"""Reading RSS 2.0 documents."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

from feedbox.model import Article, Feed


class FeedError(Exception):
    """The document is not a usable RSS feed."""


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_date(text: str) -> str:
    """An RFC 822 date as ISO 8601 in UTC; empty when the text is not a date."""
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_rss(xml_text: str, url: str) -> tuple[Feed, list[Article]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML: {exc}") from exc
    channel = root.find("channel")
    if channel is None:
        raise FeedError("no <channel> element")
    name = _text(channel, "title") or url
    articles = []
    for item in channel.findall("item"):
        link = _text(item, "link")
        article_id = _text(item, "guid") or link
        if not article_id:
            continue
        articles.append(
            Article(
                id=article_id,
                feed=name,
                title=_text(item, "title") or "(untitled)",
                link=link,
                published=parse_date(_text(item, "pubDate")),
                body=strip_tags(_text(item, "description")),
            )
        )
    return Feed(name, url), articles
