"""`craftsman run`: execute a command and record its exit code in the evidence ledger.

The run is bound to the fingerprint of the work tree *before* it started, so the gate can tell
whether it verified the final code; a command that changes the work tree never counts as a
passing run. Output is streamed through unchanged up to a live limit; past it, all of the output
goes to a log in the managed scratch directory, named at once, and its end prints when the command
exits. The ledger keeps only the tail.
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
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import IO, Any

from craftsman.diffscan import DiffScanError, DiffSnapshot
from craftsman.evidence import create_scratch_dir
from craftsman.gate import GateStateError
from craftsman.ledger import add_run, load, mutate
from craftsman.records import (
    DEFAULT_MIN_TESTS,
    MIN_TESTS_HINT,
    OUTPUT_TAIL_CHARS,
    RUN_KINDS,
    Ledger,
    LedgerError,
    Run,
    RunKind,
    format_timestamp,
)
from craftsman.testcount import RunnerOutputCounter
from craftsman.triviality import is_trivial_run
from craftsman.workspace import Workspace, open_workspace

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
# Output prints as it arrives up to this many characters; past them it goes to a log, and when it
# ends, at most the last TAIL_CHARS characters print.
LIVE_LIMIT_CHARS = 6_000
TAIL_CHARS = 6_000
# A cut moves to a line boundary when that costs at most this many characters.
_LINE_SLACK_CHARS = 200
_LOG_NAME = "run-output.log"


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
    tests_ran: int | None = None  # what the test runners in the whole output reported; None: unrecognized


class CommandNotStarted(Exception):
    """The command could not be started: a missing program, no permission or a bad directory."""


class Terminated(KeyboardInterrupt):
    """SIGTERM or SIGHUP reached `craftsman run`; it stops the command the way Ctrl-C does."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _TerminationSignals:
    """While the run command runs, SIGTERM and SIGHUP take the path of Ctrl-C (POSIX, main thread only).

    The first of them raises `Terminated` while the command runs; later ones are ignored. Once the
    command has ended (`defer`), a signal no longer interrupts recording the run: it is only noted, and
    the run command exits with 128 + its number afterwards. The previous handlers come back on exit.
    """

    def __init__(self) -> None:
        self.signum: int | None = None
        self._raising = True
        self._previous: dict[int, Callable[[int, FrameType | None], Any] | int | None] = {}

    def __enter__(self) -> _TerminationSignals:
        if os.name == "nt" or threading.current_thread() is not threading.main_thread():
            return self
        for name in ("SIGTERM", "SIGHUP"):
            if (number := getattr(signal, name, None)) is not None:
                self._previous[number] = signal.signal(number, self._handle)
        return self

    def __exit__(self, *_: object) -> None:
        for number, previous in self._previous.items():
            if previous is not None:  # None: a handler not installed from Python, which cannot be put back
                signal.signal(number, previous)

    def defer(self) -> None:
        self._raising = False

    def exit_code(self, otherwise: int) -> int:
        return otherwise if self.signum is None else 128 + self.signum

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self.signum is not None:
            return
        self.signum = signum
        if self._raising:
            raise Terminated(signum)


class _EchoMode(Enum):
    LIVE = "live"  # printing as it arrives, below the live limit
    HELD = "held"  # past the limit: the log takes the output, the end prints when it ends
    ALL = "all"  # past the limit with no log: printing as it arrives


