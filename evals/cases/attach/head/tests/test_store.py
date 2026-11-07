import io
import tempfile
import unittest
import zipfile
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

    def test_bad_ticket_and_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("../x", "a.txt", b"")
        with self.assertRaises(ValueError):
            self.store.save("OPS-42", "", b"")
        with self.assertRaises(ValueError):
            self.store.save("OPS-42", "logs/", b"")

    def test_unknown_ticket_lists_nothing(self) -> None:
        self.assertEqual(self.store.list("OPS-1"), [])

    def test_folders_are_created_and_listed_recursively(self) -> None:
        self.store.save("OPS-42", "logs/app.txt", b"log")
        self.store.save("OPS-42", "readme.txt", b"hi")
        self.assertEqual(self.store.list("OPS-42"), ["logs/app.txt", "readme.txt"])
        self.assertEqual(self.store.read("OPS-42", "logs/app.txt"), b"log")

    def test_leading_slash_stays_inside_the_ticket_directory(self) -> None:
        path = self.store.save("OPS-42", "/etc/motd", b"x")
        self.assertEqual(path, Path(self._tmp.name) / "OPS-42" / "etc" / "motd")

    def test_export_zip_is_reproducible_and_uses_relative_names(self) -> None:
        self.store.save("OPS-42", "logs/app.txt", b"log")
        self.store.save("OPS-42", "readme.txt", b"hi")
        first = self.store.export_zip("OPS-42")
        self.assertEqual(first, self.store.export_zip("OPS-42"))
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(archive.namelist(), ["logs/app.txt", "readme.txt"])
            self.assertEqual(archive.read("logs/app.txt"), b"log")


if __name__ == "__main__":
    unittest.main()
