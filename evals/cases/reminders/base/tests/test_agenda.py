import unittest
from datetime import datetime, timedelta, timezone

from reminders.agenda import Agenda, Reminder

NOW = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


class AgendaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agenda = Agenda()
        self.agenda.add(Reminder("past", "call back", NOW - timedelta(hours=1)))
        self.agenda.add(Reminder("soon", "standup", NOW + timedelta(hours=1)))
        self.agenda.add(Reminder("later", "invoice", NOW + timedelta(hours=25)))

    def test_add_requires_aware_datetime_and_unique_id(self) -> None:
        with self.assertRaises(ValueError):
            self.agenda.add(Reminder("x", "naive", datetime(2026, 3, 1, 9, 0)))
        with self.assertRaises(ValueError):
            self.agenda.add(Reminder("soon", "dup", NOW))

    def test_due_between_is_half_open(self) -> None:
        found = self.agenda.due_between(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
        self.assertEqual([r.id for r in found], ["past"])

    def test_overdue_and_remove(self) -> None:
        self.assertEqual([r.id for r in self.agenda.overdue(NOW)], ["past"])
        self.assertTrue(self.agenda.remove("past"))
        self.assertEqual(self.agenda.overdue(NOW), [])


if __name__ == "__main__":
    unittest.main()
