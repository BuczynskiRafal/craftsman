"""Glue between the work tree and the evidence ledger, shared by the review CLI and the gate.

This module knows where evidence lives on disk (ledger, review packets, archives) and how to
evaluate it against the current diff. It deliberately knows nothing about the gate's state file
so that `craftsman.gate` can depend on it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

from craftsman.diffscan import DiffSnapshot
from craftsman.files import replace_with_retry
from craftsman.gatecheck import GatePolicy, GateReport, evaluate
from craftsman.ledger import ledger_path_for, load, locked
from craftsman.records import IsolationEvidence, Ledger, LedgerError

PACKET_GLOB = "packet-*.md"
INBOX_DIRNAME = "inbox"
SCRATCH_DIRNAME = "scratch"
InboxKind = Literal["cover", "add", "verdict"]
INBOX_KINDS: tuple[InboxKind, ...] = get_args(InboxKind)
KEEP_ARCHIVES_ENV = "CRAFTSMAN_KEEP_ARCHIVES"
DEFAULT_KEEP_ARCHIVES = 5
_MAX_INBOX_ATTEMPTS = 16
_MAX_SCRATCH_ATTEMPTS = 16
_LINK_REPARSE_TAGS: frozenset[int] = frozenset({
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
})
_ROUND_PACKET_RE = re.compile(r"\Apacket-([0-9]+)(\..+)?\.md\Z")
_STAMP_PATTERN = r"([0-9]{8}T[0-9]{6}\.[0-9]{3}Z)(?:-([2-9]|[1-9][0-9]+))?"
_ARCHIVE_DIR_RE = re.compile(rf"\Aarchive-{_STAMP_PATTERN}\Z")
_ASCII_INT_RE = re.compile(r"\A[+-]?[0-9]+\Z")

ArchiveStampKey = tuple[str, int]


@dataclass(frozen=True)
class Evaluation:
    """The gate's verdict on a work tree: its diff, its ledger, and whether it applied `deep`."""

    snap: DiffSnapshot
    ledger: Ledger
    deep: bool
    report: GateReport


def evaluate_evidence(
    root: Path, snap: DiffSnapshot, deep: bool, isolation: IsolationEvidence
) -> Evaluation:
    """Evaluate the ledger of the work tree `root` against its diff `snap`.

    The gate is deep when `deep` or when the latest round was prepared deep: a review that chose
    to go deeper is held to it.
    """
    ledger = load(ledger_path_for(root))
    effective = effective_deep(ledger, deep)
    report = evaluate(ledger, snap, GatePolicy(deep=effective, isolation_evidence=isolation))
    return Evaluation(snap, ledger, effective, report)


def effective_deep(ledger: Ledger, *requested: bool) -> bool:
    """Whether any of `requested` or the latest review round is deep."""
    return any(requested) or any(round_.deep for round_ in ledger.rounds[-1:])


def packet_path(ledger_path: Path, round_number: int) -> Path:
    return ledger_path.parent / f"packet-{round_number}.md"


def _has_link_reparse_tag(st: os.stat_result) -> bool:
    return getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def _is_symlink_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or _has_link_reparse_tag(os.lstat(path))
    except OSError:
        return False


def _has_real_inode(st: os.stat_result) -> bool:
    return st.st_ino != 0 or st.st_dev != 0


def _warn_inbox(detail: str) -> None:
    sys.stderr.write(f"craftsman: warning: {detail}\n")


def _warn_scratch(detail: str) -> None:
    sys.stderr.write(f"craftsman: warning: {detail}\n")


def _prepare_inbox_dir(craftsman_dir: Path) -> Path:
    _require_real_folder(craftsman_dir, action="use")
    inbox_dir = craftsman_dir / INBOX_DIRNAME
    _require_real_folder(inbox_dir, action="use")
    try:
        craftsman_dir.mkdir(parents=True, exist_ok=True)
        inbox_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise LedgerError(f"cannot create inbox directory {inbox_dir}: {exc}") from exc
    return inbox_dir


