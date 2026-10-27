"""`craftsman review`: prepare review rounds and record or inspect evidence in the ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from craftsman.diffscan import (
    IGNORE_FILE,
    DiffScanError,
    DiffSnapshot,
    WorktreeIndex,
    detect_signals,
    resolve_base_sha,
    snapshot,
    worktree_root,
)
from craftsman.evidence import archive_ledger, effective_tier, packet_path
from craftsman.gate import (
    gate_policy,
    load_state,
    resolve_cli_state_file,
    resolve_effective_tier,
)
from craftsman.ledger import (
    DESIGN_LENS,
    RESOLUTIONS,
    SEVERITIES,
    SWEEP_LENS,
    VERDICTS,
    FieldError,
    Finding,
    GateReport,
    Ledger,
    LedgerError,
    Round,
    Run,
    SignalAck,
    _Fields,
    _next_id,
    ack_signal,
    add_coverage,
    add_finding,
    evaluate,
    is_trivial_run,
    ledger_path_for,
    load,
    merge_findings,
    mutate,
    open_round,
    relocate_finding,
    render_report,
    require_current_round,
    resolve,
    set_verdict,
    uncovered_by_lens,
    waive_run,
)
from craftsman.lenses import LENS_IDS, TIERS, Tier, select_lenses
from craftsman.packet import collect_context, render_packet
from craftsman.quality import collect_quality_signals

PROG = "craftsman review"
SHOW_RECENT_RUNS = 5
CITED_LINE_CHARS = 120
# Ledger record fields that come straight from a command-line flag, for error messages.
FIELD_FLAGS = {
    "lens": "--lens",
    "path": "--file",
    "line": "--line",
    "severity": "--severity",
    "title": "--title",
    "scenario": "--scenario",
    "signal_id": "--signal",
    "note": "--note",
    "digests": "--hunks",
    "verdict": "--as",
    "verdict_evidence": "--evidence",
    "verdict_run": "--run",
    "resolution": "--as",
    "resolution_note": "--note",
    "reason": "--reason",
}
JSON_FIELD_NAMES = {
    "path": "file",
    "digests": "hunks",
    "verdict": "as",
    "verdict_evidence": "evidence",
    "verdict_run": "run",
    "signal_id": "signal",
}

S = TypeVar("S", bound=str)


@dataclass(frozen=True)
class Workspace:
    root: Path
    state: dict[str, Any]  # the active craftsman state, or {} outside an active session
    ledger_path: Path


def open_workspace(cwd: str | None) -> Workspace:
    """Locate the work tree, the active gate state and the ledger for `cwd`."""
    root = worktree_root(cwd or os.getcwd())
    loaded = load_state(resolve_cli_state_file(None, str(root)))
    state = loaded if loaded and loaded.get("active") else {}
    return Workspace(root=root, state=state, ledger_path=ledger_path_for(root))


def resolve_base(ws: Workspace, ledger: Ledger, explicit: str | None) -> str:
    """`--base`, else the latest round's base, else the active task's base, else HEAD."""
    state_base = ws.state.get("base_sha")
    ledger_base = ledger.rounds[-1].base_sha if ledger.rounds else None
    ref = explicit or ledger_base or (state_base if isinstance(state_base, str) else None)
    return resolve_base_sha(ws.root, ref)


def resolve_tier(ws: Workspace, ledger: Ledger, explicit: Tier | None) -> Tier:
    """`--tier`, else the active task's effective gear, else the latest round's, else standard."""
    if explicit is not None:
        return explicit
    if ws.state:
        return resolve_effective_tier(ws.state, str(ws.root))
    return ledger.rounds[-1].tier if ledger.rounds else "standard"


def evaluation_tier(ws: Workspace, ledger: Ledger, explicit: Tier | None) -> Tier:
    """The strictest of `--tier`, the active task's gear and the latest round's tier.

    An explicit `--tier` can raise the bar for `check`, never lower it.
    """
    requested = [explicit] if explicit is not None else []
    if ws.state:
        requested.append(resolve_effective_tier(ws.state, str(ws.root)))
    if not requested:
        return ledger.rounds[-1].tier if ledger.rounds else "standard"
    return effective_tier(ledger, *requested)


# --- subcommands ----------------------------------------------------------------------------


def _prepare(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    tier = resolve_tier(ws, ledger, args.tier)
    snap = snapshot(ws.root, resolve_base(ws, ledger, args.base))
    if snap.is_empty:
        print(f"Nothing to review: the work tree matches base {snap.base_sha[:12]}.")
        return 0
    picks = select_lenses(tier, detect_signals(snap.hunks), docs_only=snap.docs_only)
    lens_ids = [pick.lens.id for pick in picks]
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    quality_signals = collect_quality_signals(ws.root, snap)

    def open_next(current: Ledger) -> tuple[Ledger, tuple[Ledger, Round, bool]]:
        packet = packet_path(ws.ledger_path, len(current.rounds) + 1)
        updated, round_ = open_round(
            current, snap, tier, lens_ids, str(packet), symbols, signals=quality_signals
        )
        return updated, (updated, round_, updated is not current)

    ledger, round_, created = mutate(ws.ledger_path, open_next)
    packet = Path(round_.packet_path)
    if created or not packet.is_file():
        task = args.task or str(ws.state.get("task") or "")
        ctx = collect_context(
            ws.root,
            snap,
            round_,
            picks,
            task,
            symbols=symbols,
            ledger=ledger,
            index=index,
            quality_signals=quality_signals,
        )
        _write_text(packet, render_packet(ctx))
    _print_prepare_summary(round_, packet, created)
    return 0


def _print_prepare_summary(round_: Round, packet: Path, created: bool) -> None:
    if not created:
        print(f"Round {round_.number} unchanged (same diff and tier). Packet: {packet}")
        return
    carried = sum(hunk.carried for hunk in round_.hunks)
    print(
        f"Round {round_.number} prepared ({round_.tier}): {len(round_.hunks)} hunks, "
        f"{carried} carried, {len(round_.hunks) - carried} to review."
    )
    print(f"Lenses: {', '.join(round_.lenses)}")
    print(f"Packet: {packet}")
    sweep_last = "; run the `sweep` lens last" if SWEEP_LENS in round_.lenses else ""
    print(
        "Next: read the packet, apply every lens to each non-carried hunk, and record "
        f"`craftsman review add` / `craftsman review cover`{sweep_last}."
    )


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc


def _load_json_entries(source: str) -> list[Any]:
    """Read and parse a non-empty JSON list of entry objects from `source` (`-` for stdin)."""
    label = "stdin" if source == "-" else source
    try:
        if source == "-":
            buf = getattr(sys.stdin, "buffer", None)
            text = buf.read().decode("utf-8") if buf is not None else sys.stdin.read()
        else:
            text = Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"cannot read {label}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"invalid JSON in {label}: {exc}") from exc
    if not isinstance(raw, list):
        raise LedgerError(f"--from-json expects a JSON list of objects (got {type(raw).__name__})")
    if not raw:
        raise LedgerError("--from-json list must not be empty")
    return raw


def _batch_error(index: int, label: str | None, exc: LedgerError) -> LedgerError:
    head = f"entry {index} ({label})" if label else f"entry {index}"
    field = JSON_FIELD_NAMES.get(exc.field, exc.field)
    if isinstance(exc, FieldError):
        detail = f"{field}: {exc.problem}" if field else exc.problem
        return LedgerError(f"{head}: {detail}")
    if field:
        return LedgerError(f"{head}: {field}: {exc}")
    return LedgerError(f"{head}: {exc}")


def _cover_from_json(args: argparse.Namespace) -> int:
    if any(val is not None for val in (args.lens, args.hunks, args.files, args.note)):
        raise LedgerError("--from-json cannot be combined with --lens, --hunks, --files or --note")
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))

    def record_batch(current: Ledger) -> tuple[Ledger, list[str]]:
        round_ = require_current_round(current, snap)
        lines: list[str] = []
        for index, raw in enumerate(raw_entries, 1):
            label = raw.get("lens") if isinstance(raw, dict) and isinstance(raw.get("lens"), str) else None
            try:
                f = _Fields(raw)
                lens = f.choice("lens", tuple(sorted(LENS_IDS)))
                key = "files" if isinstance(raw, dict) and "files" in raw else "hunks"
                targets = [item.strip() for item in f.texts(key) if item.strip()]
                note = f.text("note")
                f.done(None)
                current, _ = add_coverage(current, lens, targets, note)
            except LedgerError as exc:
                raise _batch_error(index, label, exc) from None
            left = sum(len(ids) for ids in uncovered_by_lens(current, round_).values())
            lines.append(
                f"Coverage recorded: `{lens}` covered {', '.join(targets)} in round "
                f"{round_.number}; {left} lens-hunk pair(s) left in this round."
            )
        return current, lines

    for line in mutate(ws.ledger_path, record_batch):
        print(line)
    return 0


def _cover(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _cover_from_json(args)
    if args.hunks is not None and args.files is not None:
        raise LedgerError("pass either --hunks or --files, not both")
    if args.lens == DESIGN_LENS and args.hunks is not None:
        raise LedgerError("the `design` lens covers changed files, not hunks; pass --files instead of --hunks")
    if args.lens != DESIGN_LENS and args.files is not None:
        raise LedgerError("--files is only used with `--lens design`; pass --hunks for hunk lenses")
    raw_targets = args.files if args.files is not None else args.hunks
    target_flag = "--files" if args.lens == DESIGN_LENS else "--hunks"
    missing = [
        flag
        for flag, val in (("--lens", args.lens), (target_flag, raw_targets), ("--note", args.note))
        if val is None
    ]
    if missing:
        args.subparser.error(f"the following arguments are required: {', '.join(missing)}")
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))
    targets = [item.strip() for item in (raw_targets or "").split(",") if item.strip()]

    def record(current: Ledger) -> tuple[Ledger, tuple[Ledger, Round]]:
        require_current_round(current, snap)
        updated, _ = add_coverage(current, args.lens, targets, args.note)
        return updated, (updated, updated.rounds[-1])

    ledger, round_ = mutate(ws.ledger_path, record)
    left = sum(len(ids) for ids in uncovered_by_lens(ledger, round_).values())
    print(
        f"Coverage recorded: `{args.lens}` covered {', '.join(targets)} in round {round_.number}; "
        f"{left} lens-hunk pair(s) left in this round."
    )
    return 0


def _source_lines(root: Path, path: str, line: int) -> list[str] | None:
    """The cited file's lines (None when the diff deletes it), refusing a line past its end."""
    source = root / path
    lines = _git_lines(source) if source.is_file() else None
    if lines is not None and line > len(lines):
        raise FieldError("line", f"{line} is past the end of {path}, which has {len(lines)} lines")
    return lines


