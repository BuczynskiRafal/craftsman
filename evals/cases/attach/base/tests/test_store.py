import tempfile
import unittest
from pathlib import Path

from attach.store import AttachmentStore


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = AttachmentStore(Path(self._tmp.name))

    def test_save_read_list_delete(self) -> None:
        self.store.save("OPS-42", "b.txt", b"two")
        self.store.save("OPS-42", "a.txt", b"one")
        self.assertEqual(self.store.read("OPS-42", "a.txt"), b"one")
        self.assertEqual(self.store.list("OPS-42"), ["a.txt", "b.txt"])
        self.store.delete("OPS-42", "a.txt")
        self.assertEqual(self.store.list("OPS-42"), ["b.txt"])

    def test_directories_are_dropped_from_names(self) -> None:
        path = self.store.save("OPS-42", "../../evil.txt", b"x")
        self.assertEqual(path.parent.name, "OPS-42")

    def test_bad_ticket_and_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("../x", "a.txt", b"")
        with self.assertRaises(ValueError):
            self.store.save("OPS-42", "", b"")

    def test_unknown_ticket_lists_nothing(self) -> None:
        self.assertEqual(self.store.list("OPS-1"), [])


if __name__ == "__main__":
    unittest.main()