class OutputEcho:
    """Prints the child's output to our stdout as it arrives, up to a live limit; past it, a log takes over.

    Up to `LIVE_LIMIT_CHARS` characters print unchanged as they arrive. When the output passes the
    limit, printing stops and a marker line names a log in the managed scratch directory. The log
    holds the whole output from the first byte and grows as the output arrives, so it can be read
    while the command still runs; its path never changes. When the output ends, what came after the
    live part prints whole if it is at most `TAIL_CHARS` characters, else as a marker line and its
    last `TAIL_CHARS` characters. If the log cannot be written, the marker says why and everything
    prints as it arrives. Text is handled with `surrogateescape`, so bytes that are not UTF-8 reach
    stdout and the log unchanged; memory holds at most the live part and the rolling tail.
    """

    def __init__(self, craftsman_dir: Path | None) -> None:
        """`craftsman_dir` holds the scratch directory; None prints everything as it arrives, with no log."""
        self._craftsman_dir = craftsman_dir
        self._lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
        self._display_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._reader_present = True
        self._finished = False
        self._mode = _EchoMode.LIVE
        self._live = ""  # what printed as it arrived, up to the live limit
        self._rest = ""  # the last TAIL_CHARS characters after the live part
        self._rest_chars = 0  # characters after the live part
        self._before_rest = ""  # the character in front of `_rest`
        self._log: IO[bytes] | None = None
        self._log_path: Path | None = None

    @property
    def log_path(self) -> Path | None:
        """The log of the whole output, once the output has passed the live limit and while the log is good."""
        return self._log_path

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if self._craftsman_dir is None:
                self._write(chunk)
            elif not self._finished:
                self._accept(self._decoder.decode(chunk))

    def finish(self) -> None:
        """The output has ended: print what was held back after the live part, if anything."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self._craftsman_dir is None:
                return
            self._accept(self._decoder.decode(b"", final=True))
            if (log := self._log) is None or self._log_path is None:
                return
            self._log = None
            try:
                log.close()
            except OSError as exc:
                self._abandon_log(log, exc)
                return
            self._write_held(f"full output: {self._log_path}", saved=True)

    def _accept(self, text: str) -> None:
        if not text:
            return
        if self._mode is _EchoMode.ALL:
            self._write_text(text)
        elif self._mode is _EchoMode.HELD:
            self._hold(text)
            assert self._log is not None
            try:
                self._log.write(_encode(text))
                self._log.flush()  # a reader of the log sees the output as it arrives
            except OSError as exc:
                self._abandon_log(self._log, exc)
        elif len(self._live) + len(text) <= LIVE_LIMIT_CHARS:
            self._live += text
            self._write_text(text)
        else:
            self._pass_limit(text)

    def _pass_limit(self, text: str) -> None:
        """Print `text` up to the live limit, then start the log with all output so far and name it."""
        cut = LIVE_LIMIT_CHARS - len(self._live)
        if not (self._live + text[:cut]).endswith("\n") and (
            end := text.find("\n", cut, cut + _LINE_SLACK_CHARS)
        ) >= 0:
            cut = end + 1  # the rest of the line has already arrived: end the live part with it
        everything, live, rest = self._live + text, text[:cut], text[cut:]
        self._write_text(live)
        self._live += live
        lead = "" if self._live.endswith("\n") else "\n"
        assert self._craftsman_dir is not None
        log: IO[bytes] | None = None
        path: Path | None = None
        try:
            path = (create_scratch_dir(self._craftsman_dir) / _LOG_NAME).resolve()
            log = path.open("wb")
            log.write(_encode(everything))
            log.flush()
        except (LedgerError, OSError) as exc:
            self._mode = _EchoMode.ALL
            _discard(log, path)
            self._write_text(f"{lead}[{PROG}: {_not_saved(exc)}]\n{rest}")
            return
        self._log, self._log_path, self._mode = log, path, _EchoMode.HELD
        self._write_text(
            f"{lead}[{PROG}: output continues; its end prints when the command exits; "
            f"full output, written as it arrives: {path}]\n"
        )
        self._hold(rest)

    def _hold(self, text: str) -> None:
        self._rest_chars += len(text)
        rolled = self._rest + text
        if len(rolled) > TAIL_CHARS:
            self._before_rest = rolled[-TAIL_CHARS - 1]
        self._rest = rolled[-TAIL_CHARS:]

    def _abandon_log(self, log: IO[bytes], exc: OSError) -> None:
        """The log broke: remove it, print what was held back and from then on print as it arrives.

        A half-written log would mislead whoever reads it.
        """
        path, self._log, self._log_path, self._mode = self._log_path, None, None, _EchoMode.ALL
        _discard(log, path)
        self._write_held(_not_saved(exc), saved=False)

    def _write_held(self, where: str, *, saved: bool) -> None:
        """Print the output held after the live part: all of it, or a marker line and its last part."""
        rest, self._rest = self._rest, ""
        if self._rest_chars > TAIL_CHARS:
            if self._before_rest != "\n" and (cut := rest.find("\n", 0, _LINE_SLACK_CHARS)) >= 0:
                rest = rest[cut + 1 :]
            self._write_text(f"[{PROG}: {self._rest_chars - len(rest)} characters omitted; {where}]\n{rest}")
        elif saved:
            self._write_text(rest)  # nothing was omitted
        else:
            self._write_text(f"[{PROG}: {where}]\n{rest}")

    def _write_text(self, text: str) -> None:
        if text:
            self._write(_encode(text))

    def _write(self, data: bytes) -> None:
        if not self._reader_present:
            return
        try:
            sink = getattr(sys.stdout, "buffer", None)
            if sink is not None:
                sink.write(data)
                sink.flush()
            else:
                sys.stdout.write(self._display_decoder.decode(data))
                sys.stdout.flush()
        except OSError:
            # Whoever reads our output went away (e.g. `| head`); keep draining the child so it
            # never blocks on a full pipe, and keep the tail for the ledger.
            self._reader_present = False


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


def _not_saved(exc: Exception) -> str:
    reason = " ".join(str(exc).split()) or type(exc).__name__
    return f"full output not saved: {reason}; the output continues below as it arrives"


def _discard(log: IO[bytes] | None, path: Path | None) -> None:
    with suppress(OSError):
        if log is not None:
            log.close()
    with suppress(OSError):
        if path is not None:
            path.unlink()


class _OutputPump:
    """Hands the child's output to `echo` as it arrives, keeps the last bytes of it and counts its tests."""

    def __init__(self, source: IO[bytes], echo: OutputEcho) -> None:
        self._source = source
        self._echo = echo
        self._tail = bytearray()
        self._tail_lock = threading.Lock()
        self._counter = RunnerOutputCounter()
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

    def tests_ran(self) -> int | None:
        """The tests the runners in all the output so far reported (not only the kept tail)."""
        with self._tail_lock:
            return self._counter.result()

    def _run(self) -> None:
        source = self._source.fileno()
        while chunk := os.read(source, 65536):
            with self._tail_lock:
                self._tail += chunk
                self._counter.feed(chunk)
                del self._tail[:-_TAIL_BYTES]
            self._echo.feed(chunk)


