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

    def test_list_show_and_read_state(self) -> None:
        self.run_cli("import", str(self.rss))
        lines = self.run_cli("list")[1].splitlines()
        self.assertEqual(lines[0], "* 2026-03-03  Field Notes: Soil samples")
        self.assertEqual(lines[-1], "* ----------  Field Notes: Undated note")
        code, text = self.run_cli("show", "fn-1")
        self.assertEqual(code, 0)
        self.assertIn("Birds are back early.", text)
        self.assertEqual(len(self.run_cli("list", "--unread")[1].splitlines()), 2)

    def test_errors(self) -> None:
        self.assertEqual(self.run_cli("show", "nope")[0], 1)
        self.assertEqual(self.run_cli("import", str(self.tmp / "missing.xml"))[0], 1)

    def test_feeds(self) -> None:
        self.run_cli("import", str(self.rss))
        self.assertEqual(self.run_cli("feeds"), (0, "Field Notes  3 articles\n"))


if __name__ == "__main__":
    unittest.main()
