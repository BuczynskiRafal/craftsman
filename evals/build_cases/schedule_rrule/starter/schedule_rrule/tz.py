"""Deterministic stdlib tzinfo DST rules (no OS tzdata required)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Literal


class ScheduleError(Exception):
    """Base error for timezone, recurrence, and calendar operations."""


def _nth_sunday(year: int, month: int, nth: int) -> datetime:
    first = datetime(year, month, 1)
    offset = (6 - first.weekday()) % 7
    day = 1 + offset + 7 * (nth - 1)
    return datetime(year, month, day)


def _last_sunday(year: int, month: int) -> datetime:
    next_month = datetime(year + (month // 12), (month % 12) + 1, 1)
    last = next_month - timedelta(days=1)
    offset = (last.weekday() - 6) % 7
    return last - timedelta(days=offset)


class RuleZone(tzinfo):
    """Pure-Python deterministic DST tzinfo supporting US and EU rules."""

    def __init__(
        self,
        name: str,
        std_offset: timedelta,
        dst_offset: timedelta,
        std_name: str,
        dst_name: str,
        rule: Literal["US", "EU"],
    ) -> None:
        self.name = name
        self._std_offset = std_offset
        self._dst_offset = dst_offset
        self._std_name = std_name
        self._dst_name = dst_name
        self._rule = rule

    def transitions_utc(self, year: int) -> tuple[datetime, datetime]:
        """Return `(spring_utc_naive, fall_utc_naive)` instants for `year`."""
        if self._rule == "US":
            spring_local = _nth_sunday(year, 3, 2).replace(hour=2, minute=0)
            fall_local = _nth_sunday(year, 11, 1).replace(hour=2, minute=0)
            return (spring_local - self._std_offset, fall_local - self._dst_offset)
        spring_utc = _last_sunday(year, 3).replace(hour=1, minute=0)
        fall_utc = _last_sunday(year, 10).replace(hour=1, minute=0)
        return (spring_utc, fall_utc)

    def _is_dst_wall(self, dt: datetime) -> bool:
        spring_utc, fall_utc = self.transitions_utc(dt.year)
        naive = dt.replace(tzinfo=None)
        dst_delta = self._dst_offset - self._std_offset
        fall_start_wall = fall_utc + self._std_offset
        fall_end_wall = fall_utc + self._dst_offset
        spring_end_wall = spring_utc + self._dst_offset
        if naive < spring_end_wall:
            return False
        if naive < fall_start_wall:
            return True
        if naive < fall_end_wall:
            return dt.fold == 0
        del dst_delta
        return False

    def utcoffset(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return self._std_offset
        return self._dst_offset if self._is_dst_wall(dt) else self._std_offset

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta(0)
        return (self._dst_offset - self._std_offset) if self._is_dst_wall(dt) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        if dt is not None and self._is_dst_wall(dt):
            return self._dst_name
        return self._std_name

    def fromutc(self, dt: datetime) -> datetime:
        if dt.tzinfo is not self:
            raise ValueError("dt.tzinfo must be self")
        utc_naive = dt.replace(tzinfo=None)
        spring_utc, fall_utc = self.transitions_utc(utc_naive.year)
        if spring_utc <= utc_naive < fall_utc:
            return (utc_naive + self._dst_offset).replace(tzinfo=self, fold=0)
        gap = self._dst_offset - self._std_offset
        in_fold = fall_utc <= utc_naive < (fall_utc + gap)
        return (utc_naive + self._std_offset).replace(tzinfo=self, fold=1 if in_fold else 0)


_US_EASTERN = RuleZone(
    "America/New_York", timedelta(hours=-5), timedelta(hours=-4), "EST", "EDT", "US"
)
_EU_WARSAW = RuleZone(
    "Europe/Warsaw", timedelta(hours=1), timedelta(hours=2), "CET", "CEST", "EU"
)
_FIXED_RE = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")


def get_timezone(name: str) -> tzinfo:
    if name == "UTC":
        return timezone.utc
    if name == "America/New_York":
        return _US_EASTERN
    if name == "Europe/Warsaw":
        return _EU_WARSAW
    m = _FIXED_RE.match(name)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hh, mm = int(m.group(2)), int(m.group(3))
        if hh <= 23 and mm <= 59:
            return timezone(sign * timedelta(hours=hh, minutes=mm))
    raise ScheduleError(f"unknown timezone: {name}")


def resolve_wall_time(
    naive_dt: datetime,
    tz_name: str,
    *,
    gap_policy: Literal["shift", "skip", "error"] = "shift",
    ambiguous_policy: Literal["first", "second", "error"] = "first",
) -> datetime | None:
    del gap_policy, ambiguous_policy
    # Starter bug: attaches tzinfo naively without detecting nonexistent gaps or ambiguous fall-back folds.
    tz = get_timezone(tz_name)
    return naive_dt.replace(tzinfo=tz)
