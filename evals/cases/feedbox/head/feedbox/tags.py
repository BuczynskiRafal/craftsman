"""Tags on feeds."""

from __future__ import annotations

from feedbox.model import Feed


def normalize(tag: str) -> str:
    return tag.strip().lower()


def parse_tags(text: str) -> list[str]:
    """Tags from a comma separated string: normalized, without empty ones or repeats."""
    tags: list[str] = []
    for part in text.split(","):
        tag = normalize(part)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def add_tag(feed: Feed, tag: str) -> bool:
    """Give the feed a tag; False when it has it already or the tag is empty."""
    tag = normalize(tag)
    if not tag or tag in feed.tags:
        return False
    feed.tags.append(tag)
    return True


def remove_tag(feed: Feed, tag: str) -> bool:
    tag = normalize(tag)
    if tag not in feed.tags:
        return False
    feed.tags.remove(tag)
    return True
