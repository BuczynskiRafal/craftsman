# Task: Complete Positional Full-Text, Boolean & Faceted Search Index

Complete `docindex_search` so clean re-indexing/deletion, stopword-aware positional phrase matching, boolean query parsing with parentheses, multi-select facet counts, deterministic tie-breaking, and JSON snapshot persistence satisfy every requirement below while keeping `tests/test_starter.py` passing.

## Requirements

1. **Positional Tokenizer (`tokenizer.py`)**
   - `Token(term: str, position: int)`
   - Extract lowercase alphanumeric tokens (`[a-z0-9]+`).
   - Assign 0-based `position` offsets across ALL raw tokens **before** filtering `stopwords` (when `drop_stopwords=True`), so gaps occupied by removed stopwords stay reflected in `Token.position` (e.g. `"the quick brown fox"` -> `[("quick", 1), ("brown", 2), ("fox", 3)]`, where `"the"` at offset `0` leaves `"quick"` at `1`).

2. **Index Maintenance & Clean Cleanup (`index.py`)**
   - `SearchIndex.add_or_update(doc_id: str, text: str, facets: Mapping[str, str | Sequence[str]] | None = None) -> None`
   - Raise `SearchError` if `doc_id` is empty/whitespace.
   - Updating an existing `doc_id` must remove ALL previous term postings and facet memberships for that `doc_id` before indexing the new text and facets.
   - `SearchIndex.delete(doc_id: str) -> bool` returns `False` if missing, `True` if removed, and cleans up both postings and facets.
   - When deleting or updating removes the last posting for a term, prune that term from the inverted index (`index.doc_freq(term) == 0` and `term not in index.terms()`).
   - Facet values per field may be passed as a single `str` or a sequence of strings; normalize each document's facet values per field as a sorted deduplicated list.

3. **Boolean & Positional Phrase Query Engine (`query.py`)**
   - `search(index: SearchIndex, query_str: str, *, filters: Mapping[str, str | Sequence[str]] | None = None, limit: int = 10) -> SearchResponse`
   - An empty/whitespace `query_str` matches all documents (subject to `filters`).
   - Query syntax:
     - Case-sensitive uppercase operators `AND`, `OR`, `NOT` (unary negation) and grouping parentheses `(` `)`.
     - Precedence: `NOT` > `AND` > `OR`. Adjacent terms/phrases/groups without an explicit binary operator use implicit `AND` (`a b` == `a AND b`, `a NOT b` == `a AND (NOT b)`).
     - Double-quoted phrases `"term1 term2 ..."` match only if the kept phrase tokens occur in sequence with exact relative position deltas (`pos_k - pos_0 == rel_k - rel_0`), where phrase query tokens use the same stopword-gap position rule (`"a in b"` keeps relative gap `2`, `"a b"` requires adjacent `1`). If a phrase contains zero non-stopword tokens, it matches no documents.
     - Reject invalid syntax with `QuerySyntaxError` (subclass of `SearchError`): unclosed quotes, unbalanced parentheses, empty `()`, leading/trailing binary operators (`AND`, `OR`), consecutive binary operators, trailing `NOT`.
   - Ranking & determinism:
     - Score positive matching terms/phrases with positive TF-IDF (`idf = math.log((1 + N) / (1 + df)) + 1.0`, `score = sum(tf * idf)` across matched positive query clauses). Documents matched only via pure negation (`NOT foo`) or empty query score `0.0`.
     - Sort hits by `(-score, doc_id)` ascending so ties are deterministic, and truncate `hits` to `limit` while keeping `SearchResponse.total` equal to the full count of matching documents before `limit`.

4. **Multi-Select Facet Filtering & Facet Counts (`query.py`)**
   - `filters: {field: value_or_values}` applies `OR` within a single field's values and `AND` across different fields.
   - `SearchResponse.facets: dict[str, dict[str, int]]`:
     - Compute counts for each facet field `F` across all documents that match the parsed boolean query AND every filter **except** filters on `F` itself (multi-select facet exclusion).
     - Only include values whose count is `> 0` in `facets[F]`.

5. **Snapshot Persistence (`storage.py`)**
   - `save_snapshot(index: SearchIndex, path: Path) -> None` and `load_snapshot(path: Path) -> SearchIndex` round-trip documents, postings/positions, and facets to JSON so queries, `doc_freq`, `add_or_update`, and `delete` work identically after reload.
