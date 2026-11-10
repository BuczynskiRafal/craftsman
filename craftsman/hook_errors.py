"""The hook error log: what a platform hook could not do, written where `review check` shows it.

A hook never crashes the session it serves (the platform treats a crashed hook as "allow"), so every
failure it survives would otherwise vanish: a hook that crashed, a transcript it could not read, a git
call that ran out of its budget, a ledger it could not load. Each one is appended as one JSON line to
`<git-dir>/craftsman/hook-errors.log`; `review check`, `review report` and `gate --complete` print
`summary_line` when the log holds entries newer than the latest round.

Every hook entry point wraps its work in `hook_run`: the log, the git budget (`diffscan.git_budget`) and the
notice of a ledger set aside all become active for the run, and any code underneath reports with `report` without
knowing where the log lives. The Antigravity entry points use the same wrapper.

The log is keyed by the Craftsman directory (`ledger_path.parent`), so every entry point (the Claude Code
and Copilot hooks, the review guard, and the Antigravity hooks) logs the same way without knowing the others.
It is bounded: past `MAX_LOG_BYTES` the file is moved to `hook-errors.log.1` (replacing the older one), so
the log keeps the last 512 KB at most. `record` never raises; a log that cannot be written is reported on
stderr, which is all a hook can still do.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

from craftsman.diffscan import DiffScanError, GitTimeoutError, git_budget
from craftsman.hook_budget import git_budget_s
from craftsman.hook_fast import named_git_dir
from craftsman.ledger import watch_set_asides
from craftsman.records import LEDGER_FILENAME, Ledger, LedgerError, format_timestamp, parse_timestamp

ErrorKind = Literal["crash", "transcript", "git-timeout", "git", "ledger", "state", "provenance", "await-user", "scope"]
ERROR_KINDS: tuple[ErrorKind, ...] = get_args(ErrorKind)
LOG_NAME = "hook-errors.log"
ROTATED_NAME = "hook-errors.log.1"
MAX_LOG_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 1000

# Reports one problem: (kind, message). Entry points hand it to code that must not know where the log lives.
ErrorSink = Callable[[ErrorKind, str], None]


@dataclass(frozen=True)
class HookError:
    at: str
    source: str
    kind: ErrorKind
    message: str


def log_path(craftsman_dir: Path) -> Path:
    return craftsman_dir / LOG_NAME


def craftsman_dir_for(cwd: str | Path) -> Path | None:
    """`<git-dir>/craftsman` of the checkout that contains `cwd`, read from its `.git` entry WITHOUT running git.

    A hook that ran out of its git budget still has to log that, so the log cannot depend on git. A `.git`
    directory and the `gitdir:` pointer file of a linked work tree are understood (`hook_fast.named_git_dir`,
    which the PreToolUse fast path reads them with too); None when there is none.
    """
    directory = Path(cwd)
    for candidate in (directory, *directory.parents):
        try:
            git_dir = named_git_dir(str(candidate))
        except (OSError, ValueError):
            return None
        if git_dir is not None:
            return Path(git_dir) / "craftsman"
    return None


def record(
    craftsman_dir: Path,
    kind: ErrorKind,
    message: str,
    *,
    source: str = "",
    now: datetime | None = None,
) -> bool:
    """Append one entry to the log in `craftsman_dir`; False (and a line on stderr) when it could not be written.

    `source` names the hook event or entry point (`pre-tool`, `subagent-stop`, `antigravity-stop`).
    """
    entry = HookError(
        format_timestamp(now or datetime.now(timezone.utc)),
        source,
        kind,
        " ".join(message.split())[:MAX_MESSAGE_CHARS],
    )
    line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
    path = log_path(craftsman_dir)
    try:
        if craftsman_dir.is_symlink():
            raise OSError(f"{craftsman_dir} is a symlink")
        craftsman_dir.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        # One write of one line in append mode: concurrent hooks do not interleave their lines.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        sys.stderr.write(f"craftsman hook error: could not write {path}: {exc}; the error was: {kind}: {entry.message}\n")
        return False
    return True


@dataclass
class HookLog:
    """Where the problems of one hook run go: the log in `craftsman_dir` (None: stderr only), as `source`."""

    craftsman_dir: Path | None
    source: str
    _git_timed_out: bool = False

    def record(self, kind: ErrorKind, message: str) -> None:
        if self.craftsman_dir is None:
            sys.stderr.write(f"craftsman hook error ({kind}): {message}\n")
            return
        record(self.craftsman_dir, kind, message, source=self.source)

    def git_timeout(self, message: str) -> None:
        """One entry per hook run: after the first timeout every later git call finds the budget spent."""
        if not self._git_timed_out:
            self._git_timed_out = True
            self.record("git-timeout", message)

    def set_aside(self, backup: Path) -> None:
        self.record("ledger", f"the review ledger of an older craftsman was set aside as {backup.name}")


_ACTIVE: ContextVar[HookLog | None] = ContextVar("craftsman_hook_log", default=None)


@contextmanager
def hook_run(event: str, cwd: str | Path | None) -> Iterator[HookLog]:
    """The frame of one hook run (`event`: a key of `hook_budget.HOOK_TIMEOUTS_S`) in the checkout containing `cwd`.

    Inside it `report` logs into that checkout's log, every git call shares the event's budget, and an older
    ledger that a read sets aside is logged. A git call that runs out of time is logged once, even when the code
    that called it catches the error.
    """
    log = HookLog(craftsman_dir_for(cwd or os.getcwd()), event)
    token = _ACTIVE.set(log)
    try:
        with git_budget(git_budget_s(event), on_timeout=log.git_timeout), watch_set_asides(log.set_aside):
            yield log
    finally:
        _ACTIVE.reset(token)


def kind_of(exc: Exception) -> ErrorKind:
    """The kind of log entry for a problem a hook survives: a git timeout, another git failure, a gate state,
    a ledger, else a crash."""
    from craftsman.gate import GateStateError  # gate prints the summary, so it imports this module

    if isinstance(exc, GitTimeoutError):
        return "git-timeout"
    if isinstance(exc, DiffScanError):
        return "git"
    if isinstance(exc, GateStateError):
        return "state"
    return "ledger" if isinstance(exc, LedgerError) else "crash"


def report(kind: ErrorKind, message: str, craftsman_dir: Path | None = None) -> None:
    """Log a problem a hook survives: into the running hook's log (`hook_run`), else into `craftsman_dir`, else stderr."""
    log = _ACTIVE.get()
    if log is None:
        log = HookLog(craftsman_dir, "hook")
    log.record(kind, message)


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_LOG_BYTES:
            return
    except FileNotFoundError:
        return
    os.replace(path, path.with_name(ROTATED_NAME))


