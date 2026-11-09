"""The plumbing of `--from-json` batches and single-record commands: reading the file, validating every entry,
recording all or nothing, and the flag checks that go with it."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from craftsman.evidence import (
    consume_inbox_file,
)
from craftsman.files import decode_json_bytes
from craftsman.ledger import (
    load,
    mutate,
)
from craftsman.records import (
    BatchRejected,
    EntryProblem,
    FieldError,
    Fields,
    Ledger,
    LedgerError,
    MultiFieldError,
    collecting_problems,
)
from craftsman.review_common import PROG
from craftsman.workspace import (
    Workspace,
)

UTF8_SAVE_HINT = "save it as UTF-8 (PowerShell: Set-Content -Encoding utf8)"


R = TypeVar("R")


T = TypeVar("T")


JSON_FIELD_NAMES = {
    "path": "file",
    "verdict": "as",
    "verdict_evidence": "evidence",
    "verdict_run": "run",
}


def require_flags(args: argparse.Namespace, flags: Mapping[str, object]) -> None:
    missing = [flag for flag, value in flags.items() if value is None]
    if missing:
        args.subparser.error(f"the following arguments are required: {', '.join(missing)}")


def reject_flags_with_from_json(flags: Mapping[str, object]) -> None:
    used = [flag for flag, value in flags.items() if value is not None]
    if used:
        raise LedgerError(f"--from-json cannot be combined with {', '.join(used)}")


def decode_input(data: bytes, label: str) -> str:
    try:
        return decode_json_bytes(data)
    except UnicodeDecodeError as exc:
        raise LedgerError(f"cannot read {label}: {exc}; {UTF8_SAVE_HINT}") from exc


def load_json_entries(source: str) -> list[Any]:
    """Read and parse a non-empty JSON list of entry objects from `source` (`-` for stdin)."""
    label = "stdin" if source == "-" else source
    try:
        data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read {label}: {exc}") from exc
    text = decode_input(data, label)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"invalid JSON in {label}: {exc}") from exc
    if not isinstance(raw, list):
        raise LedgerError(f"--from-json expects a JSON list of objects (got {type(raw).__name__})")
    if not raw:
        raise LedgerError("--from-json list must not be empty")
    return raw


def entry_label(raw: Any, key: str) -> str | None:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


class EntryReader:
    """Reads one `--from-json` entry and keeps every problem, so a rejected batch lists them all."""

    def __init__(self, raw: Any) -> None:
        self.fields = Fields(raw)
        self.errors: list[LedgerError] = []
        self._failed: set[str] = set()

    def read(self, key: str, get: Callable[[Fields], T], fallback: T) -> T:
        try:
            return get(self.fields)
        except FieldError as exc:
            self.errors.append(exc)
            self._failed.add(key)
            return fallback

    def failed(self, *keys: str) -> bool:
        return any(key in self._failed for key in keys)

    def reject_unknown(self) -> None:
        self.errors.extend(
            FieldError(key, "is not a known field") for key in self.fields.unknown_fields()
        )

    def finish(self, step: Callable[[list[LedgerError]], R | None]) -> R:
        """Run `step` with the problems of the semantic checks collected next to the field problems."""
        with collecting_problems() as sink:
            try:
                result = step(sink)
            except LedgerError as exc:
                sink.append(exc)
                result = None
        errors = [*self.errors, *sink]
        if not errors and result is not None:
            return result
        if not errors:
            raise LedgerError("the entry was not recorded")
        raise errors[0] if len(errors) == 1 else MultiFieldError(errors)


def attempt(sink: list[LedgerError], get: Callable[[], T], fallback: T) -> T:
    try:
        return get()
    except LedgerError as exc:
        sink.append(exc)
        return fallback


def _entry_problem(index: int, label: str | None, exc: LedgerError) -> EntryProblem:
    field = JSON_FIELD_NAMES.get(exc.field, exc.field)
    problem = exc.problem if isinstance(exc, FieldError) else str(exc)
    return EntryProblem(index=index, label=label, field=field, problem=problem)


def record_entries(
    command: str,
    raw_entries: Sequence[Any],
    current: Ledger,
    label_for: Callable[[int, Any], str | None],
    record_one: Callable[[int, Ledger, Any], tuple[Ledger, R]],
) -> tuple[Ledger, list[R]]:
    results: list[R] = []
    problems: list[EntryProblem] = []
    for index, raw in enumerate(raw_entries, 1):
        try:
            current, item = record_one(index, current, raw)
        except LedgerError as exc:
            label = label_for(index, raw)
            found = exc.errors if isinstance(exc, MultiFieldError) else (exc,)
            problems.extend(_entry_problem(index, label, error) for error in found)
            continue
        results.append(item)
    if problems:
        raise BatchRejected(f"{PROG} {command}", tuple(problems), len(raw_entries))
    return current, results


def print_check_passed(count: int) -> int:
    noun = "entry" if count == 1 else "entries"
    print(f"Check passed: {count} {noun} valid; nothing was recorded.")
    return 0


def run_single(
    ws: Workspace, check: bool, record: Callable[[Ledger], tuple[Ledger, R]], emit: Callable[[R], None]
) -> int:
    if check:
        record(load(ws.ledger_path))
        return print_check_passed(1)
    emit(mutate(ws.ledger_path, record))
    return 0


def run_batch(
    ws: Workspace,
    args: argparse.Namespace,
    record_batch: Callable[[Ledger], tuple[Ledger, list[R]]],
    emit: Callable[[R], None],
) -> int:
    if args.check:
        _, checked = record_batch(load(ws.ledger_path))
        return print_check_passed(len(checked))
    recorded = mutate(ws.ledger_path, record_batch)
    consume_inbox_file(ws.ledger_path.parent, args.from_json)
    for item in recorded:
        emit(item)
    return 0
