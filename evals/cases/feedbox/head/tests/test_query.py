import unittest

from feedbox.digest import build_digest
from feedbox.model import Article, Feed
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

    def test_since_is_inclusive_and_drops_undated(self) -> None:
        self.assertEqual(self.ids(since="2026-03-03"), ["b", "c"])
        self.assertEqual(self.ids(since="2026-03-01"), ["b", "c", "a"])

    def test_tags_pick_feeds(self) -> None:
        feeds = {"One": Feed("One", "1.xml", ["birds"]), "Two": Feed("Two", "2.xml", ["soil"])}
        self.assertEqual(self.ids(tags=["birds"], feeds=feeds), ["c", "a", "d"])
        self.assertEqual(self.ids(tags=["SOIL"], feeds=feeds), ["b"])
        self.assertEqual(self.ids(tags=["wind"], feeds=feeds), [])


class DigestTests(unittest.TestCase):
    def test_counts_unread_and_lists_the_newest(self) -> None:
        def titled(article_id: str, feed: str, published: str, read: bool = False) -> Article:
            return Article(article_id, feed, f"Title {article_id}", "l", published, read=read)

        articles = [titled(c, "Birds", f"2026-03-0{n}T10:00:00+00:00") for n, c in enumerate("abcd", 1)]
        articles += [titled("e", "Soil", "2026-03-04T10:00:00+00:00")]
        articles += [titled("f", "Soil", "2026-03-05T10:00:00+00:00", read=True)]
        self.assertEqual(
            build_digest(articles),
            "Birds: 4 unread\n  - Title d\n  - Title c\n  - Title b\n  ... and 1 more\nSoil: 1 unread\n  - Title e\n",
        )
        self.assertEqual(
            build_digest(articles, since="2026-03-03", per_feed=1),
            "Birds: 2 unread\n  - Title d\n  ... and 1 more\nSoil: 1 unread\n  - Title e\n",
        )

    def test_nothing_new(self) -> None:
        self.assertEqual(build_digest([]), "Nothing new.\n")
        self.assertEqual(build_digest(ARTICLES, since="2027-01-01"), "Nothing new.\n")


if __name__ == "__main__":
    unittest.main()