def create_inbox_file(craftsman_dir: Path, kind: InboxKind) -> Path:
    """Create and return a unique empty JSON batch file inside `craftsman_dir/inbox`."""
    if kind not in INBOX_KINDS:
        raise LedgerError(f"unknown inbox kind {kind!r}; expected one of {', '.join(INBOX_KINDS)}")
    pid = os.getpid()
    for attempt in range(_MAX_INBOX_ATTEMPTS):
        inbox_dir = _prepare_inbox_dir(craftsman_dir)
        candidate = inbox_dir / f"{kind}-{pid}-{secrets.token_hex(4)}.json"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            retryable = isinstance(exc, (FileExistsError, FileNotFoundError))
            if retryable and attempt + 1 < _MAX_INBOX_ATTEMPTS:
                continue
            raise LedgerError(f"cannot create inbox file in {inbox_dir}: {exc}") from exc
        os.close(fd)
        return candidate
    raise LedgerError(f"cannot create inbox file in {craftsman_dir / INBOX_DIRNAME}")


def _is_consumable_inbox_entry(craftsman_dir: Path, inbox_dir: Path, candidate: Path) -> bool:
    for item in (craftsman_dir, inbox_dir, candidate, candidate.parent, candidate.parent.parent):
        if _is_symlink_or_junction(item):
            return False
    try:
        resolved_inbox = craftsman_dir.resolve() / INBOX_DIRNAME
        if inbox_dir.resolve() != resolved_inbox or candidate.parent.resolve() != resolved_inbox:
            return False
        parent_stat, inbox_stat = os.lstat(candidate.parent), os.lstat(inbox_dir)
        grand_stat, craftsman_stat = os.lstat(candidate.parent.parent), os.lstat(craftsman_dir)
        if _has_real_inode(inbox_stat) and not os.path.samestat(parent_stat, inbox_stat):
            return False
        if _has_real_inode(craftsman_stat) and not os.path.samestat(grand_stat, craftsman_stat):
            return False
        cand_stat = os.lstat(candidate)
    except OSError:
        return False
    return stat.S_ISREG(cand_stat.st_mode) and not _has_link_reparse_tag(cand_stat)


def consume_inbox_file(craftsman_dir: Path, source: str) -> bool:
    """Delete `source` if and only if it is a regular non-symlink file inside `craftsman_dir/inbox`."""
    if not source or source == "-":
        return False
    full_craftsman = craftsman_dir if craftsman_dir.is_absolute() else Path.cwd() / craftsman_dir
    inbox_dir = full_craftsman / INBOX_DIRNAME
    candidate = Path(source)
    full_candidate = candidate if candidate.is_absolute() else Path.cwd() / candidate
    if not _is_consumable_inbox_entry(full_craftsman, inbox_dir, full_candidate):
        return False
    try:
        candidate.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        _warn_inbox(f"could not remove inbox file {candidate}: {exc}")
        return False
    with suppress(OSError):
        inbox_dir.rmdir()
    return True


