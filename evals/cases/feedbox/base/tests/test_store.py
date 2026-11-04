import tempfile
import unittest
from pathlib import Path

from feedbox.model import Article
from feedbox.store import Library


def article(article_id: str, feed: str = "Notes", published: str = "2026-03-02T12:00:00+00:00") -> Article:
    return Article(article_id, feed, f"Title {article_id}", f"https://example.org/{article_id}", published, "text")


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "library.json"

    def test_round_trip(self) -> None:
        library = Library(self.path)
        library.add_feed("Notes", "notes.xml")
        library.add_articles([article("a"), article("b")])
        library.mark_read("b")
        library.save()
        loaded = Library.load(self.path)
        self.assertEqual(loaded.feeds, library.feeds)
        self.assertEqual(loaded.articles, library.articles)
        self.assertTrue(loaded.articles["b"].read)

    def test_missing_file_is_an_empty_library(self) -> None:
        library = Library.load(self.path)
        self.assertEqual((library.feeds, library.articles), ({}, {}))

    def test_add_articles_skips_known_ids(self) -> None:
        library = Library(self.path)
        self.assertEqual(library.add_articles([article("a"), article("b")]), 2)
        self.assertEqual(library.add_articles([article("b"), article("c")]), 1)
        self.assertEqual(sorted(library.articles), ["a", "b", "c"])

    def test_add_feed_keeps_the_first_url(self) -> None:
        library = Library(self.path)
        library.add_feed("Notes", "one.xml")
        self.assertEqual(library.add_feed("Notes", "two.xml").url, "one.xml")


if __name__ == "__main__":
    unittest.main()
