"""Small pieces every part of `craftsman review` shares: the program name, the refusal error and a few helpers."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from craftsman import huntplan
from craftsman.records import (
    Ledger,
    LedgerError,
    Slot,
)

PROG = "craftsman review"


def comma_list(raw: str) -> list[str]:
    """The non-blank items of a comma-separated flag value, stripped."""
    return [item.strip() for item in raw.split(",") if item.strip()]


class CommandRefused(LedgerError):
    """A command the caller must not run (or not run yet): reported like an error, with its own exit code."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def slot_list(slots: Sequence[Slot]) -> str:
    return ", ".join(slot.id for slot in slots) or "none"


def cwd_of(args: argparse.Namespace) -> Path:
    return Path(args.cwd or os.getcwd())


def store_due_waves(ledger: Ledger, now: datetime | None = None) -> Ledger:
    """`ledger` with the waves that are due stored in the plan of its latest round; the same object when none is."""
    if not ledger.rounds or ledger.rounds[-1].plan is None:
        return ledger
    return huntplan.plan_due_waves(ledger, datetime.now(timezone.utc) if now is None else now)