def _add_one_from_json(
    ws: Workspace, cwd: Path, snap: DiffSnapshot, current: Ledger, raw: Any
) -> tuple[Ledger, Finding, list[str] | None]:
    f = _Fields(raw)
    lens = f.choice("lens", tuple(sorted(LENS_IDS)))
    raw_file = f.text("file")
    line = f.integer("line")
    severity = f.choice("severity", SEVERITIES)
    title = f.text("title")
    scenario = f.text("scenario")
    suggestion = f.get_text("suggestion")
    signal_id = f.get_optional_text("signal")
    f.done(None)
    path = _finding_path(ws.root, cwd, raw_file, snap)
    lines = _source_lines(ws.root, path, line)
    updated, finding = add_finding(
        current,
        lens=lens,
        path=path,
        line=line,
        severity=severity,
        title=title,
        scenario=scenario,
        suggestion=suggestion,
        signal_id=signal_id,
    )
    return updated, finding, lines


def _add_from_json(args: argparse.Namespace) -> int:
    flags = (
        args.lens,
        args.file,
        args.line,
        args.severity,
        args.title,
        args.scenario,
        args.suggestion,
        args.signal,
    )
    if any(val is not None for val in flags):
        raise LedgerError(
            "--from-json cannot be combined with --lens, --file, --line, --severity, --title, "
            "--scenario, --suggestion or --signal"
        )
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))
    cwd = Path(args.cwd or os.getcwd())

    def record_batch(current: Ledger) -> tuple[Ledger, list[tuple[Finding, list[str] | None]]]:
        require_current_round(current, snap)
        results: list[tuple[Finding, list[str] | None]] = []
        for index, raw in enumerate(raw_entries, 1):
            next_id = _next_id("F", (item.id for item in current.findings))
            try:
                current, finding, lines = _add_one_from_json(ws, cwd, snap, current, raw)
            except LedgerError as exc:
                raise _batch_error(index, next_id, exc) from None
            results.append((finding, lines))
        return current, results

    for finding, lines in mutate(ws.ledger_path, record_batch):
        print(f"{finding.id} recorded ({finding.severity}, {finding.lens}, {finding.path}:{finding.line})")
        print(_cited_line(finding, lines))
    return 0


