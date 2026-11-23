"""Hidden acceptance tests for docindex_search."""

import tempfile
import unittest
from pathlib import Path

from docindex_search import (
    QuerySyntaxError,
    SearchError,
    SearchIndex,
    load_snapshot,
    save_snapshot,
    search,
    tokenize,
)


class HiddenDocIndexTests(unittest.TestCase):
    def test_tokenizer_preserves_stopword_position_gaps(self) -> None:
        toks = tokenize("the quick brown fox")
        self.assertEqual([(t.term, t.position) for t in toks], [("quick", 1), ("brown", 2), ("fox", 3)])

    def test_update_and_delete_remove_stale_postings_and_prune_terms(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("d1", "alpha beta gamma", facets={"tag": ["v1"]})
        idx.add_or_update("d2", "beta delta")

        idx.add_or_update("d1", "delta epsilon", facets={"tag": ["v2"]})
        self.assertEqual(idx.doc_freq("alpha"), 0)
        self.assertNotIn("alpha", idx.terms())
        self.assertEqual(search(idx, "alpha").total, 0)
        self.assertEqual([h.doc_id for h in search(idx, "beta").hits], ["d2"])
        self.assertEqual([h.doc_id for h in search(idx, "delta", filters={"tag": "v2"}).hits], ["d1"])

        self.assertTrue(idx.delete("d2"))
        self.assertFalse(idx.delete("d2"))
        self.assertEqual(idx.doc_freq("beta"), 0)
        self.assertNotIn("beta", idx.terms())
        self.assertEqual(search(idx, "beta").total, 0)

    def test_phrase_matching_respects_stopword_gaps(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("adj", "quick brown fox jumps")
        idx.add_or_update("gap", "quick on the fox jumps")
        idx.add_or_update("rev", "fox brown quick")

        res_brown_fox = search(idx, '"brown fox"')
        self.assertEqual([h.doc_id for h in res_brown_fox.hits], ["adj"])

        res_quick_fox = search(idx, '"quick fox"')
        self.assertEqual(res_quick_fox.total, 0)

        res_gap = search(idx, '"quick in the fox"')
        self.assertEqual([h.doc_id for h in res_gap.hits], ["gap"])

        # Stopword-only phrase matches nothing.
        self.assertEqual(search(idx, '"the and of"').total, 0)

    def test_boolean_operators_precedence_and_parentheses(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("d1", "python grpc service")
        idx.add_or_update("d2", "python rest service")
        idx.add_or_update("d3", "go grpc service")
        idx.add_or_update("d4", "python rest deprecated")

        # Parentheses + OR + NOT
        res = search(idx, '(python OR go) AND grpc')
        self.assertEqual([h.doc_id for h in res.hits], ["d1", "d3"])

        # Precedence: NOT > AND > OR -> python AND (NOT deprecated) OR go AND grpc
        res2 = search(idx, 'python NOT deprecated OR go grpc')
        self.assertEqual(sorted(h.doc_id for h in res2.hits), ["d1", "d2", "d3"])

        # Negation-only query
        res3 = search(idx, 'NOT deprecated')
        self.assertEqual([h.doc_id for h in res3.hits], ["d1", "d2", "d3"])

    def test_query_syntax_errors_raise(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("d1", "alpha beta")
        for bad in ('(alpha', 'alpha)', '"unclosed', '()', 'AND alpha', 'alpha OR', 'alpha NOT', 'alpha AND OR beta'):
            with self.subTest(bad=bad):
                with self.assertRaises(QuerySyntaxError):
                    search(idx, bad)

    def test_multi_select_facet_filters_and_facet_counts(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("d1", "cloud queue", facets={"lang": "python", "tier": "pro"})
        idx.add_or_update("d2", "cloud queue", facets={"lang": "go", "tier": "pro"})
        idx.add_or_update("d3", "cloud queue", facets={"lang": "python", "tier": "free"})
        idx.add_or_update("d4", "cloud storage", facets={"lang": "rust", "tier": "pro"})

        # Filter lang=python AND tier=pro -> hits=[d1]
        # Facet counts for 'lang' exclude lang filter (only tier=pro + query "cloud queue" -> python:1, go:1).
        # Facet counts for 'tier' exclude tier filter (only lang=python + query "cloud queue" -> pro:1, free:1).
        res = search(idx, "cloud queue", filters={"lang": ["python"], "tier": ["pro"]})
        self.assertEqual([h.doc_id for h in res.hits], ["d1"])
        self.assertEqual(res.facets.get("lang"), {"go": 1, "python": 1})
        self.assertEqual(res.facets.get("tier"), {"free": 1, "pro": 1})

    def test_deterministic_scoring_tie_break_and_limit_vs_total(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("doc-b", "vector search engine")
        idx.add_or_update("doc-a", "vector search engine")
        idx.add_or_update("doc-c", "vector vector vector search")

        res = search(idx, "vector", limit=2)
        self.assertEqual(res.total, 3)
        self.assertEqual(len(res.hits), 2)
        self.assertEqual(res.hits[0].doc_id, "doc-c")
        self.assertEqual(res.hits[1].doc_id, "doc-a")  # ties with doc-b, sorted ascending by doc_id

    def test_snapshot_round_trip_preserves_positions_and_facets(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("d1", "the distributed cache", facets={"lang": ["python", "rust"]})
        idx.add_or_update("d2", "cache invalidation", facets={"lang": "go"})

        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "index.json"
            save_snapshot(idx, snap)
            loaded = load_snapshot(snap)

        self.assertEqual(search(loaded, '"distributed cache"').hits[0].doc_id, "d1")
        self.assertEqual(search(loaded, "", filters={"lang": "rust"}).total, 1)
        loaded.delete("d1")
        self.assertEqual(loaded.doc_freq("distributed"), 0)

    def test_empty_doc_id_rejected(self) -> None:
        idx = SearchIndex()
        with self.assertRaises(SearchError):
            idx.add_or_update("   ", "hello")


if __name__ == "__main__":
    unittest.main()
