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
    FieldError,
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
from craftsman.scout import Scout, Target, parse_target
from craftsman.workspace import (
    open_workspace,
)
from craftsman.worktree_index import WorktreeIndex

SHOW_RECENT_RUNS = 5


def _parse_scout_targets(raw_targets: Sequence[str]) -> Target:
    if len(raw_targets) == 1:
        return parse_target(raw_targets[0])
    parsed = [parse_target(raw) for raw in raw_targets]
    if all(item.kind == "hunks" for item in parsed):
        return Target("hunks", ",".join(item.raw for item in parsed))
    raise FieldError("target", "multiple arguments are only supported for hunk ids (e.g. H3 H4 H5)")


def _parse_selected_finding_ids(raw_ids: str | None, positional_ids: Sequence[str]) -> list[str]:
    selected: list[str] = []
    if raw_ids is not None:
        parts = [part.strip() for part in raw_ids.split(",")]
        if not parts or any(not part for part in parts):
            raise LedgerError("pass comma-separated finding ids to --ids (e.g. F1,F2)")
        selected.extend(parts)
    for raw in positional_ids:
        cleaned = raw.strip()
        if not cleaned:
            raise LedgerError("finding id must not be blank")
        selected.append(cleaned)
    return list(dict.fromkeys(selected))


def handle_scout(args: argparse.Namespace) -> int:
    target = _parse_scout_targets(args.targets)
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
    hunks_mode = args.hunks is not None
    selected_ids = _parse_selected_finding_ids(args.ids, args.finding_ids)
    if args.packet_path and hunks_mode:
        raise LedgerError("pass either --packet-path or --hunks, not both")
    if (args.packet_path or hunks_mode) and selected_ids:
        raise LedgerError("finding ids cannot be combined with --packet-path or --hunks")
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    if selected_ids:
        by_id = {finding.id: finding for finding in ledger.findings}
        unknown = [finding_id for finding_id in selected_ids if finding_id not in by_id]
        if unknown:
            raise LedgerError(f"unknown finding id(s): {', '.join(unknown)}")
        selected = [by_id[finding_id] for finding_id in selected_ids]
        if args.json:
            print(json.dumps({"findings": [finding.to_json() for finding in selected]}, indent=2, ensure_ascii=False))
            return 0
        print("\n".join(findings_table(selected)))
        details = finding_details(selected)
        if details:
            print()
            print("\n".join(details))
        return 0
    snap = ws.snapshot(ledger)
    latest = ledger.rounds[-1] if ledger.rounds else None
    if args.packet_path:
        if latest is None:
            raise LedgerError("no review round yet; run `craftsman review prepare`")
        print(str(Path(latest.packet_path).resolve()))
        return 0
    slots = hunt_slots(ledger)
    if hunks_mode:
        only_ids: set[str] | None = None
        hunks_spec: str | None = None
        if args.hunks != "":
            if latest is None:
                raise LedgerError("no review round yet; run `craftsman review prepare`")
            only_ids = set(expand_hunk_spec(args.hunks, [hunk.id for hunk in latest.hunks]))
            hunks_spec = args.hunks.strip()
        return _show_hunks(
            ws.root,
            latest,
            snap,
            not_covered(ledger, slots),
            slots,
            as_json=args.json,
            only_ids=only_ids,
            hunks_spec=hunks_spec,
        )
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
    only_ids: set[str] | None = None,
    hunks_spec: str | None = None,
) -> int:
    if as_json:
        hunks_list = hunks_json(root, latest, snap, missing) if latest else []
        if only_ids is not None:
            hunks_list = [item for item in hunks_list if item["id"] in only_ids]
        payload = {
            "round": round_json(latest, snap) if latest else None,
            "stale": bool(latest is not None and latest.fingerprint != snap.fingerprint),
            "hunks": hunks_list,
            **({} if slots is None else {"slots": slots_json(slots)}),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
        return 0
    print("\n".join(hunk_map_lines(root, latest, snap, missing, slots, only_ids=only_ids)))
    if hunks_spec:
        print(f"Tip: run `craftsman review view --hunks {hunks_spec}` to inspect the diff body.")
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
