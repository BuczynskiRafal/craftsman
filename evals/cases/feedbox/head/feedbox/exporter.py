"""Write articles to a folder as Markdown or HTML files, with an index."""

from __future__ import annotations

import html
from collections.abc import Sequence
from pathlib import Path

from feedbox.model import Article
from feedbox.slug import slugify

FORMATS = ("md", "html")
INDEX_NAME = "index.md"


def render_markdown(article: Article) -> str:
    lines = [f"# {article.title}", ""]
    byline = " | ".join(part for part in (article.author, article.published[:10], article.feed) if part)
    lines += [f"*{byline}*", "", article.body or "(no text)", "", f"[Original]({article.link})", ""]
    return "\n".join(lines)


def render_html(article: Article) -> str:
    title = html.escape(article.title)
    byline = html.escape(" | ".join(p for p in (article.author, article.published[:10], article.feed) if p))
    return (
        "<!doctype html>\n<meta charset=\"utf-8\">\n"
        f"<title>{title}</title>\n<h1>{title}</h1>\n<p><em>{byline}</em></p>\n"
        f"<p>{html.escape(article.body) or '(no text)'}</p>\n"
        f"<p><a href=\"{html.escape(article.link, quote=True)}\">Original</a></p>\n"
    )


def article_path(dest: Path, article: Article, fmt: str) -> Path:
    """dest/<feed>/<date>-<title>.<fmt>, with 'undated' when the article has no date."""
    day = article.published[:10] or "undated"
    return dest / article.feed / f"{day}-{slugify(article.title)}.{fmt}"


def unique_path(path: Path) -> Path:
    """The path itself, or the first of name-2, name-3, ... that does not exist yet."""
    candidate = path
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        number += 1
    return candidate


def write_index(dest: Path, entries: Sequence[tuple[Article, Path]]) -> Path:
    """index.md in dest with one link per exported file."""
    lines = ["# Exported articles", ""]
    for article, path in entries:
        lines.append(f"- [{article.title}]({path.relative_to(dest)})")
    index = dest / INDEX_NAME
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def export(articles: Sequence[Article], dest: Path, fmt: str = "md") -> list[Path]:
    """Write every article to its own file under dest; existing files are never overwritten."""
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    render = render_markdown if fmt == "md" else render_html
    dest.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[Article, Path]] = []
    for article in articles:
        path = unique_path(article_path(dest, article, fmt))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(article), encoding="utf-8")
        entries.append((article, path))
    write_index(dest, entries)
    return [path for _, path in entries]
