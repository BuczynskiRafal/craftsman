import unittest

from feedbox.parser import FeedError, parse_date, parse_rss, strip_tags
from tests.samples import SAMPLE_RSS


class ParserTests(unittest.TestCase):
    def test_feed_and_articles(self) -> None:
        feed, articles = parse_rss(SAMPLE_RSS, "notes.xml")
        self.assertEqual((feed.name, feed.url), ("Field Notes", "notes.xml"))
        self.assertEqual([a.title for a in articles], ["Spring migration", "Soil samples", "Undated note"])
        self.assertEqual([a.id for a in articles], ["fn-1", "https://example.org/soil", "https://example.org/undated"])

    def test_dates_are_utc_iso(self) -> None:
        _, articles = parse_rss(SAMPLE_RSS, "notes.xml")
        self.assertEqual(articles[0].published, "2026-03-02T12:00:00+00:00")
        self.assertEqual(articles[1].published, "2026-03-03T07:30:00+00:00")
        self.assertEqual(articles[2].published, "")

    def test_bad_dates_are_empty(self) -> None:
        self.assertEqual(parse_date("yesterday-ish"), "")
        self.assertEqual(parse_date(""), "")

    def test_markup_is_stripped_from_the_body(self) -> None:
        self.assertEqual(strip_tags("<p>Birds are <b>back</b>\n early.</p>"), "Birds are back early.")
        _, articles = parse_rss(SAMPLE_RSS, "notes.xml")
        self.assertEqual(articles[0].body, "Birds are back early.")

    def test_invalid_documents(self) -> None:
        with self.assertRaises(FeedError):
            parse_rss("<rss><channel>", "x")
        with self.assertRaises(FeedError):
            parse_rss("<html/>", "x")


if __name__ == "__main__":
    unittest.main()
