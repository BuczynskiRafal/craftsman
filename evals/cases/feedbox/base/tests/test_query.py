import unittest

from feedbox.model import Article
from feedbox.query import select


def article(article_id: str, feed: str, published: str, read: bool = False) -> Article:
    return Article(article_id, feed, "t", "l", published, read=read)


ARTICLES = [
    article("a", "One", "2026-03-01T10:00:00+00:00"),
    article("b", "Two", "2026-03-03T10:00:00+00:00", read=True),
    article("c", "One", "2026-03-03T10:00:00+00:00"),
    article("d", "One", ""),
]


class SelectTests(unittest.TestCase):
    def ids(self, **kwargs: object) -> list[str]:
        return [a.id for a in select(ARTICLES, **kwargs)]  # type: ignore[arg-type]

    def test_newest_first_ties_by_id_undated_last(self) -> None:
        self.assertEqual(self.ids(), ["b", "c", "a", "d"])

    def test_filters(self) -> None:
        self.assertEqual(self.ids(feed="One"), ["c", "a", "d"])
        self.assertEqual(self.ids(unread=True), ["c", "a", "d"])
        self.assertEqual(self.ids(limit=2), ["b", "c"])


if __name__ == "__main__":
    unittest.main()
