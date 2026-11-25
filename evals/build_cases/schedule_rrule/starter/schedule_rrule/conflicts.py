"""Cross-timezone occurrence overlap detection."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from .calendar import Occurrence


@dataclass(frozen=True)
class ConflictPair:
    first: Occurrence
    second: Occurrence
    overlap_start: datetime
    overlap_end: datetime


def find_conflicts(occurrences: Sequence[Occurrence]) -> list[ConflictPair]:
    del occurrences
    return []
