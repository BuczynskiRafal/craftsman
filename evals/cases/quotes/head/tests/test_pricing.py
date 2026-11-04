import unittest
from decimal import Decimal

from quotes.pricing import Line, Quoter, UnknownSku

PRICES = {"bolt": Decimal("0.35"), "nut": Decimal("0.20")}
RATES = {"EUR": Decimal("0.90"), "PLN": Decimal("4.00")}


class QuoterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rate_calls: list[str] = []
        self.quoter = Quoter(PRICES, self._rates)

    def _rates(self, currency: str) -> Decimal:
        self.rate_calls.append(currency)
        return RATES[currency]

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

    def test_quote_in_another_currency(self) -> None:
        self.assertEqual(self.quoter.quote("bolt", 3, "EUR"), Decimal("0.94"))
        pln = Quoter(PRICES, self._rates)
        self.assertEqual(pln.total([Line("bolt", 3), Line("nut", 5)], "PLN"), Decimal("8.20"))

    def test_base_currency_does_not_fetch_a_rate(self) -> None:
        self.quoter.quote("bolt", 3, "USD")
        self.assertEqual(self.rate_calls, [])

    def test_repeated_quote_is_cached(self) -> None:
        self.quoter.quote("bolt", 3, "EUR")
        self.quoter.quote("bolt", 3, "EUR")
        self.assertEqual(self.rate_calls, ["EUR"])

    def test_summarize_reports_unknown_skus(self) -> None:
        summary = self.quoter.summarize([Line("bolt", 3), Line("screw", 1)], "EUR")
        self.assertEqual(summary.total, Decimal("0.94"))
        self.assertEqual(summary.currency, "EUR")
        self.assertEqual(summary.unknown, ["screw"])


if __name__ == "__main__":
    unittest.main()
