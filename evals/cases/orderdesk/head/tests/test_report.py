import unittest

from orderdesk import report
from orderdesk.models import Address, Line, Order


def make_order(order_id: str, day: str, status: str = "new", qty: int = 1) -> Order:
    return Order(
        order_id,
        "Ada",
        Address("Ada", "1 Main St", "Austin", "73301"),
        [Line("MUG-01", qty, 1000, 350)],
        f"{day}T09:00:00+00:00",
        status,
    )


class ReportTests(unittest.TestCase):
    def test_daily_summary_groups_by_day_and_skips_cancelled(self) -> None:
        orders = [
            make_order("A1", "2026-03-02"),
            make_order("A2", "2026-03-02", qty=2),
            make_order("A3", "2026-03-03"),
            make_order("A4", "2026-03-03", status="cancelled"),
        ]
        self.assertEqual(
            report.daily_summary(orders),
            {
                "2026-03-02": {"orders": 2, "revenue_cents": 3210},
                "2026-03-03": {"orders": 1, "revenue_cents": 1070},
            },
        )

    def test_csv(self) -> None:
        text = report.to_csv([make_order("A1", "2026-03-02")])
        self.assertEqual(
            text,
            "id,customer,created_at,status,carrier,tracking,subtotal,discount,shipping,tax,total\n"
            "A1,Ada,2026-03-02T09:00:00+00:00,new,,,10.00,0.00,0.00,0.70,10.70\n",
        )

    def test_csv_has_the_shipping_columns(self) -> None:
        order = make_order("A2", "2026-03-02")
        order.carrier, order.tracking, order.shipping_cents = "post", "940000010019", 600
        row = report.to_csv([order]).splitlines()[1].split(",")
        self.assertEqual(row[4:6], ["post", "940000010019"])
        self.assertEqual(row[8], "6.00")
        self.assertEqual(row[-1], "16.70")


if __name__ == "__main__":
    unittest.main()
