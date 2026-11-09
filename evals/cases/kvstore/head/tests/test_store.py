import tempfile
import unittest
from pathlib import Path

from kvstore import Store


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "db.json"
        self.clock = FakeClock()
        self.store = Store(self.path, self.clock)

    def test_set_get_delete(self) -> None:
        self.store.set("a", 1)
        self.assertEqual(self.store.get("a"), 1)
        self.assertTrue(self.store.delete("a"))
        self.assertFalse(self.store.delete("a"))
        self.assertIsNone(self.store.get("a"))

    def test_ttl_expires_at_the_deadline(self) -> None:
        self.store.set("a", 1, ttl=10)
        self.clock.now += 9.9
        self.assertEqual(self.store.get("a"), 1)
        self.clock.now += 0.1
        self.assertIsNone(self.store.get("a"))
        self.assertEqual(self.store.keys(), [])

    def test_save_and_reload(self) -> None:
        self.store.set("a", [1, 2])
        self.store.save()
        again = Store(self.path, self.clock)
        self.assertEqual(again.get("a"), [1, 2])

    def test_save_replaces_the_file_and_leaves_no_temp_file(self) -> None:
        self.store.set("a", 1)
        self.store.save()
        self.store.set("b", 2)
        self.store.save()
        self.assertEqual(Store(self.path, self.clock).keys(), ["a", "b"])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["db.json"])

    def test_snapshot_ignores_later_writes_and_expired_keys(self) -> None:
        self.store.set("a", 1)
        self.store.set("gone", 2, ttl=1)
        self.clock.now += 5
        snap = self.store.snapshot()
        self.store.set("b", 3)
        self.store.delete("a")
        self.assertEqual(sorted(snap), ["a"])
        self.assertEqual(snap["a"]["value"], 1)


if __name__ == "__main__":
    unittest.main()
