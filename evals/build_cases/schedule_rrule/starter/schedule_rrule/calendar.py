"""Calendar series, overrides, exdates, and window expansion."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from .rrule import RRule
from .tz import resolve_wall_time


@dataclass(frozen=True)
class EventOverride:
    original_start: datetime
    new_start: datetime | None = None
    new_duration: timedelta | None = None
    title: str | None = None
    cancelled: bool = False


@dataclass(frozen=True)
class CalendarSeries:
    series_id: str
    title: str
    dtstart: datetime
    duration: timedelta
    tz_name: str
    rrule: RRule | None = None
    exdates: tuple[datetime, ...] = ()
    overrides: tuple[EventOverride, ...] = ()
    gap_policy: Literal["shift", "skip", "error"] = "shift"
    ambiguous_policy: Literal["first", "second", "error"] = "first"
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Occurrence:
    series_id: str
    title: str
    start: datetime
    end: datetime
    original_start: datetime


def expand_events(
    series_list: Sequence[CalendarSeries],
    window_start: datetime,
    window_end: datetime,
) -> list[Occurrence]:
    out: list[Occurrence] = []
    for series in series_list:
        if series.rrule is None:
            starts = [
                series.dtstart
                if series.dtstart.tzinfo is not None
                else (
                    resolve_wall_time(
                        series.dtstart,
                        series.tz_name,
                        gap_policy=series.gap_policy,
                        ambiguous_policy=series.ambiguous_policy,
                    )
                    or series.dtstart
                )
            ]
        else:
            starts = series.rrule.expand(
                series.dtstart,
                series.tz_name,
                gap_policy=series.gap_policy,
                ambiguous_policy=series.ambiguous_policy,
            )
        for st in starts:
            en = st + series.duration
            if en > window_start and st < window_end:
                out.append(Occurrence(series.series_id, series.title, st, en, st))
    out.sort(key=lambda o: (o.start.astimezone(timezone.utc), o.series_id))
    return out
