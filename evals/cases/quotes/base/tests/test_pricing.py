import unittest
from decimal import Decimal

from quotes.pricing import Line, Quoter, UnknownSku

PRICES = {"bolt": Decimal("0.35"), "nut": Decimal("0.20")}


class QuoterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.quoter = Quoter(PRICES)

    def test_quote_rounds_to_cents(self) -> None:
        self.assertEqual(self.quoter.quote("bolt", 3), Decimal("1.05"))

    def test_bulk_discount_from_ten_units(self) -> None:
        self.assertEqual(self.quoter.quote("bolt", 9), Decimal("3.15"))
        self.assertEqual(self.quoter.quote("bolt", 10), Decimal("3.32"))

    def test_unknown_sku_and_bad_quantity(self) -> None:
        with self.assertRaises(UnknownSku):
            self.quoter.quote("screw", 1)
        with self.assertRaises(ValueError):
            self.quoter.quote("bolt", 0)

    def test_total_adds_lines(self) -> None:
        lines = [Line("bolt", 3), Line("nut", 5)]
        self.assertEqual(self.quoter.total(lines), Decimal("2.05"))


if __name__ == "__main__":
    unittest.main()