def execute(
    argv: Sequence[str], cwd: Path, timeout_s: float, echo: OutputEcho | None = None
) -> Outcome:
    """Run `argv`, printing its combined output through `echo` (default: all of it, as it arrives).

    Raises CommandNotStarted if the command cannot start. The caller finishes `echo`.
    """
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
    pump = _OutputPump(process.stdout, echo or OutputEcho(None))
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
    return Outcome(exit_code, duration, pump.tail(), status, pump.tests_ran())


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
    if sys.platform == "win32":  # mypy narrows platforms only on `sys.platform`
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
        usage=f"{PROG} [--kind KIND] [--min-tests N] [--cwd DIR] [--timeout SEC] [--full] -- <command> [args...]",
        description="Run a command and record its exit code in the Craftsman evidence ledger.",
    )
    parser.add_argument("--kind", choices=RUN_KINDS, default="other", help=", ".join(RUN_KINDS))
    parser.add_argument(
        "--min-tests",
        type=int,
        default=DEFAULT_MIN_TESTS,
        help=(
            "A test run must report at least this many tests to count as passing "
            f"(default {DEFAULT_MIN_TESTS}; 0 turns the check off). Applies to --kind test."
        ),
    )
    parser.add_argument("--cwd", help="Directory to run in (default: current)")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="Seconds (default 1800)"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Print all of the output as it arrives. By default the first "
            f"{LIVE_LIMIT_CHARS:,} characters print as they arrive; past them a line names a log file "
            "in the `craftsman review scratch` area (pruned when the review ends) that holds the full "
            "output and grows while the command runs, and when the command exits, the rest prints "
            f"if it is at most {TAIL_CHARS:,} characters, else a line with the number of characters "
            f"omitted and the last {TAIL_CHARS:,}. The ledger record is the same either way."
        ),
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
    if args.min_tests < 0:
        parser.error("--min-tests must be >= 0")
    try:
        return _run_recorded(
            command,
            Path(args.cwd or os.getcwd()).resolve(),
            args.kind,
            args.timeout,
            args.min_tests,
            args.full,
        )
    except (LedgerError, DiffScanError, GateStateError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 1


def _run_recorded(
    command: Sequence[str],
    cwd: Path,
    kind: RunKind,
    timeout_s: float,
    min_tests: int = DEFAULT_MIN_TESTS,
    full: bool = False,
) -> int:
    """Run `command` in `cwd` and record the run; the exit status to report to the caller."""
    ws = open_workspace(str(cwd))
    ledger = load(ws.ledger_path)
    echo = OutputEcho(None if full else ws.ledger_path.parent)
    with _TerminationSignals() as signals:
        try:
            code = _run_and_record(ws, ledger, echo, signals, command, cwd, kind, timeout_s, min_tests)
        except Terminated as exc:  # outside the command, e.g. before it started: nothing to record
            sys.stderr.write(f"{PROG}: {_signal_name(exc.signum)} received; the run was not recorded\n")
            return 128 + exc.signum
        finally:
            echo.finish()  # whatever the output still holds reaches stdout, even when recording fails
        return signals.exit_code(code)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _run_and_record(
    ws: Workspace,
    ledger: Ledger,
    echo: OutputEcho,
    signals: _TerminationSignals,
    command: Sequence[str],
    cwd: Path,
    kind: RunKind,
    timeout_s: float,
    min_tests: int,
) -> int:
    before, started_at = ws.snapshot(ledger), format_timestamp(datetime.now(timezone.utc))
    try:
        outcome = execute(command, cwd, timeout_s, echo)
    except CommandNotStarted as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return EXIT_NOT_STARTED
    finally:
        signals.defer()  # the command has ended: recording it is no longer interrupted
    echo.finish()  # the end of the output prints as soon as the command has ended
    if signals.signum is not None and outcome.status is RunStatus.INTERRUPTED:
        sys.stderr.write(f"{PROG}: {_signal_name(signals.signum)} received; the command was stopped\n")
    if outcome.status is RunStatus.TIMED_OUT:
        sys.stderr.write(f"{PROG}: timed out after {timeout_s:g}s; the command was killed\n")
    try:
        after = ws.snapshot(ledger)
    except KeyboardInterrupt:
        sys.stderr.write(f"{PROG}: interrupted; the run was not recorded\n")
        return EXIT_INTERRUPTED
    if modified := (after.fingerprint != before.fingerprint):
        _warn_modified(before, after)
    run = _persist_run(
        ws.ledger_path, command, cwd, kind, outcome, before, after, started_at, modified, min_tests
    )
    sys.stderr.write(f"{run.id} recorded ({run.kind}, exit {run.exit_code}, {run.duration_s:.1f}s)\n")
    if is_trivial_run(run.argv, run.kind):
        sys.stderr.write(
            f"{PROG}: warning: `{run.command}` is trivial and does not count as verification\n"
        )
    if run.too_few_tests:
        sys.stderr.write(
            f"{PROG}: warning: {run.tests_shortfall}; it does not count as a passing run. {MIN_TESTS_HINT}\n"
        )
    elif run.switched_off_test_minimum:
        sys.stderr.write(f"{PROG}: warning: {run.id} reported 0 tests and passes only because of --min-tests 0\n")
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
    min_tests: int,
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
            tests_ran=outcome.tests_ran if kind == "test" else None,
            min_tests=min_tests,
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