def _add(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _add_from_json(args)
    required = (
        ("--lens", args.lens),
        ("--file", args.file),
        ("--line", args.line),
        ("--severity", args.severity),
        ("--title", args.title),
        ("--scenario", args.scenario),
    )
    missing = [flag for flag, val in required if val is None]
    if missing:
        args.subparser.error(f"the following arguments are required: {', '.join(missing)}")
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))
    path = _finding_path(ws.root, Path(args.cwd or os.getcwd()), args.file, snap)
    lines = _source_lines(ws.root, path, args.line)

    def record(current: Ledger) -> tuple[Ledger, Finding]:
        require_current_round(current, snap)
        return add_finding(
            current,
            lens=args.lens,
            path=path,
            line=args.line,
            severity=args.severity,
            title=args.title,
            scenario=args.scenario,
            suggestion=args.suggestion or "",
            signal_id=args.signal,
        )

    finding = mutate(ws.ledger_path, record)
    print(f"{finding.id} recorded ({finding.severity}, {finding.lens}, {finding.path}:{finding.line})")
    print(_cited_line(finding, lines))
    return 0


def _ack(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))

    def record(current: Ledger) -> tuple[Ledger, SignalAck]:
        require_current_round(current, snap)
        return ack_signal(current, args.signal_id, args.reason)

    ack = mutate(ws.ledger_path, record)
    print(f"{ack.signal_id} acknowledged in round {ack.round}: {ack.reason}")
    return 0


