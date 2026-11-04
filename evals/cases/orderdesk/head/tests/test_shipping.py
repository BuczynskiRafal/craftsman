import unittest

from orderdesk import pricing, shipping
from orderdesk.models import Address, Line, Order
from orderdesk.rates import default_table


def make_order(net_cents: int = 3000, country: str = "US", postcode: str = "73301", weight_g: int = 500) -> Order:
    return Order(
        "A1001",
        "Ada",
        Address("Ada", "1 Main St", "Austin", postcode, country),
        [Line("MUG-01", 1, net_cents, weight_g)],
        "2026-03-02T09:00:00+00:00",
    )


class QuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = default_table()

    def test_rate_plus_handling(self) -> None:
        self.assertEqual(shipping.quote_shipping(make_order(), self.table, "post"), 450 + 150)

    def test_heavy_orders_are_split_into_parcels(self) -> None:
        self.assertEqual(shipping.parcel_count(0), 1)
        self.assertEqual(shipping.parcel_count(20_000), 1)
        self.assertEqual(shipping.parcel_count(20_001), 2)
        order = make_order(weight_g=30_000)
        self.assertEqual(shipping.quote_shipping(order, self.table, "post"), 2 * (1900 + 150))

    def test_free_shipping_threshold(self) -> None:
        below = shipping.quote_shipping(make_order(7400), self.table, "post")
        at = shipping.quote_shipping(make_order(7500), self.table, "post")
        above = shipping.quote_shipping(make_order(9000), self.table, "post")
        self.assertGreater(below, 0)
        self.assertEqual(above, 0)
        self.assertLessEqual(at, below)

    def test_discount_counts_against_the_threshold(self) -> None:
        order = make_order(9000)
        order.discount_cents = 3000
        self.assertGreater(shipping.quote_shipping(order, self.table, "post"), 0)

    def test_staff_override_replaces_the_rate(self) -> None:
        order = make_order(3000)
        order.shipping_override_cents = 250
        self.assertEqual(shipping.quote_shipping(order, self.table, "express"), 250)

    def test_remote_zone_costs_more(self) -> None:
        near = shipping.quote_shipping(make_order(postcode="90210"), default_table(), "post")
        far = shipping.quote_shipping(make_order(postcode="96795"), default_table(), "post")
        self.assertEqual((near, far), (600, 900))


class ApplyAndTotalTests(unittest.TestCase):
    def test_apply_shipping_sets_cost_and_carrier(self) -> None:
        order = make_order()
        self.assertEqual(shipping.apply_shipping(order, default_table(), "express"), 1050)
        self.assertEqual((order.shipping_cents, order.carrier), (1050, "express"))

    def test_shipping_is_part_of_the_total_but_not_taxed(self) -> None:
        order = make_order(3000)
        before = pricing.total_cents(order)
        shipping.apply_shipping(order, default_table(), "post")
        self.assertEqual(pricing.tax_cents(order), 210)
        self.assertEqual(pricing.total_cents(order), before + 600)

    def test_shipped_orders_cannot_be_repriced(self) -> None:
        order = make_order()
        order.status = "shipped"
        with self.assertRaises(ValueError):
            shipping.apply_shipping(order, default_table(), "post")


class StatusFlowTests(unittest.TestCase):
    def test_happy_path_records_history(self) -> None:
        order = make_order()
        order.tracking = "940000000011"
        for status in ("packed", "shipped", "delivered"):
            shipping.advance(order, status)
        self.assertEqual([h["status"] for h in order.history], ["packed", "shipped", "delivered"])

    def test_cannot_skip_steps_or_ship_without_tracking(self) -> None:
        order = make_order()
        with self.assertRaises(ValueError):
            shipping.advance(order, "delivered")
        shipping.advance(order, "packed")
        with self.assertRaises(ValueError):
            shipping.advance(order, "shipped")

    def test_cancelled_orders_stay_cancelled(self) -> None:
        order = make_order()
        shipping.advance(order, "cancelled")
        with self.assertRaises(ValueError):
            shipping.advance(order, "packed")


if __name__ == "__main__":
    unittest.main()
