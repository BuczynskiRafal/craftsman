"""Hidden acceptance tests for schedule_rrule."""

import unittest
from datetime import datetime, timedelta, timezone

from schedule_rrule import (
    CalendarSeries,
    EventOverride,
    RRule,
    ScheduleError,
    expand_events,
    find_conflicts,
    get_timezone,
    resolve_wall_time,
)


class HiddenScheduleRRuleTests(unittest.TestCase):
    def test_resolve_wall_time_spring_gap_policies(self) -> None:
        # US spring forward 2026: 2nd Sunday of March = 2026-03-08, 02:00..02:59 does not exist.
        gap = datetime(2026, 3, 8, 2, 30)
        shifted = resolve_wall_time(gap, "America/New_York", gap_policy="shift")
        assert shifted is not None
        self.assertEqual((shifted.hour, shifted.minute), (3, 30))
        self.assertEqual(shifted.utcoffset(), timedelta(hours=-4))

        self.assertIsNone(resolve_wall_time(gap, "America/New_York", gap_policy="skip"))
        with self.assertRaises(ScheduleError):
            resolve_wall_time(gap, "America/New_York", gap_policy="error")

    def test_resolve_wall_time_fall_back_ambiguous_policies(self) -> None:
        # EU fall back 2026: last Sunday of Oct = 2026-10-25, 02:00..02:59 occurs twice in Europe/Warsaw.
        amb = datetime(2026, 10, 25, 2, 30)
        first = resolve_wall_time(amb, "Europe/Warsaw", ambiguous_policy="first")
        second = resolve_wall_time(amb, "Europe/Warsaw", ambiguous_policy="second")
        assert first is not None and second is not None
        self.assertEqual(first.fold, 0)
        self.assertEqual(first.utcoffset(), timedelta(hours=2))
        self.assertEqual(second.fold, 1)
        self.assertEqual(second.utcoffset(), timedelta(hours=1))
        self.assertEqual(second.astimezone(timezone.utc) - first.astimezone(timezone.utc), timedelta(hours=1))

        with self.assertRaises(ScheduleError):
            resolve_wall_time(amb, "Europe/Warsaw", ambiguous_policy="error")

    def test_daily_series_preserves_local_wall_clock_across_dst(self) -> None:
        # Spring forward 2026-03-08 in America/New_York: 09:00 local moves from UTC-5 (14:00Z) to UTC-4 (13:00Z).
        ny = get_timezone("America/New_York")
        series = CalendarSeries(
            series_id="sync",
            title="Sync",
            dtstart=datetime(2026, 3, 7, 9, 0, tzinfo=ny),
            duration=timedelta(minutes=30),
            tz_name="America/New_York",
            rrule=RRule(freq="DAILY", count=3),
        )
        occs = expand_events(
            [series],
            datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 10, 23, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([(o.start.day, o.start.hour) for o in occs], [(7, 9), (8, 9), (9, 9)])
        self.assertEqual([o.start.astimezone(timezone.utc).hour for o in occs], [14, 13, 13])

    def test_weekly_byweekday_respects_interval_and_until_inclusive(self) -> None:
        utc = get_timezone("UTC")
        series = CalendarSeries(
            series_id="mwf",
            title="MWF",
            dtstart=datetime(2026, 1, 7, 10, 0, tzinfo=utc),  # Wednesday (weekday 2)
            duration=timedelta(hours=1),
            tz_name="UTC",
            rrule=RRule(freq="WEEKLY", interval=2, byweekday=(0, 4), until=datetime(2026, 1, 23, 10, 0, tzinfo=utc)),
        )
        occs = expand_events(
            [series],
            datetime(2026, 1, 1, 0, 0, tzinfo=utc),
            datetime(2026, 2, 1, 0, 0, tzinfo=utc),
        )
        # Week 1 (Jan 5 Mon, Jan 9 Fri >= Jan 7): Jan 9. Week 3 (interval=2): Jan 19 Mon, Jan 23 Fri (inclusive).
        self.assertEqual([(o.start.month, o.start.day) for o in occs], [(1, 9), (1, 19), (1, 23)])

    def test_monthly_bymonthday_skip_vs_clamp_and_negative_one(self) -> None:
        utc = get_timezone("UTC")
        s_skip = CalendarSeries(
            series_id="skip31",
            title="Skip31",
            dtstart=datetime(2026, 1, 31, 8, 0, tzinfo=utc),
            duration=timedelta(hours=1),
            tz_name="UTC",
            rrule=RRule(freq="MONTHLY", count=3, bymonthday=(31,), monthday_policy="skip"),
        )
        occs_skip = expand_events([s_skip], datetime(2026, 1, 1, tzinfo=utc), datetime(2026, 8, 1, tzinfo=utc))
        self.assertEqual([(o.start.month, o.start.day) for o in occs_skip], [(1, 31), (3, 31), (5, 31)])

        s_clamp = CalendarSeries(
            series_id="clamp31",
            title="Clamp31",
            dtstart=datetime(2024, 1, 31, 8, 0, tzinfo=utc),  # 2024 leap year
            duration=timedelta(hours=1),
            tz_name="UTC",
            rrule=RRule(freq="MONTHLY", count=3, bymonthday=(31,), monthday_policy="clamp"),
        )
        occs_clamp = expand_events([s_clamp], datetime(2024, 1, 1, tzinfo=utc), datetime(2024, 4, 1, tzinfo=utc))
        self.assertEqual([(o.start.month, o.start.day) for o in occs_clamp], [(1, 31), (2, 29), (3, 31)])

        s_last = CalendarSeries(
            series_id="last",
            title="EOM",
            dtstart=datetime(2025, 1, 31, 8, 0, tzinfo=utc),
            duration=timedelta(hours=1),
            tz_name="UTC",
            rrule=RRule(freq="MONTHLY", count=2, bymonthday=(-1,)),
        )
        occs_last = expand_events([s_last], datetime(2025, 1, 1, tzinfo=utc), datetime(2025, 4, 1, tzinfo=utc))
        self.assertEqual([(o.start.month, o.start.day) for o in occs_last], [(1, 31), (2, 28)])

    def test_count_vs_exdate_and_overrides_moving_into_and_out_of_window(self) -> None:
        utc = get_timezone("UTC")
        series = CalendarSeries(
            series_id="s1",
            title="Original",
            dtstart=datetime(2026, 5, 1, 10, 0, tzinfo=utc),
            duration=timedelta(hours=1),
            tz_name="UTC",
            rrule=RRule(freq="DAILY", count=4),  # generates May 1, 2, 3, 4
            exdates=(datetime(2026, 5, 2, 10, 0, tzinfo=utc),),
            overrides=(
                # May 1 moved INTO later window [May 3, May 5)
                EventOverride(
                    original_start=datetime(2026, 5, 1, 10, 0, tzinfo=utc),
                    new_start=datetime(2026, 5, 3, 15, 0, tzinfo=utc),
                    title="Moved In",
                ),
                # May 3 moved OUT of window [May 3, May 5)
                EventOverride(
                    original_start=datetime(2026, 5, 3, 10, 0, tzinfo=utc),
                    new_start=datetime(2026, 5, 10, 10, 0, tzinfo=utc),
                ),
                # May 4 cancelled via override
                EventOverride(
                    original_start=datetime(2026, 5, 4, 10, 0, tzinfo=utc),
                    cancelled=True,
                ),
            ),
        )
        occs = expand_events(
            [series],
            datetime(2026, 5, 3, 0, 0, tzinfo=utc),
            datetime(2026, 5, 5, 0, 0, tzinfo=utc),
        )
        self.assertEqual(len(occs), 1)
        self.assertEqual(occs[0].title, "Moved In")
        self.assertEqual(occs[0].start, datetime(2026, 5, 3, 15, 0, tzinfo=utc))
        self.assertEqual(occs[0].original_start, datetime(2026, 5, 1, 10, 0, tzinfo=utc))

    def test_gap_skip_does_not_consume_rrule_count(self) -> None:
        ny = get_timezone("America/New_York")
        series = CalendarSeries(
            series_id="gap",
            title="Nightly",
            dtstart=datetime(2026, 3, 7, 2, 30, tzinfo=ny),
            duration=timedelta(minutes=30),
            tz_name="America/New_York",
            rrule=RRule(freq="DAILY", count=2),
            gap_policy="skip",
        )
        # Mar 7 exists; Mar 8 at 02:30 is skipped cleanly; Mar 9 satisfies count=2.
        occs = expand_events(
            [series],
            datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 12, 0, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([(o.start.month, o.start.day) for o in occs], [(3, 7), (3, 9)])

    def test_cross_timezone_conflicts_ignore_back_to_back(self) -> None:
        warsaw = get_timezone("Europe/Warsaw")
        utc = get_timezone("UTC")
        ny = get_timezone("America/New_York")
        # 2026-01-15: Warsaw is UTC+1, NY is UTC-5.
        # s1: 14:00-15:00 Warsaw = 13:00-14:00 UTC
        # s2: 08:30-09:30 NY     = 13:30-14:30 UTC (overlaps s1 [13:30, 14:00))
        # s3: 14:00-15:00 UTC    = touches s1 end at 14:00Z (no conflict with s1), overlaps s2 [14:00, 14:30)
        s1 = CalendarSeries("a", "A", datetime(2026, 1, 15, 14, 0, tzinfo=warsaw), timedelta(hours=1), "Europe/Warsaw")
        s2 = CalendarSeries("b", "B", datetime(2026, 1, 15, 8, 30, tzinfo=ny), timedelta(hours=1), "America/New_York")
        s3 = CalendarSeries("c", "C", datetime(2026, 1, 15, 14, 0, tzinfo=utc), timedelta(hours=1), "UTC")
        occs = expand_events([s1, s2, s3], datetime(2026, 1, 15, 0, 0, tzinfo=utc), datetime(2026, 1, 16, 0, 0, tzinfo=utc))
        conflicts = find_conflicts(occs)
        self.assertEqual([(c.first.series_id, c.second.series_id) for c in conflicts], [("a", "b"), ("b", "c")])
        self.assertEqual(conflicts[0].overlap_start.astimezone(timezone.utc), datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(conflicts[0].overlap_end.astimezone(timezone.utc), datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
