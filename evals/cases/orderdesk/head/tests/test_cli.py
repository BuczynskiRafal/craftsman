import io
import json
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

    def test_quote_and_ship(self) -> None:
        order_id = self.new_order()
        code, text = self.run_cli("quote", order_id)
        self.assertEqual((code, text.splitlines()), (0, ["express  14.50", "post  8.50"]))
        code, text = self.run_cli("ship", order_id, "--carrier", "post")
        self.assertEqual(code, 0)
        self.assertIn("shipping 8.50", text)
        shown = self.run_cli("show", order_id)[1]
        self.assertIn("shipping 8.50 (post)", shown)
        self.assertIn("total    44.35", shown)
        self.assertRegex(shown, r"tracking 94\d{11}")

    def test_ship_with_override_and_unknown_carrier(self) -> None:
        order_id = self.new_order()
        code, text = self.run_cli("ship", order_id, "--carrier", "express", "--override", "2.50")
        self.assertEqual(code, 0)
        self.assertIn("shipping 2.50", text)
        self.assertEqual(self.run_cli("ship", order_id, "--carrier", "pigeon")[0], 1)

    def test_advance_requires_tracking_and_follows_the_flow(self) -> None:
        order_id = self.new_order()
        self.assertEqual(self.run_cli("advance", order_id, "packed")[0], 0)
        self.assertEqual(self.run_cli("advance", order_id, "shipped")[0], 1)
        self.run_cli("ship", order_id, "--carrier", "post")
        self.assertEqual(self.run_cli("advance", order_id, "shipped")[0], 0)

    def test_track_reports_how_many_events_were_applied(self) -> None:
        order_id = self.new_order()
        self.run_cli("ship", order_id, "--carrier", "post")
        tracking = next(line.split()[-1] for line in self.run_cli("show", order_id)[1].splitlines() if "tracking" in line)
        self.run_cli("advance", order_id, "packed")
        path = Path(self._tmp.name) / "events.json"
        path.write_text(
            json.dumps([{"tracking": tracking, "status": "picked_up", "at": "2026-03-04T10:00:00+00:00"}]),
            encoding="utf-8",
        )
        self.assertEqual(self.run_cli("track", order_id, str(path)), (0, "applied 1, rejected 0\n"))

    def test_show_unknown_order(self) -> None:
        self.assertEqual(self.run_cli("show", "A9")[0], 1)

    def test_list_and_report(self) -> None:
        self.new_order()
        self.assertIn("A1001  Ada  35.85", self.run_cli("list")[1])
        self.assertTrue(self.run_cli("report", "--csv")[1].startswith("id,customer"))
        self.assertIn("1 orders  35.85", self.run_cli("report")[1])


if __name__ == "__main__":
    unittest.main()
