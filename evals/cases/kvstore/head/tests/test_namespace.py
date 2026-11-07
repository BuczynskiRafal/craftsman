import tempfile
import unittest
from pathlib import Path

from kvstore import Store


class NamespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "db.json")

    def test_namespace_prefixes_keys(self) -> None:
        users = self.store.namespace("users")
        users.set("ann", {"age": 3})
        self.store.set("other", 1)
        self.assertEqual(users.get("ann"), {"age": 3})
        self.assertEqual(users.keys(), ["ann"])
        self.assertEqual(self.store.keys(), ["other", "users:ann"])
        self.assertTrue(users.delete("ann"))
        self.assertEqual(users.keys(), [])

    def test_namespaces_do_not_see_each_other(self) -> None:
        self.store.namespace("a").set("k", 1)
        self.store.namespace("ab").set("k", 2)
        self.assertEqual(self.store.namespace("a").keys(), ["k"])
        self.assertEqual(self.store.namespace("a").get("k"), 1)
