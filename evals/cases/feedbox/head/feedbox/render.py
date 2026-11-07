"""Text output for articles."""

from __future__ import annotations

import textwrap

from feedbox.model import Article


def summary_line(article: Article) -> str:
    mark = " " if article.read else "*"
    day = article.published[:10] or "----------"
    return f"{mark} {day}  {article.feed}: {article.title}"


def full_text(article: Article, width: int = 72) -> str:
    head = [article.title, f"{article.feed}  {article.published[:10]}", article.link, ""]
    body = textwrap.fill(article.body, width=width) if article.body else "(no text)"
    return "\n".join([*head, body])
