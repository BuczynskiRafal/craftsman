"""`craftsman run`: execute a command and record its exit code in the evidence ledger.

The run is bound to the fingerprint of the work tree *before* it started, so the gate can tell
whether it verified the final code; a command that changes the work tree never counts as a
passing run. Output is streamed through unchanged; only its tail is kept.
"""

from __future__ import annotations

import argparse
import codecs
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import IO

from craftsman.diffscan import DiffScanError, DiffSnapshot
from craftsman.gate import GateStateError
from craftsman.ledger import add_run, load, mutate
from craftsman.records import (
    OUTPUT_TAIL_CHARS,
    RUN_KINDS,
    LedgerError,
    Run,
    RunKind,
    format_timestamp,
)
from craftsman.triviality import is_trivial_run
from craftsman.workspace import open_workspace

CHANGED_PATHS_SHOWN = 5
PROG = "craftsman run"
DEFAULT_TIMEOUT_S = 1800.0
EXIT_TIMEOUT = 124
EXIT_NOT_STARTED = 127
EXIT_INTERRUPTED = 130
TERMINATE_GRACE_S = 5.0
TASKKILL_TIMEOUT_S = 10.0
READER_JOIN_TIMEOUT_S = 5.0
# A UTF-8 character takes at most 4 bytes.
_TAIL_BYTES = OUTPUT_TAIL_CHARS * 4


class RunStatus(Enum):
    COMPLETED = "completed"
    TIMED_OUT = "timed out"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    duration_s: float
    output_tail: str
    status: RunStatus = RunStatus.COMPLETED


class CommandNotStarted(Exception):
    """The command could not be started: a missing program, no permission or a bad directory."""


class _OutputPump:
    """Copies the child's output to our stdout as it arrives and keeps the last bytes of it."""

    def __init__(self, source: IO[bytes]) -> None:
        self._source = source
        self._tail = bytearray()
        self._tail_lock = threading.Lock()
        self._echo = True
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> bool:
        """Wait for the output to end; False while a leftover process still holds the pipe open."""
        self._thread.join(READER_JOIN_TIMEOUT_S)
        return not self._thread.is_alive()

    def tail(self) -> str:
        with self._tail_lock:
            data = bytes(self._tail)
        return data.decode("utf-8", errors="replace")[-OUTPUT_TAIL_CHARS:]

    def _run(self) -> None:
        source = self._source.fileno()
        while chunk := os.read(source, 65536):
            with self._tail_lock:
                self._tail += chunk
                del self._tail[:-_TAIL_BYTES]
            if self._echo:
                self._write(chunk)

    def _write(self, chunk: bytes) -> None:
        try:
            sink = getattr(sys.stdout, "buffer", None)
            if sink is not None:
                sink.write(chunk)
                sink.flush()
            else:
                sys.stdout.write(self._decoder.decode(chunk))
                sys.stdout.flush()
        except OSError:
            # Whoever reads our output went away (e.g. `| head`); keep draining the child so it
            # never blocks on a full pipe, and keep the tail for the ledger.
            self._echo = False


def execute(argv: Sequence[str], cwd: Path, timeout_s: float) -> Outcome:
    """Run `argv`, streaming its combined output; raises CommandNotStarted if it cannot start."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [_program(argv[0], cwd), *argv[1:]],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        raise CommandNotStarted(f"cannot start {argv[0]!r}: {exc}") from exc
    assert process.stdout is not None
    pump = _OutputPump(process.stdout)
    pump.start()
    status = RunStatus.COMPLETED
    try:
        code = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(process, graceful=False)
        code, status = EXIT_TIMEOUT, RunStatus.TIMED_OUT
    except KeyboardInterrupt:
        _stop(process, graceful=True)
        code, status = EXIT_INTERRUPTED, RunStatus.INTERRUPTED
    try:
        drained = pump.join()
    except KeyboardInterrupt:
        drained, status = False, RunStatus.INTERRUPTED
    # A process the command left behind may still hold the pipe open; closing it under the reader
    # could hang on Windows, and the reader ends with this process anyway.
    if drained:
        process.stdout.close()
    duration = time.monotonic() - started
    exit_code = code if code >= 0 else 128 - code  # killed by signal N -> 128 + N, like shells
    return Outcome(exit_code, duration, pump.tail(), status)


def _program(executable: str, cwd: Path) -> str:
    """The program to start; a relative path with a directory part is relative to `cwd`.

    POSIX resolves such paths against the child's working directory; Windows would use ours.
    """
    has_directory = os.sep in executable or (os.altsep is not None and os.altsep in executable)
    if has_directory and not os.path.isabs(executable):
        executable = str(cwd / executable)
    if os.name == "nt":  # find `.exe`, `.cmd` and `.bat` files through PATHEXT
        return shutil.which(executable) or executable
    return executable


def _stop(process: subprocess.Popen[bytes], *, graceful: bool) -> None:
    """Stop the command and every process it started."""
    if os.name == "nt":
        _stop_windows_tree(process)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM if graceful else signal.SIGKILL)
        process.wait(timeout=TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the group exited between the timeout and the kill
        process.wait()
    except ProcessLookupError:
        process.wait()


def _stop_windows_tree(process: subprocess.Popen[bytes]) -> None:
    """`taskkill /T` also ends the command's descendants; killing the command alone is the fallback."""
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            timeout=TASKKILL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"{PROG}: warning: taskkill failed ({exc}); stopping only the command\n")
    if process.poll() is None:
        process.kill()
    process.wait()


