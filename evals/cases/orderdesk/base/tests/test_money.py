import unittest

from orderdesk.money import format_cents, percent_of, to_cents


class MoneyTests(unittest.TestCase):
    def test_to_cents(self) -> None:
        self.assertEqual(to_cents("12"), 1200)
        self.assertEqual(to_cents("12.5"), 1250)
        self.assertEqual(to_cents("-3.05"), -305)

    def test_to_cents_rejects_junk(self) -> None:
        for text in ("", "1.234", "abc", "1.x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                to_cents(text)

    def test_format_cents(self) -> None:
        self.assertEqual(format_cents(1250), "12.50")
        self.assertEqual(format_cents(-5), "-0.05")

    def test_percent_of_rounds_half_up(self) -> None:
        self.assertEqual(percent_of(1000, 700), 70)
        self.assertEqual(percent_of(1050, 500), 53)


if __name__ == "__main__":
    unittest.main()
