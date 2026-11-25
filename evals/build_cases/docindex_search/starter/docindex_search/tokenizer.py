"""Tokenizer with optional stopword filtering."""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "the", "of", "to", "in", "on", "for", "with", "is", "it", "as", "at", "by", "from"}
)
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Token:
    term: str
    position: int


def tokenize(
    text: str,
    *,
    stopwords: frozenset[str] = DEFAULT_STOPWORDS,
    drop_stopwords: bool = True,
) -> list[Token]:
    # Starter bug: numbers position after dropping stopwords instead of original token offset.
    words = [m.group(0).lower() for m in _WORD_RE.finditer(text)]
    if drop_stopwords:
        words = [w for w in words if w not in stopwords]
    return [Token(term=w, position=idx) for idx, w in enumerate(words)]