def _move(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))
    path = None
    if args.file is not None:
        path = _finding_path(ws.root, Path(args.cwd or os.getcwd()), args.file, snap)

    def record(current: Ledger) -> tuple[Ledger, tuple[Finding, list[str] | None]]:
        updated, finding = relocate_finding(
            current, args.finding_id, path=path, line=args.line, fingerprint=snap.fingerprint
        )
        return updated, (finding, _source_lines(ws.root, finding.path, finding.line))

    finding, lines = mutate(ws.ledger_path, record)
    print(f"{finding.id} now points at {finding.path}:{finding.line}")
    print(_cited_line(finding, lines))
    return 0


def _merge(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)

    def record(current: Ledger) -> tuple[Ledger, tuple[Finding, tuple[Finding, ...]]]:
        updated, target, duplicates = merge_findings(
            current,
            args.target_id,
            args.duplicate_ids,
            title=args.title,
            scenario=args.scenario,
            suggestion=args.suggestion,
        )
        return updated, (target, duplicates)

    target, duplicates = mutate(ws.ledger_path, record)
    dup_ids = ", ".join(item.id for item in duplicates)
    print(f"{dup_ids} merged into {target.id} ({target.severity}, {target.path}:{target.line})")
    return 0


def _cited_line(finding: Finding, lines: Sequence[str] | None) -> str:
    """Echo the cited source line so a wrong `--line` is noticed while the finding is fresh."""
    if lines is None:
        return f"  ({finding.path} is deleted in the diff; line {finding.line} refers to the base version)"
    text = lines[finding.line - 1].strip()
    return f"  L{finding.line}: {text[:CITED_LINE_CHARS]}{'…' if len(text) > CITED_LINE_CHARS else ''}"


def _git_lines(path: Path) -> list[str]:
    """The file's lines as git counts them: split on newlines only."""
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc
    return text.removesuffix("\n").split("\n") if text else []


def _finding_path(root: Path, cwd: Path, raw: str, snap: DiffSnapshot) -> str:
    """Root-relative POSIX path of a file in the work tree or the diff.

    A relative `--file` is looked up from `--cwd` first, then from the repository root. Symlinks
    are not followed, so a finding on a tracked symlink keeps the symlink's path.
    """
    root = root.resolve()
    candidates = [Path(raw)] if Path(raw).is_absolute() else [cwd.resolve() / raw, root / raw]
    inside = False
    for candidate in candidates:
        try:
            relative = Path(os.path.normpath(candidate)).relative_to(root).as_posix()
        except ValueError:
            continue
        inside = True
        if relative in snap.files or (root / relative).is_file():
            return relative
    if not inside:
        raise FieldError("path", f"{raw!r} is outside the repository {root}")
    raise FieldError("path", f"{raw!r} does not exist in the work tree or the diff")