def _changed_paths(before: DiffSnapshot, after: DiffSnapshot) -> list[str]:
    """Files whose part of the diff differs between two snapshots of the same base."""
    old = {(hunk.path, hunk.digest) for hunk in before.hunks} | {(path, "") for path in before.files}
    new = {(hunk.path, hunk.digest) for hunk in after.hunks} | {(path, "") for path in after.files}
    return sorted({path for path, _ in old ^ new})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        usage=f"{PROG} [--kind KIND] [--cwd DIR] [--timeout SEC] -- <command> [args...]",
        description="Run a command and record its exit code in the Craftsman evidence ledger.",
    )
    parser.add_argument("--kind", choices=RUN_KINDS, default="other", help=", ".join(RUN_KINDS))
    parser.add_argument("--cwd", help="Directory to run in (default: current)")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="Seconds (default 1800)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    if "--" not in raw:
        if "-h" in raw or "--help" in raw:
            parser.print_help()
            return 0
        parser.error("put `--` between craftsman options and the command")
    split = raw.index("--")
    args = parser.parse_args(raw[:split])
    command = raw[split + 1 :]
    if not command:
        parser.error("missing command after `--`")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        return _run_recorded(command, Path(args.cwd or os.getcwd()).resolve(), args.kind, args.timeout)
    except (LedgerError, DiffScanError, GateStateError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 1


def _run_recorded(command: Sequence[str], cwd: Path, kind: RunKind, timeout_s: float) -> int:
    """Run `command` in `cwd` and record the run; the exit status to report to the caller."""
    ws = open_workspace(str(cwd))
    ledger = load(ws.ledger_path)
    before, started_at = ws.snapshot(ledger), format_timestamp(datetime.now(timezone.utc))
    try:
        outcome = execute(command, cwd, timeout_s)
    except CommandNotStarted as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return EXIT_NOT_STARTED
    if outcome.status is RunStatus.TIMED_OUT:
        sys.stderr.write(f"{PROG}: timed out after {timeout_s:g}s; the command was killed\n")
    try:
        after = ws.snapshot(ledger)
    except KeyboardInterrupt:
        sys.stderr.write(f"{PROG}: interrupted; the run was not recorded\n")
        return EXIT_INTERRUPTED
    if modified := (after.fingerprint != before.fingerprint):
        _warn_modified(before, after)
    run = _persist_run(ws.ledger_path, command, cwd, kind, outcome, before, after, started_at, modified)
    sys.stderr.write(f"{run.id} recorded ({run.kind}, exit {run.exit_code}, {run.duration_s:.1f}s)\n")
    if is_trivial_run(run.argv):
        sys.stderr.write(
            f"{PROG}: warning: `{run.command}` is trivial and does not count as verification\n"
        )
    return EXIT_INTERRUPTED if outcome.status is RunStatus.INTERRUPTED else outcome.exit_code


def _persist_run(
    ledger_path: Path,
    command: Sequence[str],
    cwd: Path,
    kind: RunKind,
    outcome: Outcome,
    before: DiffSnapshot,
    after: DiffSnapshot,
    started_at: str,
    modified: bool,
) -> Run:
    return mutate(
        ledger_path,
        lambda current: add_run(
            current,
            kind=kind,
            argv=command,
            cwd=str(cwd),
            exit_code=outcome.exit_code,
            duration_s=round(outcome.duration_s, 3),
            fingerprint=before.fingerprint,
            started_at=started_at,
            output_tail=outcome.output_tail,
            fingerprint_after=after.fingerprint if modified else None,
        ),
    )


def _warn_modified(before: DiffSnapshot, after: DiffSnapshot) -> None:
    changed = _changed_paths(before, after)
    shown = ", ".join(changed[:CHANGED_PATHS_SHOWN]) + (
        " …" if len(changed) > CHANGED_PATHS_SHOWN else ""
    )
    sys.stderr.write(
        f"{PROG}: warning: the command modified the working tree ({shown or 'unknown files'}); "
        "this run does not count as a passing run at either version of the code. If those "
        "files are output the command always writes, add them to .gitignore or "
        ".craftsmanignore; then run it again.\n"
    )
