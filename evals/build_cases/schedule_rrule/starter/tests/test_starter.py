"""Starter sanity tests for schedule_rrule."""

import unittest
from datetime import datetime, timedelta

from schedule_rrule import CalendarSeries, RRule, expand_events, get_timezone


class StarterScheduleTests(unittest.TestCase):
    def test_daily_series_in_utc(self) -> None:
        utc = get_timezone("UTC")
        series = CalendarSeries(
            series_id="standup",
            title="Standup",
            dtstart=datetime(2026, 1, 1, 9, 0, tzinfo=utc),
            duration=timedelta(minutes=30),
            tz_name="UTC",
            rrule=RRule(freq="DAILY", count=3),
        )
        occs = expand_events([series], datetime(2026, 1, 1, 0, 0, tzinfo=utc), datetime(2026, 1, 5, 0, 0, tzinfo=utc))
        self.assertEqual(len(occs), 3)
        self.assertEqual([o.start.day for o in occs], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
