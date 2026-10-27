"""Glue between the work tree and the evidence ledger, shared by the review CLI and the gate.

This module knows where evidence lives on disk (ledger, review packets, archives) and how to
evaluate it against the current diff. It deliberately knows nothing about the gate's state file
so that `craftsman.gate` can depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from craftsman.diffscan import DiffSnapshot
from craftsman.gatecheck import GatePolicy, GateReport, evaluate
from craftsman.ledger import ledger_path_for, load, locked, replace_with_retry
from craftsman.lenses import Tier, strictest_tier
from craftsman.records import IsolationEvidence, Ledger, LedgerError

PACKET_GLOB = "packet-*.md"


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
    stamp = _unused_stamp(ledger_path)
    archived: list[Path] = []
    try:
        if ledger_path.exists():
            target = _archived_ledger_path(ledger_path, stamp)
            replace_with_retry(ledger_path, target)
            archived.append(target)
        packets = sorted(ledger_path.parent.glob(PACKET_GLOB))
        if packets:
            folder = _archive_folder(ledger_path, stamp)
            folder.mkdir()
            for packet in packets:
                replace_with_retry(packet, folder / packet.name)
            archived.append(folder)
    except OSError as exc:
        raise LedgerError(f"cannot archive {ledger_path}: {exc}") from exc
    return tuple(archived)


def _archive_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"


def _unused_stamp(ledger_path: Path) -> str:
    """A UTC timestamp that names neither an existing archived ledger nor an archive folder."""
    base = stamp = _archive_stamp()
    attempt = 1
    while (
        _archived_ledger_path(ledger_path, stamp).exists()
        or _archive_folder(ledger_path, stamp).exists()
    ):
        attempt += 1
        stamp = f"{base}-{attempt}"
    return stamp


def _archived_ledger_path(ledger_path: Path, stamp: str) -> Path:
    return ledger_path.with_name(f"{ledger_path.stem}-{stamp}{ledger_path.suffix}")


def _archive_folder(ledger_path: Path, stamp: str) -> Path:
    return ledger_path.parent / f"archive-{stamp}"