def _verdict(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        if any(val is not None for val in (args.finding_id, args.verdict, args.evidence, args.run)):
            raise LedgerError("--from-json cannot be combined with ID, --as, --evidence or --run")
        raw_entries = _load_json_entries(args.from_json)
        ws = open_workspace(args.cwd)

        def record_batch(current: Ledger) -> tuple[Ledger, list[Finding]]:
            judged: list[Finding] = []
            for index, raw in enumerate(raw_entries, 1):
                label = (
                    raw.get("id")
                    if isinstance(raw, dict) and isinstance(raw.get("id"), str)
                    else None
                )
                try:
                    f = _Fields(raw)
                    finding_id = f.text("id")
                    verdict = f.choice("as", VERDICTS)
                    evidence = f.text("evidence")
                    run_id = f.get_optional_text("run")
                    f.done(None)
                    current, finding = set_verdict(current, finding_id, verdict, evidence, run_id)
                except LedgerError as exc:
                    raise _batch_error(index, label, exc) from None
                judged.append(finding)
            return current, judged

        for finding in mutate(ws.ledger_path, record_batch):
            run = f" (run {finding.verdict_run})" if finding.verdict_run else ""
            print(f"{finding.id} verdict recorded: {finding.verdict}{run}")
        return 0

    missing = [
        flag
        for flag, val in (
            ("ID", args.finding_id),
            ("--as", args.verdict),
            ("--evidence", args.evidence),
        )
        if val is None
    ]
    if missing:
        args.subparser.error(f"the following arguments are required: {', '.join(missing)}")
    ws = open_workspace(args.cwd)
    finding = mutate(
        ws.ledger_path,
        lambda current: set_verdict(current, args.finding_id, args.verdict, args.evidence, args.run),
    )
    run = f" (run {finding.verdict_run})" if finding.verdict_run else ""
    print(f"{finding.id} verdict recorded: {finding.verdict}{run}")
    return 0


def _resolve(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = snapshot(ws.root, resolve_base(ws, load(ws.ledger_path), None))
    finding = mutate(
        ws.ledger_path,
        lambda current: resolve(
            current, args.finding_id, args.resolution, args.note, fingerprint=snap.fingerprint
        ),
    )
    print(f"{finding.id} resolved as {finding.resolution}")
    return 0


def _waive_run(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    run = mutate(ws.ledger_path, lambda current: waive_run(current, args.run_id, args.reason))
    print(f"{run.id} waived; it no longer blocks the gate")
    return 0


def _show(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    snap = snapshot(ws.root, resolve_base(ws, ledger, None))
    latest = ledger.rounds[-1] if ledger.rounds else None
    if getattr(args, "packet_path", False):
        if latest is None:
            raise LedgerError("no review round yet; run `craftsman review prepare`")
        print(str(Path(latest.packet_path).resolve()))
        return 0
    if args.json:
        payload = {
            "round": _round_json(latest, snap) if latest else None,
            "coverage": _coverage_json(ledger, latest) if latest else {},
            "findings": [finding.to_json() for finding in ledger.findings],
            "runs": [run.to_json() for run in ledger.runs[-SHOW_RECENT_RUNS:]],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
    else:
        print(_round_line(latest, snap))
        print(f"Packet: {Path(latest.packet_path).resolve()}")
        print("")
        print("Coverage (covered/total non-carried hunks):")
        print("\n".join(_coverage_lines(ledger, latest)))
    print("")
    print("\n".join(_findings_table(ledger.findings)))
    print("")
    print("\n".join(_run_lines(ledger.runs[-SHOW_RECENT_RUNS:], snap)))
    return 0


def _round_line(round_: Round, snap: DiffSnapshot) -> str:
    carried = sum(hunk.carried for hunk in round_.hunks)
    if round_.fingerprint == snap.fingerprint:
        freshness = "current"
    elif snap.is_empty:
        freshness = f"the work tree matches base {snap.base_sha[:12]}: nothing left to review"
    else:
        freshness = "stale: the code changed, run `craftsman review prepare`"
    return (
        f"Round {round_.number} ({round_.tier}) prepared {round_.prepared_at} · "
        f"{len(round_.hunks)} hunks ({carried} carried) · {freshness}"
    )


def _round_json(round_: Round, snap: DiffSnapshot) -> dict[str, Any]:
    return {
        "number": round_.number,
        "tier": round_.tier,
        "prepared_at": round_.prepared_at,
        "current": round_.fingerprint == snap.fingerprint,
        "hunks": len(round_.hunks),
        "carried": sum(hunk.carried for hunk in round_.hunks),
        "lenses": list(round_.lenses),
        "packet": round_.packet_path,
    }


def _coverage_json(ledger: Ledger, round_: Round) -> dict[str, Any]:
    open_hunks = sum(not hunk.carried for hunk in round_.hunks)
    paths = list(dict.fromkeys(h.path for h in round_.hunks))
    open_files = sum(not all(h.carried for h in round_.hunks if h.path == p) for p in paths)
    return {
        lens: {
            "covered": (open_files if lens == DESIGN_LENS else open_hunks) - len(missing),
            "total": open_files if lens == DESIGN_LENS else open_hunks,
            "missing": missing,
        }
        for lens, missing in uncovered_by_lens(ledger, round_).items()
    }


def _coverage_lines(ledger: Ledger, round_: Round) -> list[str]:
    width = max(len(lens) for lens in round_.lenses)
    lines = []
    for lens, entry in _coverage_json(ledger, round_).items():
        missing = f" (missing {','.join(entry['missing'])})" if entry["missing"] else ""
        last = " — run last" if lens == SWEEP_LENS else ""
        lines.append(f"  {lens:<{width}}  {entry['covered']}/{entry['total']}{missing}{last}")
    return lines


def _findings_table(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["Findings: none recorded."]
    lines = [
        "| ID | Severity | Verdict | Resolution | Location | Title |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for finding in findings:
        run = f" ({finding.verdict_run})" if finding.verdict_run else ""
        verdict = f"{finding.verdict or '—'}{run}"
        lines.append(
            f"| {finding.id} | {finding.severity} | {verdict} | {finding.resolution or '—'} | "
            f"`{_cell(finding.path)}:{finding.line}` | {_cell(finding.title)} |"
        )
    return lines


def _cell(text: str) -> str:
    """Text safe for one Markdown table cell: pipes escaped, line breaks flattened."""
    return " ".join(text.split("\n")).replace("\r", "").replace("|", "\\|")


def _run_lines(runs: Sequence[Run], snap: DiffSnapshot) -> list[str]:
    if not runs:
        return ["Runs: none recorded."]
    lines = [f"Runs (last {len(runs)}):"]
    for run in runs:
        freshness = "current" if run.fingerprint == snap.fingerprint else "stale"
        lines.append(
            f"- {run.id} {run.kind} exit {run.exit_code} · {run.duration_s:.1f}s · {freshness} · "
            f"`{run.short_command}`"
        )
    return lines


def _evaluate(
    ws: Workspace, tier_override: Tier | None, *, review_only: bool = False
) -> tuple[Ledger, DiffSnapshot, Tier, GateReport]:
    if review_only and ws.state:
        raise LedgerError(
            "--review-only is not allowed during an active /craftsman gate session; "
            "in /craftsman the full gate applies (run `craftsman review check` without --review-only)"
        )
    ledger = load(ws.ledger_path)
    tier = evaluation_tier(ws, ledger, tier_override)
    snap = snapshot(ws.root, resolve_base(ws, ledger, None))
    policy = gate_policy(ws.state, tier)
    return ledger, snap, tier, evaluate(ledger, snap, policy, review_only=review_only)


def _check(args: argparse.Namespace) -> int:
    _, _, _, report = _evaluate(
        open_workspace(args.cwd), args.tier, review_only=args.review_only
    )
    if args.json:
        print(json.dumps(_report_json(report), indent=2, ensure_ascii=False))
    else:
        print(render_report(report))
    return 0 if report.passed else 1


def _report_json(report: GateReport) -> dict[str, Any]:
    return {
        "passed": report.passed,
        "issues": [{"code": issue.code, "message": issue.message} for issue in report.issues],
        "warnings": list(report.warnings),
        "summary": report.summary,
    }


def _report(args: argparse.Namespace) -> int:
    ledger, snap, tier, report = _evaluate(
        open_workspace(args.cwd), None, review_only=args.review_only
    )
    groups = _finding_groups(ledger.findings)
    runs = _counting_runs(ledger.runs, snap.fingerprint)
    if args.json:
        payload = {
            **_report_json(report),
            "tier": tier,
            "rounds": len(ledger.rounds),
            "base_sha": snap.base_sha,
            "diff": {"files": len(snap.files), "added": snap.added, "deleted": snap.deleted},
            "findings": {name: [f.to_json() for f in items] for name, items in groups.items()},
            "runs": [run.to_json() for run in runs],
            "waived_runs": [run.to_json() for run in ledger.runs if run.waiver is not None],
            "signal_acks": [ack.to_json() for ack in ledger.signal_acks],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print("\n".join(_report_lines(ledger, snap, tier, report, groups, runs)))
    return 0


def _finding_groups(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = {
        "fixed": [],
        "accepted": [],
        "open": [],
        "question": [],
        "dismissed": [],
    }
    for finding in sorted(findings, key=lambda item: SEVERITIES.index(item.severity)):
        if finding.verdict == "dismissed":
            groups["dismissed"].append(finding)
        elif finding.verdict == "question":
            groups["question"].append(finding)
        elif finding.resolution is not None:
            groups[finding.resolution].append(finding)
        else:
            groups["open"].append(finding)
    return groups


def _counting_runs(runs: Sequence[Run], fingerprint: str) -> list[Run]:
    """Non-trivial commands whose latest run at the current code, per directory, passed."""
    latest = {
        (run.cwd, run.argv): run
        for run in runs
        if run.fingerprint == fingerprint and not is_trivial_run(run.argv)
    }
    return [run for run in latest.values() if run.counts_as_passing]


def _report_status_line(report: GateReport) -> str:
    if report.review_only:
        if report.passed:
            return "- Review: complete (review-only)"
        codes = ", ".join(dict.fromkeys(issue.code for issue in report.issues))
        return (
            f"- Review: incomplete — {len(report.issues)} open items ({codes}); "
            "see `craftsman review check --review-only`"
        )
    if report.passed:
        return "- Gate: PASSED"
    codes = ", ".join(dict.fromkeys(issue.code for issue in report.issues))
    return f"- Gate: BLOCKED — {len(report.issues)} open items ({codes}); see `craftsman review check`"


def _report_finding_lines(ledger: Ledger, groups: dict[str, list[Finding]]) -> list[str]:
    if not ledger.findings:
        return ["No findings recorded."]
    detail = {
        "fixed": lambda f: f"Fix: {f.resolution_note}",
        "accepted": lambda f: f"Accepted because: {f.resolution_note}",
        "open": lambda f: f"Verdict: {f.verdict or 'none yet'}",
        "question": lambda f: f"Question: {f.verdict_evidence}",
        "dismissed": lambda f: f"Dismissed: {f.verdict_evidence}",
    }
    lines: list[str] = []
    for name, items in groups.items():
        if items:
            lines.append(f"**{name.capitalize()} ({len(items)})**")
            lines.extend(
                f"- {f.id} · {f.severity} · `{f.path}:{f.line}` — {f.title}. {detail[name](f)}"
                for f in items
            )
    return lines


def _signal_ack_lines(ledger: Ledger) -> list[str]:
    if not ledger.signal_acks:
        return []
    by_round_id = {(r.number, s.id): s for r in ledger.rounds for s in r.signals}
    lines = ["", "## Acknowledged quality signals"]
    for ack in ledger.signal_acks:
        sig = by_round_id.get((ack.round, ack.signal_id))
        loc = f" (`{sig.kind}` at `{sig.file}:{sig.line}`)" if sig else ""
        lines.append(f"- {ack.signal_id}{loc} — {ack.reason}")
    return lines


def _report_lines(
    ledger: Ledger,
    snap: DiffSnapshot,
    tier: Tier,
    report: GateReport,
    groups: dict[str, list[Finding]],
    runs: Sequence[Run],
) -> list[str]:
    lines = [
        "# Craftsman review report",
        f"- Base `{snap.base_sha[:7]}` · tier `{tier}` · {len(ledger.rounds)} review round(s) · "
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines",
        _report_status_line(report),
        "",
        "## Findings",
        *_report_finding_lines(ledger, groups),
        *_signal_ack_lines(ledger),
        "",
        "## Verification at the final code",
    ]
    lines.extend(
        f"- {run.id} `{run.command}` ({run.kind}) → exit {run.exit_code} in {run.duration_s:.1f}s"
        for run in runs
    )
    if not runs:
        lines.append("No passing test, e2e, lint or build run at the final code.")
    waived = [(run, run.waiver) for run in ledger.runs if run.waiver is not None]
    if waived:
        lines += ["", "## Waived runs"]
        lines.extend(
            f"- {run.id} `{run.command}` ({run.kind}, exit {run.exit_code}) — {waiver.reason}"
            for run, waiver in waived
        )
    if report.warnings:
        lines += ["", "## Warnings", *(f"- {warning}" for warning in report.warnings)]
    return lines


def _reset(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    archived = archive_ledger(ws.ledger_path)
    if not archived:
        print("Nothing to archive: no ledger or review packets yet.")
    for path in archived:
        print(f"Archived to {path}")
    return 0


# --- argument parsing -----------------------------------------------------------------------


def literal_choice(choices: Sequence[S]) -> Callable[[str], S]:
    """argparse `type=` converter that narrows a string to one of the `Literal` choices."""

    def parse(value: str) -> S:
        for choice in choices:
            if value == choice:
                return choice
        raise argparse.ArgumentTypeError(f"invalid choice {value!r} (choose from {', '.join(choices)})")

    return parse


def _tier_option(value: str) -> Tier | None:
    """`--tier`: a concrete tier, or `auto` for the tier the gate resolved for the active task."""
    if value == "auto":
        return None
    for tier in TIERS:
        if value == tier:
            return tier
    raise argparse.ArgumentTypeError(f"invalid choice {value!r} (choose from auto, {', '.join(TIERS)})")


def _add_prepare_and_cover_parsers(
    sub: Any, common: argparse.ArgumentParser, lenses: Sequence[str]
) -> None:
    prepare = sub.add_parser(
        "prepare",
        parents=[common],
        help="Snapshot the diff and open a round",
        description="Snapshot the diff since the base commit and open a review round.",
        epilog=(
            "Tool caches (__pycache__, .pytest_cache, node_modules, ...) never enter the diff. "
            f"List further paths to leave out, in .gitignore syntax, in a {IGNORE_FILE} file at the "
            "repository root."
        ),
    )
    prepare.add_argument("--tier", type=_tier_option, help="auto (default), quick, standard or deep")
    prepare.add_argument("--base", help="Base commit (default: the active task's base or HEAD)")
    prepare.add_argument("--task", help="One-line description of what the change is for")
    prepare.set_defaults(handler=_prepare)

    cover = sub.add_parser("cover", parents=[common], help="Record that a lens reviewed hunks or files")
    cover.add_argument("--lens", choices=lenses)
    cover.add_argument("--hunks", help="Comma-separated hunk ids, e.g. H1,H3")
    cover.add_argument("--files", help="Comma-separated file paths for `--lens design`, e.g. pkg/a.py")
    cover.add_argument("--note", help="What you tried on each named hunk or file")
    cover.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"lens", "hunks"|"files", "note"} objects (use - for stdin)',
    )
    cover.set_defaults(handler=_cover, subparser=cover)


def _add_finding_parsers(
    sub: Any, common: argparse.ArgumentParser, lenses: Sequence[str]
) -> None:
    add = sub.add_parser("add", parents=[common], help="Record a finding")
    add.add_argument("--lens", choices=lenses)
    add.add_argument("--file")
    add.add_argument("--line", type=int)
    add.add_argument("--severity", type=literal_choice(SEVERITIES))
    add.add_argument("--title")
    add.add_argument("--scenario", help="Trigger -> wrong outcome")
    add.add_argument("--suggestion", default=None)
    add.add_argument("--signal", default=None, help="Quality signal id resolved by this finding (e.g. S1)")
    add.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"lens", "file", "line", "severity", "title", "scenario", "suggestion"?, "signal"?} objects (use - for stdin)',
    )
    add.set_defaults(handler=_add, subparser=add)

    move = sub.add_parser(
        "move",
        parents=[common],
        help="Correct the line (or file) of a finding",
        description="Point a finding at the right line before it has a verdict, instead of "
        "recording a duplicate.",
    )
    move.add_argument("finding_id", metavar="ID")
    move.add_argument("--line", required=True, type=int)
    move.add_argument("--file", help="New file (default: keep the finding's file)")
    move.set_defaults(handler=_move)


def _add_merge_and_verdict_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    merge = sub.add_parser(
        "merge",
        parents=[common],
        help="Group duplicate findings into one root-cause finding (only when one fix removes every consequence)",
        description=(
            "Combine unjudged findings that share a root cause into a single finding "
            "(listing all consequences in its scenario) and dismiss the duplicates. "
            "Merge only when ONE fix removes every consequence. Different fixes are separate "
            "findings, even on the same line, file, or category (for example, ordering entries "
            "in AUTHORS and a missing changelog entry are two separate findings, not one "
            "'housekeeping' finding)."
        ),
    )
    merge.add_argument("target_id", metavar="TARGET_ID", help="Finding id to keep (e.g. F1)")
    merge.add_argument("duplicate_ids", metavar="DUP_ID", nargs="+", help="Duplicate finding ids to fold into TARGET_ID")
    merge.add_argument("--title", help="Updated title for the grouped finding")
    merge.add_argument("--scenario", help="Combined root-cause -> consequences scenario")
    merge.add_argument("--suggestion", help="Updated suggestion for the grouped finding")
    merge.set_defaults(handler=_merge)

    verdict = sub.add_parser("verdict", parents=[common], help="Record a verdict on a finding")
    verdict.add_argument("finding_id", metavar="ID", nargs="?")
    verdict.add_argument("--as", dest="verdict", type=literal_choice(VERDICTS))
    verdict.add_argument("--evidence")
    verdict.add_argument("--run", help="Id of a recorded run that supports the verdict")
    verdict.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"id", "as", "evidence", "run"?} objects (use - for stdin)',
    )
    verdict.set_defaults(handler=_verdict, subparser=verdict)


def _add_resolution_and_status_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    resolution = sub.add_parser("resolve", parents=[common], help="Mark a finding fixed/accepted")
    resolution.add_argument("finding_id", metavar="ID")
    resolution.add_argument("--as", dest="resolution", required=True, type=literal_choice(RESOLUTIONS))
    resolution.add_argument("--note", required=True)
    resolution.set_defaults(handler=_resolve)

    ack = sub.add_parser("ack", parents=[common], help="Acknowledge a quality signal with a concrete reason")
    ack.add_argument("signal_id", metavar="ID", help="Quality signal id (e.g. S1)")
    ack.add_argument("--reason", required=True, help="Why the signal is acceptable (>= 40 chars, concrete)")
    ack.set_defaults(handler=_ack)

    waiver = sub.add_parser(
        "waive-run",
        parents=[common],
        help="Retire an obsolete failing run",
        description=(
            "Stop a failing run from blocking the gate when its command can never pass again, "
            "e.g. a mistyped or renamed test. The reason appears in the final report."
        ),
    )
    waiver.add_argument("run_id", metavar="ID")
    waiver.add_argument("--reason", required=True, help="Why the command is obsolete")
    waiver.set_defaults(handler=_waive_run)


def _add_inspection_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    for name, handler, help_text in (
        ("show", _show, "Show coverage, findings and runs"),
        ("check", _check, "Evaluate the gate; exit 0 only when it passes"),
        ("report", _report, "Final Markdown report for the user"),
    ):
        command = sub.add_parser(name, parents=[common], help=help_text)
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handler)
    sub.choices["show"].add_argument(
        "--packet-path",
        action="store_true",
        help="Print only the resolved absolute path of the current review packet",
    )
    sub.choices["check"].add_argument("--tier", type=_tier_option)
    for name in ("check", "report"):
        sub.choices[name].add_argument(
            "--review-only",
            action="store_true",
            help="Check only review completeness (round, lenses, sweep, verdicts), without requiring fixes or test runs",
        )
    reset = sub.add_parser("reset", parents=[common], help="Archive the ledger and packets")
    reset.set_defaults(handler=_reset)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", help="Directory inside the repository (default: current)")
    parser = argparse.ArgumentParser(
        prog=PROG, description="Record and inspect code review evidence in the Craftsman ledger."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lenses = sorted(LENS_IDS)
    _add_prepare_and_cover_parsers(sub, common, lenses)
    _add_finding_parsers(sub, common, lenses)
    _add_merge_and_verdict_parsers(sub, common)
    _add_resolution_and_status_parsers(sub, common)
    _add_inspection_parsers(sub, common)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result: int = args.handler(args)
    except (LedgerError, DiffScanError) as exc:
        sys.stderr.write(f"{PROG}: error: {describe_error(exc)}\n")
        return 1
    return result


def describe_error(exc: LedgerError | DiffScanError) -> str:
    """The error message, naming the command-line flag when a flag's value was rejected."""
    if isinstance(exc, FieldError) and exc.field in FIELD_FLAGS:
        return f"{FIELD_FLAGS[exc.field]} {exc.problem}"
    return str(exc)