def prune_inbox_files(craftsman_dir: Path) -> tuple[Path, ...]:
    """Delete leftover batch JSON files from `craftsman_dir/inbox` and remove the folder if empty."""
    if _is_symlink_or_junction(craftsman_dir):
        _warn_inbox(f"could not prune symlinked directory {craftsman_dir}")
        return ()
    inbox_dir = craftsman_dir / INBOX_DIRNAME
    if _is_symlink_or_junction(inbox_dir):
        _warn_inbox(f"could not prune symlinked inbox {inbox_dir}")
        return ()
    try:
        if not inbox_dir.exists():
            return ()
        if not inbox_dir.is_dir():
            _warn_inbox(f"could not prune inbox {inbox_dir}: not a directory")
            return ()
        entries = sorted(inbox_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        _warn_inbox(f"could not prune inbox {inbox_dir}: {exc}")
        return ()
    pruned: list[Path] = []
    for entry in entries:
        try:
            if _is_symlink_or_junction(entry) or not entry.is_file():
                continue
            entry.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _warn_inbox(f"could not remove inbox file {entry}: {exc}")
            continue
        pruned.append(entry)
    with suppress(OSError):
        inbox_dir.rmdir()
    if pruned:
        names = ", ".join(item.name for item in pruned)
        sys.stderr.write(f"Pruned {len(pruned)} inbox file(s): {names}\n")
    return tuple(pruned)


def scratch_dir_for(craftsman_dir: Path) -> Path:
    return craftsman_dir / SCRATCH_DIRNAME


def _prepare_scratch_dir(craftsman_dir: Path) -> Path:
    _require_real_folder(craftsman_dir, action="use")
    scratch_dir = scratch_dir_for(craftsman_dir)
    _require_real_folder(scratch_dir, action="use")
    try:
        craftsman_dir.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise LedgerError(f"cannot create scratch directory {scratch_dir}: {exc}") from exc
    return scratch_dir


def create_scratch_dir(craftsman_dir: Path) -> Path:
    """Create a unique managed scratch directory under `craftsman_dir/scratch/<id>/`."""
    scratch_dir = _prepare_scratch_dir(craftsman_dir)
    for _ in range(_MAX_SCRATCH_ATTEMPTS):
        token = secrets.token_hex(6)
        candidate = scratch_dir / token
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            raise LedgerError(f"cannot create scratch directory in {scratch_dir}: {exc}") from exc
    raise LedgerError(f"cannot create scratch directory in {scratch_dir}")


def _make_writable(path: Path) -> None:
    """Let the owner delete `path`: rwx on a directory, read-write on a file; links stay untouched.

    A symlink or junction is never followed: chmod on one would change its target, which can be
    anything outside the scratch tree (a venv links its interpreter, for example).
    """
    if _is_symlink_or_junction(path):
        return
    with suppress(OSError):
        os.chmod(path, stat.S_IRWXU if path.is_dir() else stat.S_IREAD | stat.S_IWRITE)


def _make_tree_writable(root: Path) -> None:
    """Make every real file and directory under `root` deletable before removing the tree.

    A read-only directory blocks deleting its entries on every platform, and a read-only file
    blocks deleting itself on Windows.
    """
    _make_writable(root)
    for dirpath, dirnames, filenames in os.walk(root):
        # os.walk descends into a Windows junction, which it does not see as a link.
        dirnames[:] = [name for name in dirnames if not _is_symlink_or_junction(Path(dirpath, name))]
        for name in (*dirnames, *filenames):
            _make_writable(Path(dirpath, name))


# The `shutil.rmtree` calls a failure can be retried with; it also reports `os.open`, `os.scandir`,
# `os.lstat` and others, which take other arguments or remove nothing.
_RETRYABLE_REMOVALS = (os.unlink, os.remove, os.rmdir)


def _retry_writable(func: Callable[..., Any], path: str, error: Any) -> None:
    """`shutil.rmtree` error handler: make the entry and its directory writable, then retry once.

    `error` is the exception (`onexc`) or its `sys.exc_info()` (`onerror`). A failure that is not a
    removal, or a second failure, propagates out of `rmtree`, so the caller reports the entry as
    not removed.
    """
    if func not in _RETRYABLE_REMOVALS:
        raise error if isinstance(error, BaseException) else error[1]
    target = Path(path)
    _make_writable(target.parent)
    _make_writable(target)
    func(path)


def prune_scratch_dirs(craftsman_dir: Path) -> tuple[Path, ...]:
    """Delete all scratch directories under `craftsman_dir/scratch` and remove the folder if empty."""
    if _is_symlink_or_junction(craftsman_dir):
        _warn_scratch(f"could not prune symlinked directory {craftsman_dir}")
        return ()
    scratch_dir = craftsman_dir / SCRATCH_DIRNAME
    if _is_symlink_or_junction(scratch_dir):
        _warn_scratch(f"could not prune symlinked scratch directory {scratch_dir}")
        return ()
    try:
        if not scratch_dir.exists():
            return ()
        if not scratch_dir.is_dir():
            _warn_scratch(f"could not prune scratch {scratch_dir}: not a directory")
            return ()
        entries = sorted(scratch_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        _warn_scratch(f"could not prune scratch {scratch_dir}: {exc}")
        return ()
    pruned: list[Path] = []
    for entry in entries:
        try:
            if _is_symlink_or_junction(entry):
                os.unlink(entry)
            elif entry.is_dir():
                _make_tree_writable(entry)
                if sys.version_info >= (3, 12):
                    shutil.rmtree(entry, onexc=_retry_writable)
                else:
                    shutil.rmtree(entry, onerror=_retry_writable)
            else:
                _make_writable(entry)
                entry.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _warn_scratch(f"could not remove scratch entry {entry}: {exc}")
            continue
        pruned.append(entry)
    with suppress(OSError):
        scratch_dir.rmdir()
    return tuple(pruned)


def prune_all_temp_dirs(craftsman_dir: Path) -> tuple[Path, ...]:
    """Delete leftover inbox files and scratch directories under `craftsman_dir`."""
    return (*prune_inbox_files(craftsman_dir), *prune_scratch_dirs(craftsman_dir))


def prune_round_files(craftsman_dir: Path, round_number: int) -> tuple[Path, ...]:
    """Delete packet views of earlier rounds and full packets older than the previous round."""
    if isinstance(round_number, bool) or round_number < 1:
        raise LedgerError(f"round_number must be an integer >= 1 (got {round_number!r})")
    _require_real_folder(craftsman_dir)
    if not craftsman_dir.exists():
        return ()
    pruned: list[Path] = []
    try:
        entries = sorted(craftsman_dir.iterdir(), key=lambda item: item.name)
        for entry in entries:
            if not _should_prune_packet(entry, round_number):
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                continue
            _report_pruned(entry)
            pruned.append(entry)
    except OSError as exc:
        raise LedgerError(f"cannot prune packets in {craftsman_dir}: {exc}") from exc
    return tuple(pruned)


def _require_real_folder(folder: Path, *, action: str = "prune") -> None:
    if _is_symlink_or_junction(folder):
        raise LedgerError(f"refusing to {action} symlinked directory {folder}")
    if folder.exists() and not folder.is_dir():
        raise LedgerError(f"cannot {action} {folder}: not a directory")


def _should_prune_packet(entry: Path, round_number: int) -> bool:
    match = _ROUND_PACKET_RE.match(entry.name)
    if match is None or entry.is_symlink() or not entry.is_file():
        return False
    number = int(match.group(1))
    cutoff = round_number if match.group(2) is not None else round_number - 1
    return number < cutoff


def archive_ledger(ledger_path: Path) -> tuple[Path, ...]:
    """Move the ledger and its review packets aside so the next review starts clean."""
    with locked(ledger_path):
        return _archive(ledger_path)


def archive_ledger_if_rebased(cwd: str | Path, base_sha: str) -> tuple[Path, ...]:
    """Archive the ledger when its review started from a different base commit than `base_sha`."""
    path = ledger_path_for(cwd)
    with locked(path):
        ledger = load(path)
        if not ledger.rounds or ledger.rounds[0].base_sha == base_sha:
            return ()
        return _archive(path)


def _archive(ledger_path: Path) -> tuple[Path, ...]:
    """Archive for a caller that holds the ledger lock."""
    keep = _keep_archives_count()
    stamp = _unused_stamp(ledger_path)
    archived: list[Path] = []
    try:
        if ledger_path.exists():
            target = _archived_ledger_path(ledger_path, stamp)
            replace_with_retry(ledger_path, target)
            archived.append(target)
        packets = [
            path
            for path in sorted(ledger_path.parent.glob(PACKET_GLOB))
            if path.is_file() and not path.is_symlink()
        ]
        if packets:
            folder = _archive_folder(ledger_path, stamp)
            folder.mkdir()
            for packet in packets:
                replace_with_retry(packet, folder / packet.name)
            archived.append(folder)
        _prune_archives(ledger_path, keep)
    except OSError as exc:
        raise LedgerError(f"cannot archive {ledger_path}: {exc}") from exc
    prune_all_temp_dirs(ledger_path.parent)
    return tuple(archived)


def _keep_archives_count() -> int:
    """How many recent archives `_archive` retains, from `CRAFTSMAN_KEEP_ARCHIVES`."""
    raw = os.environ.get(KEEP_ARCHIVES_ENV)
    if raw is None:
        return DEFAULT_KEEP_ARCHIVES
    stripped = raw.strip()
    if stripped and _ASCII_INT_RE.match(stripped):
        keep = int(stripped)
        if keep >= 1:
            return keep
    raise LedgerError(
        f"{KEEP_ARCHIVES_ENV} must be an integer >= 1 (got {raw!r}); "
        f"unset it to keep the default {DEFAULT_KEEP_ARCHIVES} archives."
    )


def _prune_archives(ledger_path: Path, keep: int) -> None:
    """Keep only the newest `keep` timestamped archive folders and archived ledgers."""
    craftsman_dir = ledger_path.parent
    _require_real_folder(craftsman_dir)
    if not craftsman_dir.exists():
        return
    ledger_re = _archived_ledger_re(ledger_path)
    folders: list[tuple[ArchiveStampKey, Path]] = []
    ledgers: list[tuple[ArchiveStampKey, Path]] = []
    for entry in craftsman_dir.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir() and (key := _stamp_key(_ARCHIVE_DIR_RE, entry.name)) is not None:
            folders.append((key, entry))
        elif entry.is_file() and (key := _stamp_key(ledger_re, entry.name)) is not None:
            ledgers.append((key, entry))
    for _, folder in sorted(folders, key=lambda item: item[0])[:-keep]:
        _remove_archive_dir(folder)
        _report_pruned(folder)
    for _, archived_ledger in sorted(ledgers, key=lambda item: item[0])[:-keep]:
        try:
            archived_ledger.unlink()
        except FileNotFoundError:
            continue
        _report_pruned(archived_ledger)


def _archived_ledger_re(ledger_path: Path) -> re.Pattern[str]:
    stem = re.escape(ledger_path.stem)
    suffix = re.escape(ledger_path.suffix)
    return re.compile(rf"\A{stem}-{_STAMP_PATTERN}{suffix}\Z")


def _stamp_key(pattern: re.Pattern[str], name: str) -> ArchiveStampKey | None:
    match = pattern.match(name)
    if match is None:
        return None
    attempt = int(match.group(2)) if match.group(2) is not None else 1
    return match.group(1), attempt


def _remove_archive_dir(folder: Path) -> None:
    """Remove an `archive-<stamp>` folder after unlinking the flat files `_archive` placed in it."""
    children = sorted(folder.iterdir(), key=lambda item: item.name)
    for child in children:
        if child.is_dir() and not child.is_symlink():
            raise LedgerError(
                f"refusing to prune {folder}: unexpected subdirectory {child.name!r}"
            )
    for child in children:
        child.unlink(missing_ok=True)
    folder.rmdir()


def _report_pruned(path: Path) -> None:
    """Report a removed packet or archive on stderr so hook JSON on stdout stays intact."""
    sys.stderr.write(f"Pruned: {path.name}\n")


def _archive_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"


def _unused_stamp(ledger_path: Path) -> str:
    """A UTC timestamp strictly newer than any existing archive for the same millisecond."""
    base = _archive_stamp()
    attempt = _max_attempt_for_base(ledger_path, base) + 1
    stamp = base if attempt == 1 else f"{base}-{attempt}"
    while (
        _archived_ledger_path(ledger_path, stamp).exists()
        or _archive_folder(ledger_path, stamp).exists()
    ):
        attempt += 1
        stamp = f"{base}-{attempt}"
    return stamp


def _max_attempt_for_base(ledger_path: Path, base: str) -> int:
    craftsman_dir = ledger_path.parent
    if not craftsman_dir.is_dir():
        return 0
    ledger_re = _archived_ledger_re(ledger_path)
    highest = 0
    for entry in craftsman_dir.iterdir():
        for pattern in (_ARCHIVE_DIR_RE, ledger_re):
            key = _stamp_key(pattern, entry.name)
            if key is not None and key[0] == base and key[1] > highest:
                highest = key[1]
    return highest


def _archived_ledger_path(ledger_path: Path, stamp: str) -> Path:
    return ledger_path.with_name(f"{ledger_path.stem}-{stamp}{ledger_path.suffix}")


def _archive_folder(ledger_path: Path, stamp: str) -> Path:
    return ledger_path.parent / f"archive-{stamp}"
