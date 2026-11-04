import io
import tempfile
import unittest
from pathlib import Path

from orderdesk import cli


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = str(Path(self._tmp.name) / "orders")

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(["--dir", self.dir, *argv], out)
        return code, out.getvalue()

    def new_order(self) -> str:
        code, text = self.run_cli(
            "new", "Ada", "--ship-to", "Ada", "1 Main St", "Austin", "73301",
            "--item", "MUG-01:2", "--item", "TEA-05",
        )
        self.assertEqual(code, 0)
        return text.strip()

    def test_new_and_show(self) -> None:
        order_id = self.new_order()
        self.assertEqual(order_id, "A1001")
        code, text = self.run_cli("show", order_id)
        self.assertEqual(code, 0)
        self.assertIn("subtotal 33.50", text)
        self.assertIn("total    35.85", text)

    def test_show_unknown_order(self) -> None:
        self.assertEqual(self.run_cli("show", "A9")[0], 1)

    def test_list_and_report(self) -> None:
        self.new_order()
        self.assertIn("A1001  Ada  35.85", self.run_cli("list")[1])
        self.assertTrue(self.run_cli("report", "--csv")[1].startswith("id,customer"))
        self.assertIn("1 orders  35.85", self.run_cli("report")[1])


if __name__ == "__main__":
    unittest.main()