def read(craftsman_dir: Path) -> tuple[HookError, ...]:
    """Every entry of the log (the rotated file first), oldest first; a line that cannot be read is skipped."""
    entries: list[HookError] = []
    for name in (ROTATED_NAME, LOG_NAME):
        try:
            text = (craftsman_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            entry = _parse(line)
            if entry is not None:
                entries.append(entry)
    return tuple(entries)


def _parse(line: str) -> HookError | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    at, source, kind, message = (raw.get(key) for key in ("at", "source", "kind", "message"))
    if not (isinstance(at, str) and isinstance(source, str) and isinstance(message, str) and kind in ERROR_KINDS):
        return None
    try:
        parse_timestamp(at)
    except ValueError:
        return None
    return HookError(at, source, kind, message)


def since(craftsman_dir: Path, moment: datetime) -> tuple[HookError, ...]:
    """The entries logged at or after `moment`."""
    return tuple(entry for entry in read(craftsman_dir) if parse_timestamp(entry.at) >= moment)


def since_latest_round(craftsman_dir: Path, ledger: Ledger) -> tuple[HookError, ...]:
    """The entries logged since the latest round was prepared (none without a round)."""
    if not ledger.rounds:
        return ()
    return since(craftsman_dir, parse_timestamp(ledger.rounds[-1].prepared_at))


def summary_line(craftsman_dir: Path, ledger: Ledger) -> str | None:
    """`Hook errors since round N was prepared: K (see <path>)`, None when the latest round has none (or there is no round)."""
    entries = since_latest_round(craftsman_dir, ledger)
    if not entries:
        return None
    return f"Hook errors since round {ledger.rounds[-1].number} was prepared: {len(entries)} (see {log_path(craftsman_dir)})"


def summary_for(ledger_path: Path, ledger: Ledger) -> str | None:
    """`summary_line` for the ledger stored at `ledger_path`."""
    return summary_line(ledger_path.parent, ledger)


def summary_json(ledger_path: Path, ledger: Ledger) -> dict[str, object] | None:
    """The same facts for `--json` output: `{"count", "since_round", "path"}`, None when there are no entries."""
    entries = since_latest_round(ledger_path.parent, ledger)
    if not entries:
        return None
    return {"count": len(entries), "since_round": ledger.rounds[-1].number, "path": str(log_path(ledger_path.parent))}


def summary_of(craftsman_dir: Path) -> str | None:
    """`summary_line` for the ledger in `craftsman_dir`; None when there is nothing to say or the ledger cannot be read."""
    from craftsman.ledger import load

    try:
        ledger = load(craftsman_dir / LEDGER_FILENAME, set_aside=False)
    except LedgerError:
        return None
    return summary_line(craftsman_dir, ledger)
