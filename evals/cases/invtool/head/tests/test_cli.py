import io
import tempfile
import unittest
from pathlib import Path

from invtool import cli


class CliTests(unittest.TestCase):
    def test_add_then_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "inv.csv")
            self.assertEqual(cli.main(["--file", path, "add", "a-1", "Hex Bolt", "10", "0.25"]), 0)
            out = io.StringIO()
            self.assertEqual(cli.main(["--file", path, "list"], out), 0)
        self.assertIn("Hex Bolt", out.getvalue())

    def test_add_rejects_bad_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "inv.csv")
            self.assertEqual(cli.main(["--file", path, "add", "a-1", "Bolt", "1", "abc"]), 2)

    def test_search_prints_matches_and_exits_1_without_any(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "inv.csv")
            cli.main(["--file", path, "add", "a-1", "washer", "10", "0.05"])
            out = io.StringIO()
            self.assertEqual(cli.main(["--file", path, "search", "WASHER"], out), 0)
            self.assertIn("washer", out.getvalue())
            self.assertEqual(cli.main(["--file", path, "search", "nut"], io.StringIO()), 1)
            self.assertEqual(cli.main(["--file", path, "search", "washer", "--max-price", "x"]), 2)


if __name__ == "__main__":
    unittest.main()
