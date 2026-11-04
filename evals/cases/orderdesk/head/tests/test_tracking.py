import unittest

from orderdesk import tracking
from orderdesk.models import Address, Line, Order


def make_order(status: str = "packed") -> Order:
    order = Order(
        "A1001",
        "Ada",
        Address("Ada", "1 Main St", "Austin", "73301"),
        [Line("MUG-01", 1, 1200, 350)],
        "2026-03-02T09:00:00+00:00",
        status,
    )
    order.tracking = tracking.new_tracking_number("post", 1001)
    return order


def event(order: Order, status: str, at: str = "2026-03-04T10:00:00+00:00") -> dict[str, str]:
    return {"tracking": order.tracking, "status": status, "at": at}


class NumberTests(unittest.TestCase):
    def test_numbers_have_a_prefix_and_a_check_digit(self) -> None:
        number = tracking.new_tracking_number("post", 1001)
        self.assertTrue(number.startswith("94"))
        self.assertEqual(len(number), 13)
        self.assertTrue(tracking.is_valid(number))
        self.assertFalse(tracking.is_valid(number[:-1] + str((int(number[-1]) + 1) % 10)))

    def test_unknown_carrier(self) -> None:
        with self.assertRaises(KeyError):
            tracking.new_tracking_number("pigeon", 1)


class EventTests(unittest.TestCase):
    def test_parse_event(self) -> None:
        parsed = tracking.parse_event({"tracking": "T1", "status": "delivered", "at": "2026-03-04T10:00:00+00:00"})
        self.assertEqual((parsed.tracking, parsed.status), ("T1", "delivered"))
        for bad in ({"status": "delivered", "at": "2026-03-04"}, {"tracking": "T", "status": "lost", "at": "x"}):
            with self.assertRaises((KeyError, ValueError)):
                tracking.parse_event(bad)

    def test_events_move_the_order_forward(self) -> None:
        order = make_order()
        applied, rejected = tracking.apply_events(
            order, [event(order, "picked_up"), event(order, "in_transit"), event(order, "delivered")]
        )
        self.assertEqual((applied, rejected), (3, []))
        self.assertEqual(order.status, "delivered")
        self.assertEqual([h["status"] for h in order.history], ["shipped", "delivered"])

    def test_events_for_other_parcels_are_rejected(self) -> None:
        order = make_order()
        stray = {"tracking": "OTHER", "status": "delivered", "at": "2026-03-04T10:00:00+00:00"}
        applied, rejected = tracking.apply_events(order, [stray])
        self.assertEqual((applied, rejected), (0, [stray]))
        self.assertEqual(order.status, "packed")

    def test_impossible_transitions_are_rejected(self) -> None:
        order = make_order("new")
        late = event(order, "delivered")
        self.assertEqual(tracking.apply_events(order, [late]), (0, [late]))


if __name__ == "__main__":
    unittest.main()
