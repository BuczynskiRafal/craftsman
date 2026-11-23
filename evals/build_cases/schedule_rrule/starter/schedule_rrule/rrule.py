"""Recurrence rule specification and local wall-time expansion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .tz import ScheduleError, resolve_wall_time


@dataclass(frozen=True)
class RRule:
    freq: Literal["DAILY", "WEEKLY", "MONTHLY"]
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    byweekday: tuple[int, ...] = ()
    bymonthday: tuple[int, ...] = ()
    monthday_policy: Literal["skip", "clamp"] = "skip"

    def __post_init__(self) -> None:
        if self.interval < 1:
            raise ScheduleError("interval must be >= 1")
        if self.count is not None and self.count < 1:
            raise ScheduleError("count must be >= 1")
        if self.count is not None and self.until is not None:
            raise ScheduleError("count and until are mutually exclusive")

    def expand(
        self,
        dtstart: datetime,
        tz_name: str,
        *,
        gap_policy: Literal["shift", "skip", "error"] = "shift",
        ambiguous_policy: Literal["first", "second", "error"] = "first",
        limit: int = 1000,
    ) -> list[datetime]:
        # Starter bug: only supports simple DAILY count in UTC duration steps, drifting across DST.
        if self.freq != "DAILY":
            raise NotImplementedError(f"freq={self.freq} not implemented in starter")
        naive = dtstart.replace(tzinfo=None)
        results: list[datetime] = []
        max_items = self.count if self.count is not None else limit
        cur = naive
        while len(results) < max_items:
            resolved = resolve_wall_time(cur, tz_name, gap_policy=gap_policy, ambiguous_policy=ambiguous_policy)
            if resolved is not None:
                results.append(resolved)
            cur = cur + timedelta(days=self.interval)
        return results
