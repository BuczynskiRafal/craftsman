"""One bounded, lazy reader of JSON Lines files, shared by the transcript readers.

The Claude Code usage reader (`craftsman.usage`) and the Antigravity transcript reader
(`craftsman.transcript`) parse different formats but read their files the same way: line by line, a line
that is not JSON is data (the caller decides what it means), the last line may still be growing, and a
file past the byte cap is not read. What each line MEANS stays with the caller.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

MAX_TRANSCRIPT_BYTES = 256 * 1024 * 1024  # a transcript larger than this is not read


class StopReason(str, Enum):
    """Why a `JsonlReader` ended (see `JsonlReader.stopped`)."""

    END = "end"  # every line was read
    TOO_LARGE = "too-large"  # the file is larger than `max_bytes`; nothing was read


@dataclass(frozen=True)
class JsonlLine:
    """One line of the file: its text and, for a line that is not blank, what `json.loads` made of it.

    `value` is any JSON value (a caller that wants objects checks the type). A blank line has neither
    `value` nor `error`. `complete` is False only for a last line without its newline: it may still be
    written to, so a caller may treat a parse error there as "not finished" instead of "corrupt".
    """

    number: int  # 1-based, blank lines counted
    text: str  # without the line break
    complete: bool
    value: Any = None
    error: json.JSONDecodeError | None = None

    @property
    def blank(self) -> bool:
        return not self.text.strip()


class JsonlReader:
    """Iterate the lines of a JSON Lines file lazily; breaking out of the loop reads no more of it.

    `errors` is the text decoding error handler (`strict` raises `UnicodeDecodeError` from the iteration,
    `replace` never does). A file larger than `max_bytes` yields nothing. `stopped` says why the iteration
    ended once it has; it is None before the first line is asked for, and while the loop is still running.
    Filesystem errors (`OSError`) propagate: the caller decides what an unreadable file means.
    """

    def __init__(
        self,
        path: Path,
        *,
        errors: str = "strict",
        max_bytes: int | None = None,
    ) -> None:
        self.path = path
        self.stopped: StopReason | None = None
        self._errors = errors
        self._max_bytes = max_bytes

    def __iter__(self) -> Iterator[JsonlLine]:
        self.stopped = None
        if self._max_bytes is not None and self.path.stat().st_size > self._max_bytes:
            self.stopped = StopReason.TOO_LARGE
            return
        with self.path.open(encoding="utf-8", errors=self._errors) as handle:
            for number, raw in enumerate(handle, start=1):
                complete = raw.endswith("\n")
                text = raw[:-1] if complete else raw
                yield self._parse(number, text, complete)
        self.stopped = StopReason.END

    @staticmethod
    def _parse(number: int, text: str, complete: bool) -> JsonlLine:
        if not text.strip():
            return JsonlLine(number, text, complete)
        try:
            return JsonlLine(number, text, complete, value=json.loads(text))
        except json.JSONDecodeError as exc:
            return JsonlLine(number, text, complete, error=exc)
