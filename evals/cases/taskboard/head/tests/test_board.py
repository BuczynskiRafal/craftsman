import io
import json
import tempfile
import unittest
from pathlib import Path

from taskboard.board import Board


class BoardTests(unittest.TestCase):
    def test_add_assigns_increasing_ids_and_rejects_blank_titles(self) -> None:
        board = Board()
        self.assertEqual(board.add("write docs").id, 1)
        self.assertEqual(board.add("  ship it ").title, "ship it")
        with self.assertRaises(ValueError):
            board.add("   ")

    def test_complete_unknown_task(self) -> None:
        with self.assertRaises(KeyError):
            Board().complete(7)

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            board = Board()
            board.add("write docs", priority=3)
            board.complete(1)
            board.save(path)
            loaded = Board.load(path)
            self.assertEqual(json.loads(path.read_text())[0]["done"], True)
        self.assertEqual(loaded.tasks, board.tasks)

    def test_files_from_older_versions_get_the_default_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            path.write_text('[{"id": 1, "title": "old", "done": false}]', encoding="utf-8")
            self.assertEqual(Board.load(path).tasks[0].priority, 2)

    def test_priority_must_be_between_0_and_3(self) -> None:
        with self.assertRaises(ValueError):
            Board().add("x", priority=4)
        with self.assertRaises(ValueError):
            Board().add("x", priority=-1)

    def test_by_priority_puts_the_most_urgent_first(self) -> None:
        board = Board()
        board.add("low", priority=3)
        board.add("urgent", priority=1)
        board.add("normal")
        self.assertEqual([t.title for t in board.by_priority()], ["urgent", "normal", "low"])

    def test_export_csv(self) -> None:
        board = Board()
        board.add("write docs")
        board.add("ship it")
        board.complete(2)
        out = io.StringIO()
        board.export_csv(out)
        self.assertIn("write docs,no", out.getvalue())
        self.assertIn("ship it,yes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
