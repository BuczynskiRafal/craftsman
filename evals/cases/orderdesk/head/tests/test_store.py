import tempfile
import unittest
from pathlib import Path

from orderdesk.models import Address, Line, Order
from orderdesk.store import OrderStore


def make_order(order_id: str = "A1001") -> Order:
    return Order(
        order_id,
        "Ada",
        Address("Ada", "1 Main St", "Austin", "73301"),
        [Line("MUG-01", 2, 1200, 350)],
        "2026-03-02T09:00:00+00:00",
        discount_cents=100,
    )


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = OrderStore(Path(self._tmp.name) / "orders")

    def test_round_trip(self) -> None:
        order = make_order()
        self.store.save(order)
        self.assertEqual(self.store.load("A1001"), order)

    def test_shipping_fields_round_trip(self) -> None:
        order = make_order()
        order.shipping_cents = 600
        order.shipping_override_cents = 0
        order.carrier = "post"
        order.tracking = "940000010019"
        order.history.append({"status": "packed", "note": ""})
        self.store.save(order)
        self.assertEqual(self.store.load("A1001"), order)

    def test_missing_order(self) -> None:
        with self.assertRaises(KeyError):
            self.store.load("A9999")

    def test_ids_and_next_id(self) -> None:
        self.assertEqual(self.store.next_id(), "A1001")
        self.store.save(make_order("A1001"))
        self.store.save(make_order("A1007"))
        self.assertEqual(self.store.ids(), ["A1001", "A1007"])
        self.assertEqual(self.store.next_id(), "A1008")

    def test_old_files_without_optional_fields_load(self) -> None:
        self.store.save(make_order())
        path = Path(self._tmp.name) / "orders" / "A1001.json"
        path.write_text(
            '{"id": "A1001", "customer": "Ada", "address": {"name": "Ada", "line1": "x", '
            '"city": "y", "postcode": "1"}}',
            encoding="utf-8",
        )
        order = self.store.load("A1001")
        self.assertEqual((order.status, order.discount_cents, order.lines), ("new", 0, []))
        self.assertEqual((order.shipping_cents, order.shipping_override_cents, order.history), (0, None, []))


if __name__ == "__main__":
    unittest.main()
