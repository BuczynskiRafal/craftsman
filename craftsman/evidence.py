"""Glue between the work tree and the evidence ledger, shared by the review CLI and the gate.

This module knows where evidence lives on disk (ledger, review packets, archives) and how to
evaluate it against the current diff. It deliberately knows nothing about the gate's state file
so that `craftsman.gate` can depend on it.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from craftsman.diffscan import DiffSnapshot
from craftsman.files import replace_with_retry
from craftsman.gatecheck import GatePolicy, GateReport, evaluate
from craftsman.ledger import ledger_path_for, load, locked
from craftsman.lenses import Tier, strictest_tier
from craftsman.records import IsolationEvidence, Ledger, LedgerError

PACKET_GLOB = "packet-*.md"
KEEP_ARCHIVES_ENV = "CRAFTSMAN_KEEP_ARCHIVES"
DEFAULT_KEEP_ARCHIVES = 5
_ROUND_PACKET_RE = re.compile(r"\Apacket-([0-9]+)(\..+)?\.md\Z")
_STAMP_PATTERN = r"([0-9]{8}T[0-9]{6}\.[0-9]{3}Z)(?:-([2-9]|[1-9][0-9]+))?"
_ARCHIVE_DIR_RE = re.compile(rf"\Aarchive-{_STAMP_PATTERN}\Z")
_ASCII_INT_RE = re.compile(r"\A[+-]?[0-9]+\Z")

ArchiveStampKey = tuple[str, int]


@dataclass(frozen=True)
class Evaluation:
    """The gate's verdict on a work tree: its diff, its ledger, and the tier the gate applied."""

    snap: DiffSnapshot
    ledger: Ledger
    tier: Tier
    report: GateReport


def evaluate_evidence(
    root: Path, snap: DiffSnapshot, tier: Tier, isolation: IsolationEvidence
) -> Evaluation:
    """Evaluate the ledger of the work tree `root` against its diff `snap`.

    The gate applies `tier` or, when the latest round was prepared for a stricter tier, that one:
    a review that chose to go deeper is held to it.
    """
    ledger = load(ledger_path_for(root))
    strictest = effective_tier(ledger, tier)
    report = evaluate(ledger, snap, GatePolicy(tier=strictest, isolation_evidence=isolation))
    return Evaluation(snap, ledger, strictest, report)


def effective_tier(ledger: Ledger, tier: Tier, *others: Tier) -> Tier:
    """The strictest of the given tiers and the tier of the latest review round."""
    return strictest_tier(tier, *others, *(round_.tier for round_ in ledger.rounds[-1:]))


def packet_path(ledger_path: Path, round_number: int) -> Path:
    return ledger_path.parent / f"packet-{round_number}.md"


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


def _require_real_folder(folder: Path) -> None:
    if folder.is_symlink():
        raise LedgerError(f"refusing to prune symlinked directory {folder}")
    if folder.exists() and not folder.is_dir():
        raise LedgerError(f"cannot prune {folder}: not a directory")


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
