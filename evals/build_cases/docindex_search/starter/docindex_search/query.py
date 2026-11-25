"""Query execution, boolean parsing, phrases, and facets."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .index import SearchHit, SearchIndex, SearchResponse
from .tokenizer import tokenize


def search(
    index: SearchIndex,
    query_str: str,
    *,
    filters: Mapping[str, str | Sequence[str]] | None = None,
    limit: int = 10,
) -> SearchResponse:
    del filters
    # Starter: simple bag-of-words AND matching without boolean/phrase/facet logic.
    tokens = [t.term for t in tokenize(query_str)]
    matched: list[SearchHit] = []
    n_docs = index.doc_count
    for doc_id in sorted(index.docs_text):
        if not tokens:
            score = 0.0
        else:
            if not all(doc_id in index.postings.get(term, {}) for term in tokens):
                continue
            score = sum(
                len(index.postings[term][doc_id]) * (math.log((1 + n_docs) / (1 + index.doc_freq(term))) + 1.0)
                for term in tokens
            )
        matched.append(SearchHit(doc_id=doc_id, score=score, facets=index.docs_facets.get(doc_id, {})))
    matched.sort(key=lambda h: (-h.score, h.doc_id))
    return SearchResponse(total=len(matched), hits=matched[:limit], facets={})
