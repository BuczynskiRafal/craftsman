"""File name safe text."""

from __future__ import annotations

import re


def slugify(text: str, limit: int = 60) -> str:
    """Lower case letters and digits joined by single dashes, at most `limit` characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "untitled"
