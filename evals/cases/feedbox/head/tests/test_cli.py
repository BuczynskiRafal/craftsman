import io
import tempfile
import unittest
from pathlib import Path

from feedbox import cli
from tests.samples import SAMPLE_RSS


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.library = str(self.tmp / "library.json")
        self.rss = self.tmp / "notes.xml"
        self.rss.write_text(SAMPLE_RSS, encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        return cli.main(["--library", self.library, *argv], out), out.getvalue()

    def test_import_is_idempotent(self) -> None:
        self.assertEqual(self.run_cli("import", str(self.rss)), (0, "Field Notes: 3 new of 3\n"))
        self.assertEqual(self.run_cli("import", str(self.rss)), (0, "Field Notes: 0 new of 3\n"))

    def test_import_several_files_keeps_the_good_ones(self) -> None:
        other = self.tmp / "other.xml"
        other.write_text(SAMPLE_RSS.replace("Field Notes", "Other Notes").replace("fn-1", "on-1").replace("example.org", "other.example"), encoding="utf-8")
        code, text = self.run_cli("import", str(self.rss), str(self.tmp / "gone.xml"), str(other))
        self.assertEqual(code, 1)
        self.assertEqual(text, "Field Notes: 3 new of 3\nOther Notes: 3 new of 3\n")

    def test_list_show_and_read_state(self) -> None:
        self.run_cli("import", str(self.rss))
        lines = self.run_cli("list")[1].splitlines()
        self.assertEqual(lines[0], "* 2026-03-03  Field Notes: Soil samples")
        self.assertEqual(lines[-1], "* ----------  Field Notes: Undated note")
        code, text = self.run_cli("show", "fn-1")
        self.assertEqual(code, 0)
        self.assertIn("Birds are back early.", text)
        self.assertEqual(len(self.run_cli("list", "--unread")[1].splitlines()), 2)

    def test_tag_and_filter_by_tag(self) -> None:
        self.run_cli("import", str(self.rss))
        self.assertEqual(self.run_cli("tag", "Field Notes", "Birds"), (0, "Field Notes: birds\n"))
        self.assertEqual(self.run_cli("tag", "Field Notes", "birds")[1], "Field Notes: birds (unchanged)\n")
        self.assertEqual(len(self.run_cli("list", "--tag", "birds")[1].splitlines()), 3)
        self.assertEqual(self.run_cli("list", "--tag", "soil")[1], "")
        self.assertEqual(self.run_cli("tag", "Field Notes", "birds", "--remove")[1], "Field Notes: (no tags)\n")
        self.assertEqual(self.run_cli("tag", "Nope", "x")[0], 1)

    def test_export_writes_files_and_an_index(self) -> None:
        self.run_cli("import", str(self.rss))
        dest = self.tmp / "out"
        code, text = self.run_cli("export", str(dest), "--format", "md", "--since", "2026-03-02")
        self.assertEqual((code, text), (0, f"wrote 2 files to {dest}\n"))
        self.assertTrue((dest / "Field Notes" / "2026-03-02-spring-migration.md").is_file())
        self.assertIn("(Field Notes/2026-03-03-soil-samples.md)", (dest / "index.md").read_text(encoding="utf-8"))

    def test_digest(self) -> None:
        self.run_cli("import", str(self.rss))
        self.run_cli("show", "fn-1")
        text = self.run_cli("digest", "--per-feed", "1")[1]
        self.assertEqual(text, "Field Notes: 2 unread\n  - Soil samples\n  ... and 1 more\n")

    def test_since_needs_a_date(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli("list", "--since", "last week")

    def test_errors(self) -> None:
        self.assertEqual(self.run_cli("show", "nope")[0], 1)
        self.assertEqual(self.run_cli("import", str(self.tmp / "missing.xml"))[0], 1)

    def test_feeds(self) -> None:
        self.run_cli("import", str(self.rss))
        self.assertEqual(self.run_cli("feeds"), (0, "Field Notes  3 articles  []\n"))


if __name__ == "__main__":
    unittest.main()
