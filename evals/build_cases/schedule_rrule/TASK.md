# Task: Complete Timezone-Aware Recurring Calendar Expander & Conflict Detector

Complete `schedule_rrule` so DST wall-time resolution, `RRule` local-time expansion (`DAILY`/`WEEKLY`/`MONTHLY` with `skip`/`clamp`), `COUNT`/`UNTIL`/`EXDATE` ordering, `RECURRENCE-ID` single-occurrence overrides, half-open window queries, and UTC-aware conflict detection satisfy every requirement below while keeping `tests/test_starter.py` passing.

## Requirements

1. **Deterministic Timezone & Wall-Time Resolution (`tz.py`)**
   - Built-in deterministic timezones supported without OS tzdata:
     - `"UTC"`
     - Fixed offsets `"UTC+HH:MM"` / `"UTC-HH:MM"`
     - `"America/New_York"`: `EST` (`UTC-5`) / `EDT` (`UTC-4`), springing forward 2nd Sunday of March at `02:00` local (`[02:00, 03:00)` nonexistent) and falling back 1st Sunday of November at `02:00` local (`[01:00, 02:00)` occurs twice: first/EDT `fold=0`, second/EST `fold=1`).
     - `"Europe/Warsaw"`: `CET` (`UTC+1`) / `CEST` (`UTC+2`), springing forward last Sunday of March at `01:00 UTC` (`[02:00, 03:00)` nonexistent) and falling back last Sunday of October at `01:00 UTC` (`[02:00, 03:00)` occurs twice: first/CEST `fold=0`, second/CET `fold=1`).
   - Unknown timezone names raise `ScheduleError`.
   - `resolve_wall_time(naive_dt: datetime, tz_name: str, *, gap_policy: Literal["shift", "skip", "error"] = "shift", ambiguous_policy: Literal["first", "second", "error"] = "first") -> datetime | None`:
     - If `naive_dt` falls in a spring-forward gap:
       - `"shift"`: advance by the DST gap size (`+dst_step`, e.g. `02:30 -> 03:30`) in the new offset.
       - `"skip"`: return `None`.
       - `"error"`: raise `ScheduleError`.
     - If `naive_dt` falls in a fall-back ambiguous hour:
       - `"first"`: return `fold=0` (the earlier, DST instant).
       - `"second"`: return `fold=1` (the later, STD instant).
       - `"error"`: raise `ScheduleError`.

2. **Recurrence Expansion (`rrule.py`)**
   - `RRule(freq, interval=1, count=None, until=None, byweekday=(), bymonthday=(), monthday_policy="skip")`
   - Validate `interval >= 1`, `count >= 1` (if set), and forbid setting both `count` and `until` (`ScheduleError`). Note: weekdays use `0=Mon..6=Sun`, month days use `1..31` or `-1` (last day of month).
   - Recurrence candidates step in **local wall time** (`naive_dt`) starting from `dtstart` and resolve each candidate via `resolve_wall_time(..., gap_policy=gap_policy, ambiguous_policy=ambiguous_policy)` so wall-clock time (e.g. `09:00` local) stays `09:00` local across DST transitions!
   - `DAILY`: step by `interval` days.
   - `WEEKLY`: if `byweekday` is non-empty, generate occurrences within each `interval`-th week (week starting Monday of `dtstart`'s week) for each sorted weekday whose local datetime `>= dtstart_naive`; if empty, use `(dtstart_naive.weekday(),)`.
   - `MONTHLY`: if `bymonthday` is empty, use `(dtstart_naive.day,)`. For each `interval`-th month starting from `dtstart_naive`'s month:
     - Resolve `-1` to `days_in_month`.
     - If positive `d > days_in_month`:
       - `"skip"`: omit that day in this month.
       - `"clamp"`: clamp to `days_in_month` without altering the template day for later months (e.g. `Jan 31 -> Feb 28/29 -> Mar 31`).
     - Deduplicate and sort days within each month, skipping any candidate earlier than `dtstart_naive`.
   - Termination:
     - `count`: emit at most `count` generated rule candidates (counting before `exdates` subtraction; skipped nonexistent gap candidates with `gap_policy="skip"` do not emit or consume a count slot).
     - `until`: stop once the resolved occurrence exceeds `until` (inclusive when compared in UTC; naive `until` is resolved in the series timezone).

3. **Series, Exdates, Overrides & Windowing (`calendar.py`)**
   - `EventOverride(original_start: datetime, new_start: datetime | None = None, new_duration: timedelta | None = None, title: str | None = None, cancelled: bool = False)`
   - Compare `exdates` and `overrides` keys against generated occurrences by **exact UTC instant** (`dt.astimezone(timezone.utc)`). Note: naive timestamps passed in `exdates`/`overrides` are interpreted in the series's `tz_name` using `first`/`shift` defaults.
   - Precedence between overrides and `exdates`:
     - An `exdate` removes the matching occurrence (`cancelled` / excluded), taking precedence even if an override shares that `original_start`.
     - Otherwise, an `EventOverride` either cancels (`cancelled=True`) or replaces the occurrence's `start` (`new_start` if provided), `duration` (`new_duration` if provided), and optional `title` (`title` if provided) while preserving `original_start`.
   - Half-open window filtering `[window_start, window_end)`:
     - An occurrence (`[occ.start, occ.end)`) matches the query window iff `occ.end > window_start and occ.start < window_end` (comparing in UTC).
     - An override whose **original** start fell outside `[window_start, window_end)` MUST still appear if its `new_start..new_end` overlaps `[window_start, window_end)`. Conversely, if an override moves an in-window occurrence completely outside `[window_start, window_end)`, it MUST NOT appear in the result!
   - `expand_events(series_list, window_start, window_end)` sorts output `Occurrence`s deterministically by `(occ.start.astimezone(timezone.utc), occ.series_id)`.

4. **Cross-Timezone Conflict Detection (`conflicts.py`)**
   - `find_conflicts(occurrences: Sequence[Occurrence]) -> list[ConflictPair]`
   - Compare intervals in UTC: two distinct occurrences overlap iff `a.start < b.end and b.start < a.end` (back-to-back where `a.end == b.start` is NOT a conflict).
   - For each conflicting pair `(first, second)` ordered canonically (`(first.start_utc, first.series_id) <= (second.start_utc, second.series_id)`), emit `ConflictPair(first, second, overlap_start=max(a.start, b.start), overlap_end=min(a.end, b.end))`, sorted deterministically by `(overlap_start_utc, first.series_id, second.series_id)`.
