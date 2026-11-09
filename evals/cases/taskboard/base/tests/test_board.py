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
            board.add("write docs")
            board.complete(1)
            board.save(path)
            loaded = Board.load(path)
            self.assertEqual(json.loads(path.read_text())[0]["done"], True)
        self.assertEqual(loaded.tasks, board.tasks)

    def test_export_csv(self) -> None:
        board = Board()
        board.add("write docs")
        board.add("ship it")
        board.complete(2)
        out = io.StringIO()
        board.export_csv(out)
        self.assertEqual(out.getvalue(), "id,title,done\n1,write docs,no\n2,ship it,yes\n")


if __name__ == "__main__":
    unittest.main()
