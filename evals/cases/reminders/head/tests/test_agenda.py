import unittest
from datetime import datetime, timedelta, timezone

from reminders.agenda import Agenda, Reminder

NOW = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


class AgendaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agenda = Agenda()
        self.agenda.add(Reminder("past", "call back", NOW - timedelta(hours=1)))
        self.agenda.add(Reminder("soon", "standup", NOW + timedelta(hours=1)))
        self.agenda.add(Reminder("edge", "report", NOW + timedelta(hours=24)))
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

    def test_add_local_uses_the_agenda_timezone_for_naive_text(self) -> None:
        agenda = Agenda(tz=timezone(timedelta(hours=2)))
        reminder = agenda.add_local("r1", "dentist", "2026-03-01T09:30:00")
        self.assertEqual(reminder.due, datetime(2026, 3, 1, 7, 30, tzinfo=timezone.utc))
        explicit = agenda.add_local("r2", "flight", "2026-03-01T09:30:00-05:00")
        self.assertEqual(explicit.due, datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc))

    def test_digest_groups_by_utc_day_in_due_order(self) -> None:
        digest = self.agenda.digest(2, now=NOW)
        self.assertEqual(sorted(digest), ["2026-03-01", "2026-03-02"])
        self.assertEqual([r.id for r in digest["2026-03-01"]], ["soon"])

    def test_digest_end_is_exclusive(self) -> None:
        digest = self.agenda.digest(1, now=NOW)
        self.assertTrue(all(r.id != "edge" for r in digest.get("2026-03-03", [])))

    def test_digest_includes_the_start_instant(self) -> None:
        self.agenda.add(Reminder("now", "right now", NOW))
        digest = self.agenda.digest(1, now=NOW)
        self.assertIn("now", [r.id for r in digest["2026-03-01"]])


if __name__ == "__main__":
    unittest.main()
