import tempfile
import unittest
from pathlib import Path

from feedbox import exporter, tags
from feedbox.model import Article, Feed
from feedbox.slug import slugify


def article(title: str = "Spring migration", feed: str = "Notes", published: str = "2026-03-02T12:00:00+00:00") -> Article:
    return Article("id-" + title, feed, title, "https://example.org/a?x=1&y=2", published, "Birds <back>", author="Mira")


class SlugTests(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("Spring migration!"), "spring-migration")
        self.assertEqual(slugify("  --Ünïcode & more--  "), "n-code-more")
        self.assertEqual(slugify("???"), "untitled")
        self.assertEqual(slugify("a" * 100), "a" * 60)
        self.assertEqual(slugify("word " * 20, limit=12), "word-word-wo")


class TagTests(unittest.TestCase):
    def test_parse_tags(self) -> None:
        self.assertEqual(tags.parse_tags(" Birds, soil,,BIRDS ,"), ["birds", "soil"])

    def test_add_and_remove(self) -> None:
        feed = Feed("Notes", "n.xml")
        self.assertTrue(tags.add_tag(feed, " Field "))
        self.assertFalse(tags.add_tag(feed, "field"))
        self.assertFalse(tags.add_tag(feed, "  "))
        self.assertEqual(feed.tags, ["field"])
        self.assertTrue(tags.remove_tag(feed, "FIELD"))
        self.assertFalse(tags.remove_tag(feed, "field"))


class RenderTests(unittest.TestCase):
    def test_markdown(self) -> None:
        text = exporter.render_markdown(article())
        self.assertTrue(text.startswith("# Spring migration\n"))
        self.assertIn("*Mira | 2026-03-02 | Notes*", text)
        self.assertIn("[Original](https://example.org/a?x=1&y=2)", text)

    def test_html_escapes(self) -> None:
        text = exporter.render_html(article("A <b> title"))
        self.assertIn("<h1>A &lt;b&gt; title</h1>", text)
        self.assertIn("Birds &lt;back&gt;", text)
        self.assertIn("x=1&amp;y=2", text)


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dest = Path(self._tmp.name) / "out"

    def test_files_are_named_by_date_and_title(self) -> None:
        paths = exporter.export([article(), article("Undated", published="")], self.dest)
        names = [p.relative_to(self.dest).as_posix() for p in paths]
        self.assertEqual(names, ["Notes/2026-03-02-spring-migration.md", "Notes/undated-undated.md"])
        self.assertTrue(all(p.is_file() for p in paths))

    def test_existing_files_are_not_overwritten(self) -> None:
        first = exporter.export([article()], self.dest)[0]
        first.write_text("mine", encoding="utf-8")
        second = exporter.export([article()], self.dest)[0]
        self.assertEqual(second.name, "2026-03-02-spring-migration-2.md")
        self.assertEqual(first.read_text(encoding="utf-8"), "mine")

    def test_index_links_every_file(self) -> None:
        exporter.export([article(), article("Soil", published="2026-03-03T07:30:00+00:00")], self.dest)
        index = (self.dest / "index.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(index[0], "# Exported articles")
        self.assertIn("- [Soil](Notes/2026-03-03-soil.md)", index)

    def test_html_format_and_bad_format(self) -> None:
        path = exporter.export([article()], self.dest, "html")[0]
        self.assertEqual(path.suffix, ".html")
        with self.assertRaises(ValueError):
            exporter.export([article()], self.dest, "pdf")


if __name__ == "__main__":
    unittest.main()
