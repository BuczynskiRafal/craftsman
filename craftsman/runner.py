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
from pathlib import Path
from typing import IO

from craftsman.diffscan import DiffScanError, DiffSnapshot, snapshot
from craftsman.ledger import (
    OUTPUT_TAIL_CHARS,
    RUN_KINDS,
    Ledger,
    LedgerError,
    Run,
    add_run,
    format_timestamp,
    is_trivial_run,
    load,
    mutate,
)
from craftsman.review import Workspace, literal_choice, open_workspace, resolve_base

CHANGED_PATHS_SHOWN = 5
PROG = "craftsman run"
DEFAULT_TIMEOUT_S = 1800.0
EXIT_TIMEOUT = 124
EXIT_NOT_STARTED = 127
EXIT_INTERRUPTED = 130
TERMINATE_GRACE_S = 5.0
TASKKILL_TIMEOUT_S = 10.0
READER_JOIN_TIMEOUT_S = 5.0
_TAIL_BYTES = OUTPUT_TAIL_CHARS * 4  # enough bytes for 4000 characters of UTF-8


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    duration_s: float
    output_tail: str
    timed_out: bool
    interrupted: bool


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
    timed_out = interrupted = False
    try:
        code = process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop(process, graceful=False)
        code, timed_out = EXIT_TIMEOUT, True
    except KeyboardInterrupt:
        _stop(process, graceful=True)
        code, interrupted = EXIT_INTERRUPTED, True
    # A process the command left behind may still hold the pipe open; closing it under the reader
    # could hang on Windows, and the reader ends with this process anyway.
    if pump.join():
        process.stdout.close()
    duration = time.monotonic() - started
    exit_code = code if code >= 0 else 128 - code  # killed by signal N -> 128 + N, like shells
    return Outcome(exit_code, duration, pump.tail(), timed_out, interrupted)


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


def _snapshot(ws: Workspace) -> DiffSnapshot:
    return snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))


def _changed_paths(before: DiffSnapshot, after: DiffSnapshot) -> list[str]:
    """Files whose part of the diff differs between two snapshots of the same base."""
    old = {(hunk.path, hunk.digest) for hunk in before.hunks} | {(path, "") for path in before.files}
    new = {(hunk.path, hunk.digest) for hunk in after.hunks} | {(path, "") for path in after.files}
    return sorted({path for path, _ in old ^ new})


def _record(ws: Workspace, run: Run) -> Run:
    def append(current: Ledger) -> tuple[Ledger, Run]:
        updated = add_run(current, run)
        return updated, updated.runs[-1]

    return mutate(ws.ledger_path, append)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        usage=f"{PROG} [--kind KIND] [--cwd DIR] [--timeout SEC] -- <command> [args...]",
        description="Run a command and record its exit code in the Craftsman evidence ledger.",
    )
    parser.add_argument(
        "--kind", type=literal_choice(RUN_KINDS), default="other", help=", ".join(RUN_KINDS)
    )
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
    cwd = Path(args.cwd or os.getcwd()).resolve()
    try:
        ws = open_workspace(str(cwd))
        before_snap = _snapshot(ws)
        before = before_snap.fingerprint
        started_at = format_timestamp(datetime.now(timezone.utc))
        try:
            outcome = execute(command, cwd, args.timeout)
        except CommandNotStarted as exc:
            sys.stderr.write(f"{PROG}: error: {exc}\n")
            return EXIT_NOT_STARTED
        if outcome.timed_out:
            sys.stderr.write(f"{PROG}: timed out after {args.timeout:g}s; the command was killed\n")
        after_snap = _snapshot(ws)
        after = after_snap.fingerprint
        if after != before:
            changed = _changed_paths(before_snap, after_snap)
            shown = ", ".join(changed[:CHANGED_PATHS_SHOWN]) + (" …" if len(changed) > CHANGED_PATHS_SHOWN else "")
            sys.stderr.write(
                f"{PROG}: warning: the command modified the working tree ({shown or 'unknown files'}); "
                "this run does not count as a passing run at either version of the code. If those "
                "files are output the command always writes, add them to .gitignore or "
                ".craftsmanignore; then run it again.\n"
            )
        run = _record(
            ws,
            Run(
                id="",
                kind=args.kind,
                argv=tuple(command),
                cwd=str(cwd),
                exit_code=outcome.exit_code,
                duration_s=round(outcome.duration_s, 3),
                fingerprint=before,
                started_at=started_at,
                output_tail=outcome.output_tail,
                fingerprint_after=after if after != before else None,
            ),
        )
    except (LedgerError, DiffScanError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 1
    sys.stderr.write(f"{run.id} recorded ({run.kind}, exit {run.exit_code}, {run.duration_s:.1f}s)\n")
    if is_trivial_run(run.argv):
        sys.stderr.write(
            f"{PROG}: warning: `{run.command}` is trivial and does not count as verification\n"
        )
    return EXIT_INTERRUPTED if outcome.interrupted else outcome.exit_code
