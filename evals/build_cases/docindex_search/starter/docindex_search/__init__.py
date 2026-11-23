"""Full-text and faceted search index starter package."""

from .index import QuerySyntaxError, SearchError, SearchHit, SearchIndex, SearchResponse
from .query import search
from .storage import load_snapshot, save_snapshot
from .tokenizer import DEFAULT_STOPWORDS, Token, tokenize

__all__ = [
    "DEFAULT_STOPWORDS",
    "QuerySyntaxError",
    "SearchError",
    "SearchHit",
    "SearchIndex",
    "SearchResponse",
    "Token",
    "load_snapshot",
    "save_snapshot",
    "search",
    "tokenize",
]
