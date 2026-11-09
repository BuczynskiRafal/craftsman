import unittest

from orderdesk import pricing
from orderdesk.models import Address, Line, Order


def make_order(country: str = "US", discount: int = 0) -> Order:
    return Order(
        "A1001",
        "Ada",
        Address("Ada", "1 Main St", "Austin", "73301", country),
        [Line("MUG-01", 2, 1200, 350), Line("TEA-05", 1, 950, 290)],
        "2026-03-02T09:00:00+00:00",
        discount_cents=discount,
    )


class PricingTests(unittest.TestCase):
    def test_tax_by_country(self) -> None:
        self.assertEqual(pricing.tax_cents(make_order("US")), 235)
        self.assertEqual(pricing.tax_cents(make_order("CA")), 168)
        self.assertEqual(pricing.tax_cents(make_order("DE")), 0)

    def test_discount_reduces_tax_and_total(self) -> None:
        order = make_order("US", discount=350)
        self.assertEqual(pricing.net_cents(order), 3000)
        self.assertEqual(pricing.total_cents(order), 3210)

    def test_discount_never_makes_the_total_negative(self) -> None:
        self.assertEqual(pricing.total_cents(make_order("US", discount=99999)), 0)


if __name__ == "__main__":
    unittest.main()
