"""Inverted index and document storage."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .tokenizer import tokenize


class SearchError(Exception):
    """Base error for search index and query operations."""


class QuerySyntaxError(SearchError):
    """Raised when a search query string cannot be parsed."""


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    score: float
    facets: dict[str, list[str]]


@dataclass(frozen=True)
class SearchResponse:
    total: int
    hits: list[SearchHit]
    facets: dict[str, dict[str, int]]


class SearchIndex:
    def __init__(self) -> None:
        self.docs_text: dict[str, str] = {}
        self.docs_facets: dict[str, dict[str, list[str]]] = {}
        self.postings: dict[str, dict[str, list[int]]] = defaultdict(dict)

    @property
    def doc_count(self) -> int:
        return len(self.docs_text)

    def terms(self) -> set[str]:
        return set(self.postings)

    def doc_freq(self, term: str) -> int:
        return len(self.postings.get(term, {}))

    def add_or_update(
        self,
        doc_id: str,
        text: str,
        facets: Mapping[str, str | Sequence[str]] | None = None,
    ) -> None:
        if not doc_id.strip():
            raise SearchError("doc_id must be non-empty")
        # Starter bug: forgets to clean stale postings when overwriting doc_id.
        self.docs_text[doc_id] = text
        norm_facets: dict[str, list[str]] = {}
        if facets:
            for k, v in facets.items():
                vals = [v] if isinstance(v, str) else list(v)
                cleaned = sorted(set(vals))
                if cleaned:
                    norm_facets[k] = cleaned
        self.docs_facets[doc_id] = norm_facets
        positions: dict[str, list[int]] = defaultdict(list)
        for tok in tokenize(text):
            positions[tok.term].append(tok.position)
        for term, pos_list in positions.items():
            self.postings[term][doc_id] = pos_list

    def delete(self, doc_id: str) -> bool:
        if doc_id not in self.docs_text:
            return False
        del self.docs_text[doc_id]
        self.docs_facets.pop(doc_id, None)
        return True
