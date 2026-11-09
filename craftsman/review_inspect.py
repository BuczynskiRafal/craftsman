"""The `craftsman review` commands that read the ledger or tidy the checkout: show, scout, inbox, scratch, reset."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from craftsman.diffscan import (
    DiffSnapshot,
)
from craftsman.evidence import (
    archive_ledger,
    create_inbox_file,
    create_scratch_dir,
    prune_all_temp_dirs,
)
from craftsman.gatecheck import (
    SlotStatus,
    hunt_slots,
    require_current_round,
)
from craftsman.ledger import (
    load,
)
from craftsman.packet import (
    expand_hunk_spec,
)
from craftsman.records import (
    LedgerError,
    Round,
)
from craftsman.review_present import (
    coverage_json,
    finding_details,
    findings_table,
    hunk_map_lines,
    hunks_json,
    not_covered,
    progress_lines,
    round_json,
    round_line,
    run_lines,
    slots_json,
)
from craftsman.scout import Scout, parse_target
from craftsman.workspace import (
    open_workspace,
)
from craftsman.worktree_index import WorktreeIndex

SHOW_RECENT_RUNS = 5


def handle_scout(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    if target.kind == "hunks":
        snap = ws.snapshot(ledger)
        round_ = require_current_round(ledger, snap)
        wanted = set(expand_hunk_spec(target.raw, [hunk.id for hunk in round_.hunks]))
        scout = Scout(WorktreeIndex(ws.root), snap.base_sha)
        print(scout.hunks([hunk for hunk in snap.hunks if hunk.id in wanted]))
        return 0
    scout = Scout(WorktreeIndex(ws.root), ws.base_sha(ledger, args.base))
    print(scout.location(target.raw, target.line) if target.kind == "location" else scout.symbol(target.raw))
    return 0


def handle_inbox(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    print(create_inbox_file(ws.ledger_path.parent, args.kind).resolve())
    return 0


def handle_scratch(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    print(create_scratch_dir(ws.ledger_path.parent).resolve())
    return 0


def handle_show(args: argparse.Namespace) -> int:
    if args.packet_path and args.hunks:
        raise LedgerError("pass either --packet-path or --hunks, not both")
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    snap = ws.snapshot(ledger)
    latest = ledger.rounds[-1] if ledger.rounds else None
    if args.packet_path:
        if latest is None:
            raise LedgerError("no review round yet; run `craftsman review prepare`")
        print(str(Path(latest.packet_path).resolve()))
        return 0
    slots = hunt_slots(ledger)
    if args.hunks:
        return _show_hunks(ws.root, latest, snap, not_covered(ledger, slots), slots, as_json=args.json)
    if args.json:
        payload = {
            "round": round_json(latest, snap) if latest else None,
            "coverage": coverage_json(ledger, latest, slots) if latest else {},
            "hunks": hunks_json(ws.root, latest, snap, not_covered(ledger, slots)) if latest else [],
            **({} if slots is None else {"slots": slots_json(slots)}),
            "findings": [finding.to_json() for finding in ledger.findings],
            "runs": [run.to_json() for run in ledger.runs[-SHOW_RECENT_RUNS:]],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
    else:
        print(round_line(latest, snap))
        print(f"Packet: {Path(latest.packet_path).resolve()}")
        print()
        print("\n".join(progress_lines(ledger, latest, slots)))
    print()
    print("\n".join(findings_table(ledger.findings)))
    details = finding_details(ledger.findings)
    if details:
        print()
        print("\n".join(details))
    print()
    print("\n".join(run_lines(ledger.runs[-SHOW_RECENT_RUNS:], snap)))
    return 0


def _show_hunks(
    root: Path,
    latest: Round | None,
    snap: DiffSnapshot,
    missing: Sequence[str],
    slots: Sequence[SlotStatus] | None,
    *,
    as_json: bool,
) -> int:
    if as_json:
        payload = {
            "round": round_json(latest, snap) if latest else None,
            "stale": bool(latest is not None and latest.fingerprint != snap.fingerprint),
            "hunks": hunks_json(root, latest, snap, missing) if latest else [],
            **({} if slots is None else {"slots": slots_json(slots)}),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
        return 0
    print("\n".join(hunk_map_lines(root, latest, snap, missing, slots)))
    return 0


def handle_reset(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    archived = archive_ledger(ws.ledger_path)
    prune_all_temp_dirs(ws.ledger_path.parent)
    if not archived:
        print("Nothing to archive: no ledger or review packets yet.")
    for path in archived:
        print(f"Archived to {path}")
    return 0
