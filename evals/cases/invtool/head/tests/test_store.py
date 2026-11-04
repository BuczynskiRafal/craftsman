import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from invtool import store


class StoreTests(unittest.TestCase):
    def test_roundtrip_sorted_by_sku(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inv.csv"
            items = [
                store.Item("b-2", "Washer", 40, Decimal("0.05")),
                store.Item("a-1", "Hex Bolt", 10, Decimal("0.25")),
            ]
            store.save(path, items)
            loaded = store.load(path)
        self.assertEqual([i.sku for i in loaded], ["a-1", "b-2"])
        self.assertEqual(loaded[0].price, Decimal("0.25"))

    def test_load_missing_file_is_empty(self) -> None:
        self.assertEqual(store.load(Path("/nonexistent/inv.csv")), [])

    def test_add_rejects_duplicate_sku(self) -> None:
        item = store.Item("a-1", "Hex Bolt", 10, Decimal("0.25"))
        with self.assertRaises(ValueError):
            store.add([item], item)

    def test_search_by_name_and_limits(self) -> None:
        items = [
            store.Item("a-1", "bolt", 10, Decimal("0.25")),
            store.Item("a-2", "bolt long", 2, Decimal("0.40")),
            store.Item("b-1", "washer", 40, Decimal("0.05")),
        ]
        self.assertEqual([i.sku for i in store.search(items, "BOLT")], ["a-1", "a-2"])
        self.assertEqual([i.sku for i in store.search(items, "bolt", min_qty=10)], ["a-1"])
        limit = Decimal("0.25")
        self.assertEqual([i.sku for i in store.search(items, "", max_price=limit)], ["a-1", "b-1"])


if __name__ == "__main__":
    unittest.main()
