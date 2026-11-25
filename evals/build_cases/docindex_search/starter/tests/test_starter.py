"""Starter sanity tests for docindex_search."""

import unittest

from docindex_search import SearchIndex, search


class StarterDocIndexTests(unittest.TestCase):
    def test_basic_term_lookup(self) -> None:
        idx = SearchIndex()
        idx.add_or_update("d1", "Python concurrency with asyncio queues")
        idx.add_or_update("d2", "Rust systems programming")

        res = search(idx, "python asyncio")
        self.assertEqual(res.total, 1)
        self.assertEqual([h.doc_id for h in res.hits], ["d1"])


if __name__ == "__main__":
    unittest.main()
