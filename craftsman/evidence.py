"""Glue between the work tree and the evidence ledger, shared by the review CLI and the gate.

This module knows where evidence lives on disk (ledger, review packets, archives) and how to
evaluate it against the current diff. It deliberately knows nothing about the gate's state file
so that `craftsman.gate` can depend on it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from craftsman.diffscan import DiffSnapshot, resolve_base_sha, snapshot, worktree_root
from craftsman.ledger import (
    GatePolicy,
    GateReport,
    Ledger,
    LedgerError,
    evaluate,
    ledger_path_for,
    load,
    locked,
    replace_with_retry,
    uncovered_by_lens,
)
from craftsman.lenses import Tier, strictest_tier

PACKET_GLOB = "packet-*.md"


@dataclass(frozen=True)
class Evaluation:
    snap: DiffSnapshot
    ledger: Ledger
    report: GateReport


def evaluate_evidence(
    cwd: str | Path, base_ref: str | None, tier: Tier, policy_for: Callable[[Tier], GatePolicy]
) -> Evaluation:
    """Evaluate the ledger of the work tree containing `cwd` against its diff from `base_ref`.

    The policy is built for `tier` or, when the latest round was prepared for a stricter tier,
    for that one: a review that chose to go deeper is held to it.
    """
    root = worktree_root(cwd)
    ledger = load(ledger_path_for(root))
    snap = snapshot(root, resolve_base_sha(root, base_ref))
    policy = policy_for(effective_tier(ledger, tier))
    return Evaluation(snap, ledger, evaluate(ledger, snap, policy))


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
    while _archived_ledger_path(ledger_path, stamp).exists() or _archive_folder(ledger_path, stamp).exists():
        attempt += 1
        stamp = f"{base}-{attempt}"
    return stamp


def _archived_ledger_path(ledger_path: Path, stamp: str) -> Path:
    return ledger_path.with_name(f"{ledger_path.stem}-{stamp}{ledger_path.suffix}")


def _archive_folder(ledger_path: Path, stamp: str) -> Path:
    return ledger_path.parent / f"archive-{stamp}"


def ledger_overview(ledger: Ledger) -> dict[str, Any]:
    """Compact, JSON-ready summary of the ledger for status displays."""
    latest = ledger.rounds[-1] if ledger.rounds else None
    coverage: dict[str, Any] | None = None
    if latest is not None:
        missing = uncovered_by_lens(ledger, latest)
        open_hunks = sum(not hunk.carried for hunk in latest.hunks)
        total = open_hunks * len(latest.lenses)
        coverage = {"covered": total - sum(len(ids) for ids in missing.values()), "total": total}
    verdicts = {"none": 0, "proven": 0, "probable": 0, "question": 0, "dismissed": 0}
    for finding in ledger.findings:
        verdicts[finding.verdict or "none"] += 1
    return {
        "rounds": len(ledger.rounds),
        "tier": latest.tier if latest else None,
        "coverage": coverage,
        "findings": len(ledger.findings),
        "verdicts": verdicts,
        "resolved": sum(finding.resolution is not None for finding in ledger.findings),
        "latest_runs": [
            {"id": run.id, "kind": run.kind, "command": run.command, "exit_code": run.exit_code}
            for run in ledger.runs[-3:]
        ],
    }
