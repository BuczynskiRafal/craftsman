import io
import tempfile
import unittest
from pathlib import Path

from taskboard import cli


class CliTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli.main(["--file", str(self.path), *argv], out)
        return code, out.getvalue()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "tasks.json"

    def test_add_done_and_list(self) -> None:
        self.run_cli("add", "write docs")
        self.run_cli("add", "ship it")
        self.assertEqual(self.run_cli("done", "1")[0], 0)
        code, text = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("[x]", text.splitlines()[0])
        self.assertIn("ship it", text.splitlines()[1])

    def test_done_unknown_task_fails(self) -> None:
        self.assertEqual(self.run_cli("done", "9")[0], 1)

    def test_add_with_priority_and_sorted_list(self) -> None:
        self.run_cli("add", "later", "--priority", "3")
        self.run_cli("add", "now", "--priority", "1")
        _, text = self.run_cli("list", "--sort", "priority")
        first, second = text.splitlines()
        self.assertIn("P1 now", first)
        self.assertIn("P3 later", second)

    def test_priority_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli("add", "x", "--priority", "9")


if __name__ == "__main__":
    unittest.main()
