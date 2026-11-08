"""`craftsman review`: prepare review rounds and record or inspect evidence in the ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from craftsman import huntplan, huntview
from craftsman.agy_sync import sync_antigravity_runs
from craftsman.assets import TemplateRole, load_agent_templates
from craftsman.attribution import agent_runs
from craftsman.diffscan import (
    IGNORE_FILE,
    DiffScanError,
    DiffSnapshot,
    Hunk,
    WorktreeIndex,
    files_without_signal_patterns,
    snapshot,
    staged_paths,
    untracked_paths,
)
from craftsman.evidence import (
    INBOX_KINDS,
    archive_ledger,
    consume_inbox_file,
    create_inbox_file,
    create_scratch_dir,
    effective_deep,
    packet_path,
    prune_all_temp_dirs,
    prune_inbox_files,
    prune_round_files,
)
from craftsman.files import write_text_atomic
from craftsman.gate import GateState, GateStateError, cli_caller, state_path_for, update_state
from craftsman.gatecheck import (
    SELF_REVIEWED_BANNER,
    GatePolicy,
    GateReport,
    SlotStatus,
    evaluate,
    hunt_line,
    hunt_slots,
    large_diff_warning,
    passing_runs,
    render_report,
    require_current_round,
    slot_statuses,
)
from craftsman.generated import ExportError
from craftsman.ledger import (
    FIXED_SCOPE_ERROR,
    TargetKind,
    add_claim,
    add_coverage,
    add_finding,
    load,
    merge_findings,
    mutate,
    next_finding_id,
    open_round,
    plan_coverage,
    relocate_finding,
    resolve,
    round_unchanged,
    set_verdict,
    stable_hunk_ids,
    uncovered_hunks,
    waive_run,
)
from craftsman.lenses import LENS_IDS
from craftsman.packet import (
    PacketContext,
    bound_hints,
    collect_context,
    expand_hunk_spec,
    render_packet,
    render_packet_view,
)
from craftsman.paths import default_repo_dir
from craftsman.quality import collect_quality_signals
from craftsman.records import (
    LEAD_HUNTER,
    RESOLUTIONS,
    SEVERITIES,
    VERDICTS,
    BatchRejected,
    EntryProblem,
    FieldError,
    Fields,
    Finding,
    Ledger,
    LedgerError,
    MultiFieldError,
    QualitySignal,
    Round,
    Run,
    Severity,
    Slot,
    SlotClaim,
    Verdict,
    as_choice,
    check_coverage_note,
    collecting_problems,
    parse_timestamp,
    slot_generation,
)
from craftsman.scout import Scout, parse_target
from craftsman.text import defang_tokens, inline_md, one_line
from craftsman.usage import usage_json, usage_lines
from craftsman.workspace import (
    Workspace,
    brief_platform,
    caller_identity,
    end_review_guard,
    live_marker,
    open_workspace,
    read_marker,
    review_enforcement,
)

PROG = "craftsman review"
SHOW_RECENT_RUNS = 5
CITED_LINE_CHARS = 120
MAX_REPORTED_BATCH_PROBLEMS = 20
MAX_UNSCANNED_PATHS_SHOWN = 8
MAX_VIEW_SLUG_CHARS = 60
VIEW_HASH_CHARS = 8
UTF8_SAVE_HINT = "save it as UTF-8 (PowerShell: Set-Content -Encoding utf8)"
# Ledger record fields that come straight from a command-line flag, for error messages.
FIELD_FLAGS = {
    "lens": "--lens",
    "hunks": "--hunks",
    "files": "--files",
    "paths": "--paths",
    "add_paths": "--add-paths",
    "path": "--file",
    "line": "--line",
    "severity": "--severity",
    "title": "--title",
    "scenario": "--scenario",
    "note": "--note",
    "verdict": "--as",
    "verdict_evidence": "--evidence",
    "verdict_run": "--run",
    "resolution": "--as",
    "resolution_note": "--note",
    "reason": "--reason",
    "hunter": "--hunter",
    "slot": "slot",
    "verifier": "--verifier",
}
S = TypeVar("S", bound=str)
R = TypeVar("R")
T = TypeVar("T")
JSON_FIELD_NAMES = {
    "path": "file",
    "verdict": "as",
    "verdict_evidence": "evidence",
    "verdict_run": "run",
}


# --- subcommands ----------------------------------------------------------------------------


MAX_IGNORED_PATHS_SHOWN = 5


class _UnmatchedPathError(FieldError):
    """A syntactically valid `--paths` entry matched no changed files in the current diff."""


def _comma_list(raw: str) -> list[str]:
    """The non-blank items of a comma-separated flag value, stripped."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _print_empty_prepare(
    scope: tuple[str, ...] | None, snap: DiffSnapshot, full_snap: DiffSnapshot
) -> int:
    if scope is not None and not full_snap.is_empty:
        in_scope = set(scope)
        ignored = [path for path in full_snap.files if path not in in_scope]
        print(
            f"Nothing to review within the frozen scope (matches base {snap.base_sha[:12]}); "
            f"changed files outside scope: {_format_path_sample(ignored)}."
        )
        print(
            "Run `craftsman review reset` to archive this review and start a new one "
            "(or pass `craftsman review prepare --add-paths <paths>` to add files touched by fixes)."
        )
        return 0
    print(f"Nothing to review: the work tree matches base {snap.base_sha[:12]}.")
    return 0


MAX_TASK_CHARS = 4000
GATE_SESSION_CHARS = 8


class CommandRefused(LedgerError):
    """A command the caller must not run (or not run yet): reported like an error, with its own exit code."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class _PrepareContext:
    """Everything `prepare` decides before it takes the ledger lock."""

    ws: Workspace
    env: Mapping[str, str]
    snap: DiffSnapshot
    scope: tuple[str, ...] | None
    allow_scope_expansion: bool
    out_of_scope: tuple[str, ...]
    depth: huntplan.Depth
    symbols: Mapping[str, tuple[str, ...]]
    explicit_task: str
    marker_task: str
    now: datetime


@dataclass(frozen=True)
class _Prepared:
    """What `prepare` did to the ledger."""

    ledger: Ledger
    round: Round
    created: bool
    raised: bool
    added_slots: tuple[str, ...]  # slots a re-prepare of the same diff appended to the plan
    task_changed: bool = False  # a re-prepare of the same diff gave the round a different task text
    refreshed: bool = False  # a re-prepare of the same diff released carried hunks or made the round enforced


def _read_task_file(source: str) -> str:
    """The task text of `--task-file` (`-` is stdin): trimmed, at most `MAX_TASK_CHARS` characters."""
    label = "stdin" if source == "-" else source
    try:
        data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read {label}: {exc}") from exc
    return _decode_json_bytes(data, label).strip()[:MAX_TASK_CHARS]


def _explicit_task(args: argparse.Namespace) -> str:
    if args.task is not None and args.task_file is not None:
        raise LedgerError("pass either --task or --task-file, not both")
    raw = args.task if args.task is not None else _read_task_file(args.task_file) if args.task_file else ""
    return raw.strip()[:MAX_TASK_CHARS]


def _depth_floor(
    args: argparse.Namespace, ws: Workspace, ledger: Ledger, marker_deep: bool, *, owned: bool
) -> tuple[bool, str | None]:
    """Whether something already asks for a deep round (never lowered inside a review), and what."""
    reasons = [
        reason
        for asked, reason in (
            (bool(args.deep) and owned, "requested with --deep"),
            (ws.state is not None and ws.state.deep, "the gate is deep"),
            (any(round_.deep for round_ in ledger.rounds), "an earlier round was deep"),
            (marker_deep, "a deep review was requested"),
        )
        if asked
    ]
    return bool(reasons), (reasons[0] if reasons else None)


def _prepare(args: argparse.Namespace) -> int:
    env = os.environ
    now = datetime.now(timezone.utc)
    ws = open_workspace(args.cwd)
    explicit_task = _explicit_task(args)
    sync_antigravity_runs(ws, env)
    ledger = load(ws.ledger_path)
    base_sha = ws.base_sha(ledger, args.base)
    full_snap = snapshot(ws.root, base_sha)
    scope, snap, allow_scope_expansion = _prepare_scope_and_snapshot(
        ws, ledger, args, base_sha, full_snap
    )
    if snap.is_empty:
        end_review_guard(ws.ledger_path)  # nothing to review is a review outcome
        return _print_empty_prepare(scope, snap, full_snap)
    snap = stable_hunk_ids(ledger, snap)
    owned = _owns_gate(ws, env)
    if not owned and args.deep:
        print(
            "Note: --deep ignored: the active gate belongs to another session, "
            "which alone can raise it."
        )
    marker = live_marker(ws.ledger_path, now)  # a stale marker's depth and task belong to an older review
    floor, floor_reason = _depth_floor(args, ws, ledger, marker is not None and marker.deep, owned=owned)
    depth = huntplan.decide_depth(snap, floor=floor, floor_reason=floor_reason)
    if ws.state is not None and owned and depth.deep and not ws.state.deep:
        update_state(state_path_for(ws.root), lambda current: (_raised_to_deep(current), None))
        print("Gate raised to deep: a passing end-to-end run is now required.")
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    ctx = _PrepareContext(
        ws=ws,
        env=env,
        snap=snap,
        scope=scope,
        allow_scope_expansion=allow_scope_expansion,
        out_of_scope=tuple(sorted(set(full_snap.files) - set(scope))) if scope is not None else (),
        depth=depth,
        symbols=symbols,
        explicit_task=explicit_task,
        marker_task=marker.task.strip() if marker is not None else "",
        now=now,
    )

    # The hints parse every changed Python file: compute them before the ledger lock is taken, for
    # the hunks this round will have to review (carried hunks were reviewed already).
    planned = _plan_round(ledger, ctx, lambda _ids: ())
    hint_ids = planned.round.open_hunk_ids if planned.created or planned.refreshed else None
    hints = collect_quality_signals(ws.root, snap.only(hint_ids)) if hint_ids is not None else ()

    def signals_for(open_ids: tuple[str, ...]) -> tuple[QualitySignal, ...]:
        if open_ids == hint_ids:
            return hints
        return collect_quality_signals(ws.root, snap.only(open_ids))  # coverage landed meanwhile: rare, slow path

    def open_next(current: Ledger) -> tuple[Ledger, _Prepared]:
        if stable_hunk_ids(current, snap) != snap:
            raise LedgerError("the ledger changed while the round was prepared; run prepare again")
        result = _plan_round(current, ctx, signals_for)
        return result.ledger, result

    result = mutate(ws.ledger_path, open_next)
    ledger, round_ = result.ledger, result.round
    packet = Path(round_.packet_path)
    if result.created or result.raised or result.refreshed or result.task_changed or not packet.is_file():
        pctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=round_.task)
        _write_text(packet, render_packet(pctx))
    if ws.state is not None and not owned:
        print(_foreign_gate_note(ws.state))
    _print_prepare_summary(result, packet, snap, depth, now, check=_check_command(ws))
    _print_unscanned_files(snap)
    _print_scope_summary(ws, ledger, round_, snap, full_snap)
    if result.created and not result.raised:
        prune_round_files(ws.ledger_path.parent, round_.number)
        # Scratch directories outlive rounds: a repro written in round 1 is re-run after the fix.
        prune_inbox_files(ws.ledger_path.parent)
    return 0


def _plan_round(
    current: Ledger, ctx: _PrepareContext, signals_for: Callable[[tuple[str, ...]], tuple[QualitySignal, ...]]
) -> _Prepared:
    """Open the next round for the snapshot (or take the latest one as it is) and give it its hunt plan.

    The steps follow the planner's contract: the lenses each hunk requires and has been credited are
    computed on the ledger; `open_round` carries over the hunks that owe nothing; the plan is built for
    what the open hunks still owe, appending to the plan the round already has (so re-preparing an
    unchanged diff never changes an existing slot or forgets a wave); finally the waves that are due are
    stored in it. When this opens a NEW round, a wave that is already due is stored BEFORE anything else,
    in the round being replaced, so that a verdict recorded since the last `check` is carried into the new
    round's first plan instead of being lost to it. When the round is only refreshed (an unchanged
    diff, or an in-place raise to deep), no wave is planned until its plan is rebuilt: the barrier of a wave
    must see the generation 1 slots the refresh adds, and the hunks it releases. Carry-over is decided on
    the credit before the round opens; the plan is built on the credit as the round being planned sees it
    (hunks the new round reopened have none).
    """
    snap = ctx.snap
    changing = not round_unchanged(current, snap, ctx.scope)
    base = huntplan.plan_due_waves(current, ctx.now) if changing else current
    required = huntplan.required_by_hunk(snap.hunks, deep=ctx.depth.deep)
    enforced, platform = review_enforcement(ctx.ws, current, ctx.env, ctx.now)
    if not changing and enforced and not base.rounds[-1].enforced:
        # The round is upgraded in place: it is judged as the enforced round it is about to be, so the credit
        # it gets (and the hunks it keeps carried) never rests on what only an advisory round accepts.
        upgraded = replace(base.rounds[-1], enforced=True, platform=platform)
        base = replace(base, rounds=(*base.rounds[:-1], upgraded))
    have = huntplan.credited(base, enforced=enforced)
    carry_sweep = changing and huntplan.sweep_pending(base)
    packet = packet_path(ctx.ws.ledger_path, len(base.rounds) + 1)
    opened, round_ = open_round(
        base, snap, ctx.depth.deep, str(packet), ctx.symbols,
        scope=ctx.scope, allow_scope_expansion=ctx.allow_scope_expansion, out_of_scope=ctx.out_of_scope,
        required=required, credited=have,
    )  # fmt: skip
    created = len(opened.rounds) > len(base.rounds)
    previous = base.rounds[-1] if base.rounds and not created else None
    earlier = base.rounds if created else base.rounds[:-1]
    open_ids = set(round_.open_hunk_ids)
    open_hunks = [hunk for hunk in snap.hunks if hunk.id in open_ids]
    pending = huntplan.pending_wave_slots(base, open_hunks=open_hunks, round_hunks=snap.hunks) if created else ()
    plan = huntplan.build_plan(
        open_hunks,
        huntplan.owed_lenses(required, round_.hunks, have),
        depth=ctx.depth,
        round_number=round_.number,
        first_deep_round=ctx.depth.deep and not any(item.deep for item in earlier),
        existing=round_.plan,
        pending_wave_slots=pending,
        round_hunks=snap.hunks,
        carry_sweep=carry_sweep,
        credited=huntplan.credited(opened, enforced=enforced),
    )
    if round_.enforced:  # sticky: the round keeps the platform that first held it
        enforced, platform = True, round_.platform
    stored_before = current.rounds[-1].plan if current.rounds else None
    known = {slot.id for slot in stored_before.slots} if stored_before is not None and not created else set()
    task = _round_task(ctx, base, round_, created=created)
    refreshed = not created and (round_.hunks != current.rounds[-1].hunks or enforced != current.rounds[-1].enforced)
    prepared = replace(
        round_,
        plan=plan,
        enforced=enforced,
        platform=platform,
        task=task,
        signals=signals_for(round_.open_hunk_ids) if created or refreshed else round_.signals,
    )
    updated = huntplan.plan_due_waves(replace(opened, rounds=(*opened.rounds[:-1], prepared)), ctx.now)
    prepared = updated.rounds[-1]
    return _Prepared(
        ledger=updated,
        round=prepared,
        created=created,
        raised=previous is not None and not previous.deep and prepared.deep,
        added_slots=tuple(slot.id for slot in prepared.plan.slots if slot.id not in known)
        if not created and prepared.plan is not None
        else (),
        task_changed=not created and task != round_.task,
        refreshed=refreshed,
    )


def _round_task(ctx: _PrepareContext, base: Ledger, round_: Round, *, created: bool) -> str:
    """What the round says the change is for.

    The explicit text wins. A re-prepared round otherwise keeps its own; a new one falls back on the review
    marker's, the gate's and finally the previous round's.
    """
    if created:
        previous = base.rounds[-1].task if base.rounds else ""
        candidates = (ctx.explicit_task, ctx.marker_task, ctx.ws.task.strip(), previous)
    else:
        candidates = (ctx.explicit_task, round_.task, ctx.marker_task, ctx.ws.task.strip())
    return next((text for text in candidates if text), "")


def _used_scope_flags(args: argparse.Namespace) -> list[str]:
    return [
        flag
        for flag, active in (
            ("--paths", args.paths is not None),
            ("--add-paths", args.add_paths is not None),
            ("--staged", bool(args.staged)),
            ("--tracked", bool(args.tracked)),
        )
        if active
    ]


def _scope_from_hunks(hunks: Sequence[Hunk]) -> tuple[str, ...]:
    paths: set[str] = set()
    for hunk in hunks:
        paths.add(hunk.path)
        if hunk.old_path is not None:
            paths.add(hunk.old_path)
    return tuple(sorted(paths))


def _gate_label(state: GateState) -> str:
    """`claude session 1a2b3c4d`: whose gate it is, as a person can tell sessions apart."""
    return f"{state.owner.platform} session {(state.owner.session_id or 'unknown')[:GATE_SESSION_CHARS]}"


def _foreign_gate_note(state: GateState) -> str:
    return (
        f"This checkout has an active /craftsman gate ({_gate_label(state)}); this round follows it. "
        "For a separate review use its own worktree: git worktree add <path>."
    )


def _check_command(ws: Workspace) -> str:
    """The command that judges this checkout's review now (see `huntview.check_command`)."""
    return huntview.check_command(gate_active=ws.state is not None)


def _owns_gate(ws: Workspace, env: Mapping[str, str]) -> bool:
    """Whether this command acts for the session that owns the active gate (true without one)."""
    return ws.state is None or ws.owns_gate(env)


def _raised_to_deep(state: GateState | None) -> GateState | None:
    return replace(state, deep=True) if state is not None and state.active else state


def _prepare_scope_and_snapshot(
    ws: Workspace,
    ledger: Ledger,
    args: argparse.Namespace,
    base_sha: str,
    full_snap: DiffSnapshot,
) -> tuple[tuple[str, ...] | None, DiffSnapshot, bool]:
    used = _used_scope_flags(args)
    if ws.state is not None:
        if used:
            raise LedgerError(
                f"{used[0]} cannot be used during an active /craftsman gate session; "
                "the gate always reviews the full work tree diff"
            )
        return None, full_snap, True
    if len(used) > 1:
        raise LedgerError(f"{', '.join(used)} are mutually exclusive; pass at most one scope flag")
    if ledger.rounds:
        current_scope = ledger.current_scope
        if args.add_paths is not None:
            if current_scope is None:
                raise LedgerError(
                    "--add-paths cannot be used when the review already covers the full work tree"
                )
            queries = _parse_path_queries(ws.root, _cwd(args), args.add_paths, field="add_paths")
            added = _match_path_queries(queries, full_snap, base_sha, field="add_paths")
            expanded = tuple(sorted(set(current_scope) | set(added)))
            return expanded, snapshot(ws.root, base_sha, expanded), True
        if used:
            _require_matching_fixed_scope(ws, args, base_sha, full_snap, current_scope)
        snap = full_snap if current_scope is None else snapshot(ws.root, base_sha, current_scope)
        return current_scope, snap, False
    if args.add_paths is not None:
        raise LedgerError(
            "--add-paths extends an existing review scope; in round 1 use --paths (or omit scope flags)"
        )
    if not used:
        if full_snap.is_empty:
            return None, full_snap, False
        scope = _scope_from_hunks(full_snap.hunks)
        return scope, snapshot(ws.root, base_sha, scope), False
    scope = _resolve_flag_scope(ws, args, base_sha, full_snap)
    return (scope or None), snapshot(ws.root, base_sha, scope), False


def _require_matching_fixed_scope(
    ws: Workspace,
    args: argparse.Namespace,
    base_sha: str,
    full_snap: DiffSnapshot,
    fixed_scope: tuple[str, ...] | None,
) -> None:
    try:
        requested = _resolve_flag_scope(ws, args, base_sha, full_snap)
    except _UnmatchedPathError:
        raise LedgerError(FIXED_SCOPE_ERROR) from None
    except FieldError:
        raise
    except LedgerError:
        raise LedgerError(FIXED_SCOPE_ERROR) from None
    if requested != fixed_scope:
        raise LedgerError(FIXED_SCOPE_ERROR)


def _resolve_flag_scope(
    ws: Workspace, args: argparse.Namespace, base_sha: str, full_snap: DiffSnapshot
) -> tuple[str, ...]:
    if args.paths is not None:
        queries = _parse_path_queries(ws.root, _cwd(args), args.paths)
        return _match_path_queries(queries, full_snap, base_sha)
    if args.staged:
        staged = staged_paths(ws.root, base_sha)
        if not staged:
            raise LedgerError(
                f"nothing staged against base {base_sha[:12]}; "
                "stage files with `git add` or omit --staged"
            )
        return staged
    untracked = set(untracked_paths(ws.root))
    tracked_hunks = [
        hunk for hunk in full_snap.hunks if not (hunk.status == "added" and hunk.path in untracked)
    ]
    return _scope_from_hunks(tracked_hunks)


def _parse_path_queries(
    root: Path, cwd: Path, raw_paths: str, *, field: str = "paths"
) -> list[tuple[str, tuple[str, ...], bool]]:
    tokens = _comma_list(raw_paths)
    if not tokens:
        raise FieldError(field, "must name at least one file or directory")
    resolved_root = root.resolve()
    resolved_cwd = cwd.resolve()
    queries: list[tuple[str, tuple[str, ...], bool]] = []
    for raw in tokens:
        dir_only = raw.endswith(("/", "\\"))
        bases = [Path(raw)] if Path(raw).is_absolute() else [resolved_cwd / raw, resolved_root / raw]
        relatives: list[str] = []
        for candidate in bases:
            try:
                rel = Path(os.path.normpath(candidate)).relative_to(resolved_root).as_posix()
            except ValueError:
                continue
            if rel not in relatives:
                relatives.append(rel)
        if not relatives:
            raise FieldError(field, f"{raw!r} is outside the repository {resolved_root}")
        queries.append((raw, tuple(relatives), dir_only))
    return queries


def _hunk_matches_query(hunk: Hunk, relative: str, *, dir_only: bool) -> bool:
    if relative == ".":
        return True
    prefix = f"{relative}/"
    for candidate in (hunk.path, hunk.old_path):
        if candidate is None:
            continue
        if candidate.startswith(prefix) or (not dir_only and candidate == relative):
            return True
    return False


def _match_path_queries(
    queries: Sequence[tuple[str, tuple[str, ...], bool]],
    full_snap: DiffSnapshot,
    base_sha: str,
    *,
    field: str = "paths",
) -> tuple[str, ...]:
    matched_hunks: list[Hunk] = []
    for raw, relatives, dir_only in queries:
        hits: list[Hunk] = []
        for relative in relatives:
            hits = [
                hunk
                for hunk in full_snap.hunks
                if _hunk_matches_query(hunk, relative, dir_only=dir_only)
            ]
            if hits:
                break
        if not hits:
            changed = ", ".join(full_snap.files[:MAX_IGNORED_PATHS_SHOWN]) or "none"
            raise _UnmatchedPathError(
                field,
                f"{raw!r} matched no changed files in the diff since {base_sha[:12]} "
                f"(changed files: {changed})",
            )
        matched_hunks.extend(hits)
    return _scope_from_hunks(matched_hunks)


def _format_path_sample(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:MAX_IGNORED_PATHS_SHOWN])
    extra = len(paths) - MAX_IGNORED_PATHS_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def _print_scope_summary(
    ws: Workspace, ledger: Ledger, round_: Round, snap: DiffSnapshot, full_snap: DiffSnapshot
) -> None:
    if round_.scope is None:
        return
    rename_sources = {hunk.old_path for hunk in snap.hunks if hunk.old_path is not None}
    scoped_count = len(set(round_.scope) - rename_sources)
    print(f"Scope: {scoped_count} file(s), frozen for this review.")
    in_scope = set(round_.scope)
    ignored = [path for path in full_snap.files if path not in in_scope]
    if ignored:
        print(f"Ignored (outside the review scope): {_format_path_sample(ignored)}")
        if ws.state is None and evaluate(
            ledger,
            snap,
            GatePolicy(deep=round_.deep, isolation_evidence=ws.isolation_evidence(ledger)),
            review_only=True,
        ).passed:
            print(
                f"Hint: round {round_.number} is already complete; run `craftsman review reset` "
                "to archive it before starting a new review (or `craftsman review prepare "
                "--add-paths <paths>` to include files added by a fix)."
            )


def _print_unscanned_files(snap: DiffSnapshot) -> None:
    unscanned = files_without_signal_patterns(snap)
    if not unscanned:
        return
    shown = ", ".join(unscanned[:MAX_UNSCANNED_PATHS_SHOWN])
    more = len(unscanned) - MAX_UNSCANNED_PATHS_SHOWN
    if more > 0:
        shown += f" and {more} more"
    print(
        f"Not scanned for risk signals (no patterns for their language): {shown}. Read them for "
        "risks the suggested lenses miss and apply the matching lens yourself."
    )


def _packet_context(
    ws: Workspace,
    snap: DiffSnapshot,
    ledger: Ledger,
    round_: Round,
    index: WorktreeIndex,
    symbols: Mapping[str, tuple[str, ...]],
    *,
    task: str,
    full_hunks: Sequence[str] = (),
) -> PacketContext:
    """What the packet of `round_` shows; `full_hunks` are shown in full even when carried (slice views)."""
    return collect_context(
        ws.root,
        snap,
        round_,
        task,
        symbols=symbols,
        ledger=ledger,
        index=index,
        full_hunks=full_hunks,
    )


def _production_lines(snap: DiffSnapshot) -> int:
    return sum(huntplan.changed_lines(hunk) for hunk in snap.hunks if huntplan.is_production_hunk(hunk))


def _depth_line(depth: huntplan.Depth, snap: DiffSnapshot) -> str:
    if depth.deep:
        return f"Depth: deep ({'; '.join(depth.reasons)})"
    lines = _production_lines(snap)
    return (
        f"Depth: normal (no deep signal; {lines} changed production lines <= "
        f"{huntplan.DEEP_MIN_PRODUCTION_LINES - 1})"
    )


def _enforcement_line(round_: Round) -> str:
    if round_.enforced:
        who = huntview.PLATFORM_LABELS.get(round_.platform, "Craftsman")
        return f"Enforced: {who} hooks serve this review; finishing requires every slot below."
    return (
        "Advisory: no Craftsman hook saw this session, so the review will be SELF-REVIEWED "
        "(run `craftsman doctor`)."
    )


def _print_prepare_summary(
    result: _Prepared, packet: Path, snap: DiffSnapshot, depth: huntplan.Depth, now: datetime, *, check: str
) -> None:
    round_ = result.round
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
    changed = snap.added + snap.deleted
    deep = " (deep)" if round_.deep and not result.raised else ""
    state = (
        "raised to deep (same diff)"
        if result.raised
        else "prepared"
        if result.created
        else "unchanged (same diff)"
    )
    print(
        f"Round {round_.number} {state}{deep}: {len(snap.files)} files, {len(snap.hunks)} hunks, "
        f"{changed} changed lines; {carried} hunks carried, {len(round_.open_hunk_ids)} to review."
    )
    print(f"Packet: {packet}")
    print(_depth_line(depth, snap))
    print(_enforcement_line(round_))
    if round_.signals:
        listed, summary = bound_hints(round_.signals)
        print(f"Quality hints: {summary}")
        for sig in listed:
            print(f"  {sig.id} {sig.kind.value} {sig.file}:{sig.line} — {sig.detail}")
    warning = large_diff_warning(round_)
    if warning is not None:
        print(f"Warning: {warning}")
    _print_hunt_plan(result, now, check=check)


def _print_hunt_plan(result: _Prepared, now: datetime, *, check: str) -> None:
    """The plan table and what to do next. A re-prepared round shows where each slot stands.

    An advisory round is judged by its hunk coverage, so its plan is a suggestion: once every hunk is covered
    nothing is left to launch, whatever state its slots are in.
    """
    round_ = result.round
    statuses = slot_statuses(result.ledger, round_, now)
    print(f"Hunt plan, round {round_.number}:")
    if not statuses:
        print("  (no slot: every hunk is carried over from earlier rounds)")
        print(f"Nothing is owed. Run `{check}`.")
        return
    covered = not round_.enforced and not result.created and not uncovered_hunks(result.ledger, round_)
    for line in huntview.plan_lines(statuses, with_state=not result.created and not covered):
        print(line)
    if not round_.enforced:
        print("This plan is a suggestion: an advisory review is complete when every hunk is covered.")
    if result.added_slots:
        print(f"New slots since the last prepare: {', '.join(result.added_slots)}")
    if covered:
        print(f"Every hunk is covered. Run `{check}`.")
        return
    todo = [status.slot.id for status in statuses if status.state in ("unlaunched", "dead")]
    running = [status.slot.id for status in statuses if status.state == "running"]
    if running:
        print(f"Running: {', '.join(running)} - do not launch these again; end your turn and wait for their reports.")
    if todo:
        for line in huntview.launch_lines(todo, check=check):
            print(line)
    elif not running:
        print(f"Every slot is done. Run `{check}`.")


def _write_text(path: Path, text: str) -> None:
    """Replace `path` atomically: a reader never sees a half-written packet or view."""
    try:
        write_text_atomic(path, text)
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc


def view_slug(*, hunks: Sequence[str] = (), files: Sequence[str] = ()) -> str:
    """The file-name part of a reviewer view: one name per selection, however it was spelled.

    Hunks are named by their sorted ids in runs (`H1,H3` and `H3,H1` give `H1_H3`, `H1-H3` gives
    `H1-H3`); files by their sorted paths plus a hash of them, since flattening `/` and `.` makes
    different paths look alike. A name that has to be cut also gets a hash of the whole selection.
    """
    if hunks:
        numbers = sorted({int(hunk_id[1:]) for hunk_id in hunks})
        label = "_".join(_hunk_run(run) for run in _runs(numbers))
        whole = label
    else:
        paths = sorted(set(files))
        flat = "-".join("".join(ch if ch.isalnum() else "-" for ch in path).strip("-") for path in paths)
        label = "-".join(part for part in flat.split("-") if part)
        whole = "\n".join(paths)
        label = f"{label}-" if label else ""
        label += hashlib.sha1(whole.encode("utf-8")).hexdigest()[:VIEW_HASH_CHARS]
    if len(label) <= MAX_VIEW_SLUG_CHARS:
        return label or "view"
    digest = hashlib.sha1(whole.encode("utf-8")).hexdigest()[:VIEW_HASH_CHARS]
    return f"{label[: MAX_VIEW_SLUG_CHARS - VIEW_HASH_CHARS - 1].rstrip('-_')}-{digest}"


def _runs(numbers: Sequence[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for number in numbers:
        if runs and runs[-1][1] + 1 == number:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return runs


def _hunk_run(run: tuple[int, int]) -> str:
    first, last = run
    return f"H{first}" if first == last else f"H{first}-H{last}"


def _view_hunk_ids(
    round_: Round, args: argparse.Namespace, files: Sequence[str] | None = None
) -> tuple[str, ...]:
    if args.hunks is not None:
        return expand_hunk_spec(args.hunks, [hunk.id for hunk in round_.hunks])
    paths = list(files) if files is not None else _comma_list(args.files)
    known = {hunk.path for hunk in round_.hunks}
    unknown = [path for path in paths if path not in known]
    if not paths or unknown:
        shown = ", ".join(unknown) or "(empty)"
        raise FieldError(
            "files", f"names files not in round {round_.number}: {shown}; changed files: {', '.join(sorted(known))}"
        )
    return tuple(hunk.id for hunk in round_.hunks if hunk.path in set(paths))


def _view(args: argparse.Namespace) -> int:
    if (args.files is None) == (args.hunks is None):
        raise LedgerError("pass exactly one of --files or --hunks")
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    snap = ws.snapshot(ledger)
    round_ = require_current_round(ledger, snap)
    files = None if args.files is None else normalize_round_files(ws.root, _cwd(args), args.files, round_, snap)
    assigned = _view_hunk_ids(round_, args, files)
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    ctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=round_.task or ws.task, full_hunks=assigned)
    slug = view_slug(hunks=assigned) if args.hunks is not None else view_slug(files=files or ())
    view_path = Path(round_.packet_path).with_name(f"packet-{round_.number}.{slug}.md")
    _write_text(view_path, render_packet_view(ctx, assigned_hunks=assigned))
    print(view_path)
    return 0


def _scout(args: argparse.Namespace) -> int:
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


# --- flag validation ------------------------------------------------------------------------


def _require_flags(args: argparse.Namespace, flags: Mapping[str, object]) -> None:
    missing = [flag for flag, value in flags.items() if value is None]
    if missing:
        args.subparser.error(f"the following arguments are required: {', '.join(missing)}")


def _reject_flags_with_from_json(flags: Mapping[str, object]) -> None:
    used = [flag for flag, value in flags.items() if value is not None]
    if used:
        raise LedgerError(f"--from-json cannot be combined with {', '.join(used)}")


def _decode_json_bytes(data: bytes, label: str) -> str:
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise LedgerError(f"cannot read {label}: {exc}; {UTF8_SAVE_HINT}") from exc


def _load_json_entries(source: str) -> list[Any]:
    """Read and parse a non-empty JSON list of entry objects from `source` (`-` for stdin)."""
    label = "stdin" if source == "-" else source
    try:
        data = sys.stdin.buffer.read() if source == "-" else Path(source).read_bytes()
    except OSError as exc:
        raise LedgerError(f"cannot read {label}: {exc}") from exc
    text = _decode_json_bytes(data, label)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"invalid JSON in {label}: {exc}") from exc
    if not isinstance(raw, list):
        raise LedgerError(f"--from-json expects a JSON list of objects (got {type(raw).__name__})")
    if not raw:
        raise LedgerError("--from-json list must not be empty")
    return raw


def _entry_label(raw: Any, key: str) -> str | None:
    value = raw.get(key) if isinstance(raw, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


class _EntryReader:
    """Reads one `--from-json` entry and keeps every problem, so a rejected batch lists them all."""

    def __init__(self, raw: Any) -> None:
        self.fields = Fields(raw)
        self.errors: list[LedgerError] = []
        self._failed: set[str] = set()

    def read(self, key: str, get: Callable[[Fields], T], fallback: T) -> T:
        try:
            return get(self.fields)
        except FieldError as exc:
            self.errors.append(exc)
            self._failed.add(key)
            return fallback

    def failed(self, *keys: str) -> bool:
        return any(key in self._failed for key in keys)

    def reject_unknown(self) -> None:
        self.errors.extend(
            FieldError(key, "is not a known field") for key in self.fields.unknown_fields()
        )

    def finish(self, step: Callable[[list[LedgerError]], R | None]) -> R:
        """Run `step` with the problems of the semantic checks collected next to the field problems."""
        with collecting_problems() as sink:
            try:
                result = step(sink)
            except LedgerError as exc:
                sink.append(exc)
                result = None
        errors = [*self.errors, *sink]
        if not errors and result is not None:
            return result
        if not errors:
            raise LedgerError("the entry was not recorded")
        raise errors[0] if len(errors) == 1 else MultiFieldError(errors)


def _attempt(sink: list[LedgerError], get: Callable[[], T], fallback: T) -> T:
    try:
        return get()
    except LedgerError as exc:
        sink.append(exc)
        return fallback


def _entry_problem(index: int, label: str | None, exc: LedgerError) -> EntryProblem:
    field = JSON_FIELD_NAMES.get(exc.field, exc.field)
    problem = exc.problem if isinstance(exc, FieldError) else str(exc)
    return EntryProblem(index=index, label=label, field=field, problem=problem)


def _record_entries(
    command: str,
    raw_entries: Sequence[Any],
    current: Ledger,
    label_for: Callable[[int, Any], str | None],
    record_one: Callable[[int, Ledger, Any], tuple[Ledger, R]],
) -> tuple[Ledger, list[R]]:
    results: list[R] = []
    problems: list[EntryProblem] = []
    for index, raw in enumerate(raw_entries, 1):
        try:
            current, item = record_one(index, current, raw)
        except LedgerError as exc:
            label = label_for(index, raw)
            found = exc.errors if isinstance(exc, MultiFieldError) else (exc,)
            problems.extend(_entry_problem(index, label, error) for error in found)
            continue
        results.append(item)
    if problems:
        raise BatchRejected(f"{PROG} {command}", tuple(problems), len(raw_entries))
    return current, results


def _print_check_passed(count: int) -> int:
    noun = "entry" if count == 1 else "entries"
    print(f"Check passed: {count} {noun} valid; nothing was recorded.")
    return 0


def _run_single(
    ws: Workspace, check: bool, record: Callable[[Ledger], tuple[Ledger, R]], emit: Callable[[R], None]
) -> int:
    if check:
        record(load(ws.ledger_path))
        return _print_check_passed(1)
    emit(mutate(ws.ledger_path, record))
    return 0


def _run_batch(
    ws: Workspace,
    args: argparse.Namespace,
    record_batch: Callable[[Ledger], tuple[Ledger, list[R]]],
    emit: Callable[[R], None],
) -> int:
    if args.check:
        _, checked = record_batch(load(ws.ledger_path))
        return _print_check_passed(len(checked))
    recorded = mutate(ws.ledger_path, record_batch)
    consume_inbox_file(ws.ledger_path.parent, args.from_json)
    for item in recorded:
        emit(item)
    return 0


def _inbox(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    print(create_inbox_file(ws.ledger_path.parent, args.kind).resolve())
    return 0


def _scratch(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    print(create_scratch_dir(ws.ledger_path.parent).resolve())
    return 0


# --- briefs ---------------------------------------------------------------------------------


def _role_text(role: TemplateRole) -> str | None:
    """The body of the agent template for `role` (includes expanded), or None when the assets are not found."""
    try:
        return next((item.body for item in load_agent_templates(default_repo_dir()) if item.role == role), None)
    except (ExportError, OSError):
        return None


def _inline_role_text(env: Mapping[str, str]) -> bool:
    """Whether a reader on this platform needs the role text in its brief (Claude Code and Copilot load it)."""
    return brief_platform(env) in ("antigravity", "cli")


def _brief(args: argparse.Namespace) -> int:
    if (args.slot is None) == (args.verifier is None):
        raise LedgerError("pass exactly one of a slot id (your launch prompt names it) or --verifier F1,F2")
    env = os.environ
    ws = open_workspace(args.cwd)
    sync_antigravity_runs(ws, env)
    if args.verifier is not None:
        return _brief_verifier(ws, env, args.verifier)
    slot_id = str(args.slot).strip()
    if slot_generation(slot_id) is None:
        raise FieldError("slot", f"{slot_id!r} is not a slot id (like W1-1)")
    snap = ws.snapshot()
    by = caller_identity(env)

    def claim(current: Ledger) -> tuple[Ledger, tuple[SlotClaim, Round, Slot, int]]:
        round_ = require_current_round(current, snap)
        if round_.plan is None:
            raise LedgerError(
                f"round {round_.number} has no hunt plan (it was prepared by an older craftsman); "
                "run `craftsman review reset` and prepare again"
            )
        current = huntplan.plan_due_waves(current, datetime.now(timezone.utc))
        round_ = current.rounds[round_.number - 1]
        assert round_.plan is not None
        required = round_.plan.slots
        slot = round_.plan.slot(slot_id)
        if slot is None:
            if slot_generation(slot_id) == 1:
                raise LedgerError(f"unknown slot {slot_id} in round {round_.number}; slots of round {round_.number}: {_slot_list(required)}")
            raise CommandRefused(
                f"Slot {slot_id} is no longer required in round {round_.number} (the findings changed). "
                "Reply exactly that to the agent that launched you and stop."
            )
        updated, issued = add_claim(current, slot_id, by=by)
        hunters = sum(item.generation == slot.generation for item in required)
        return updated, (issued, updated.rounds[-1], slot, hunters)

    issued, round_, slot, hunters = mutate(ws.ledger_path, claim)
    ledger = load(ws.ledger_path)
    trigger = next((item for item in ledger.findings if item.id == slot.trigger), None) if slot.kind == "variant" else None
    print(f"Brief issued ({issued.token}) for {slot.id}, round {round_.number}.\n")
    print(
        huntview.hunter_brief(
            slot=slot,
            round_=round_,
            hunters=hunters,
            trigger=trigger,
            role_text=_role_text("reviewer") if _inline_role_text(env) else None,
            antigravity=brief_platform(env) == "antigravity",
        ),
        end="",
    )
    return 0


def _brief_verifier(ws: Workspace, env: Mapping[str, str], raw_ids: str) -> int:
    """The verifier's instruction for the named findings. Records nothing: a verifier's work is its verdicts."""
    ids = list(dict.fromkeys(_comma_list(raw_ids)))
    if not ids:
        raise FieldError("verifier", "must name at least one finding id (F1,F2)")
    ledger = load(ws.ledger_path)
    by_id = {finding.id: finding for finding in ledger.findings}
    unknown = [finding_id for finding_id in ids if finding_id not in by_id]
    if unknown:
        known = ", ".join(by_id) or "none"
        raise LedgerError(f"unknown finding(s): {', '.join(unknown)}; findings in the ledger: {known}")
    latest = ledger.rounds[-1] if ledger.rounds else None
    print(
        huntview.verifier_brief(
            findings=[by_id[finding_id] for finding_id in ids],
            round_=latest,
            base_sha=latest.base_sha if latest is not None else ws.base_sha(ledger),
            task=(latest.task if latest is not None else "") or ws.task,
            role_text=_role_text("verifier") if _inline_role_text(env) else None,
        ),
        end="",
    )
    return 0


# --- coverage -------------------------------------------------------------------------------


def _cover(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _cover_from_json(args)
    return _cover_one(args)


def _cover_one(args: argparse.Namespace) -> int:
    if (args.hunks is None) == (args.files is None):
        raise LedgerError("pass exactly one of --hunks or --files")
    _require_flags(args, {"--note": args.note})
    target_kind: TargetKind = "files" if args.files is not None else "hunks"
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    def files_of(round_: Round) -> Callable[[str], list[str]]:
        return lambda raw: normalize_round_files(ws.root, _cwd(args), raw, round_, snap)

    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, str]:
        round_ = require_current_round(current, snap)
        current = _store_due_waves(current)  # a wave slot of the displays can be named at once
        raw = args.files if args.files is not None else args.hunks
        targets = _cover_targets(round_, target_kind, raw, files_of(round_))
        updated, message = _record_coverage(
            current, round_, targets, args.note, target_kind=target_kind, hunter=args.hunter, by=by
        )
        return _store_due_waves(updated), message

    return _run_single(ws, bool(args.check), record, print)


def _cover_targets(
    round_: Round, kind: TargetKind, raw: str, files: Callable[[str], list[str]]
) -> list[str]:
    """The files, or the hunk ids with ranges like `H3-H7` expanded, named by one cover target list."""
    if kind == "files":
        return files(raw)
    return list(expand_hunk_spec(raw, [hunk.id for hunk in round_.hunks]))


def normalize_round_files(
    root: Path, cwd: Path, raw: str, round_: Round, snap: DiffSnapshot
) -> list[str]:
    """The files of `round_` that a comma-separated `--files` value names, as the round spells them.

    Accepts `./x`, backslashes, absolute paths and relative paths from `--cwd` or the repository
    root, and the old path of a renamed file. A name that matches no changed file is kept as typed
    (normalized), so the caller's unknown-file error shows it.
    """
    root = root.resolve()
    changed = {hunk.path for hunk in round_.hunks}
    renamed = {hunk.old_path: hunk.path for hunk in snap.hunks if hunk.old_path is not None}
    return [_round_file(root, cwd, item, changed, renamed) for item in _comma_list(raw)]


def _round_file(root: Path, cwd: Path, raw: str, changed: set[str], renamed: Mapping[str, str]) -> str:
    text = raw.replace("\\", "/")
    candidates = [Path(text)] if Path(text).is_absolute() else [cwd.resolve() / text, root / text]
    spellings: list[str] = []
    for candidate in candidates:
        try:
            spellings.append(Path(os.path.normpath(candidate)).relative_to(root).as_posix())
        except ValueError:
            continue
    for spelling in spellings:
        if spelling in changed:
            return spelling
        if spelling in renamed:
            return renamed[spelling]
    return spellings[0] if spellings else text


def _cover_entry_label(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("hunks", "files"):
        items = raw.get(key)
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            cleaned = [item.strip() for item in items if item.strip()]
            if cleaned:
                return ",".join(cleaned)
    return None


def _cover_one_from_json(
    state: Ledger, round_: Round, raw: Any, files: Callable[[str], list[str]], by: str | None
) -> tuple[Ledger, str]:
    reader = _EntryReader(raw)
    present = [key for key in ("hunks", "files") if key in raw]
    if len(present) == 2:
        raise LedgerError("pass either 'hunks' or 'files', not both")
    kind: TargetKind = "files" if present == ["files"] else "hunks"
    listed: list[str] = []
    if present:
        listed = reader.read(
            kind, lambda f: [item.strip() for item in f.texts(kind) if item.strip()], listed
        )
    else:
        reader.errors.append(FieldError("hunks", "is missing; pass 'hunks' or 'files'"))
    note = reader.read("note", lambda f: f.text("note"), "")
    hunter = reader.read("hunter", lambda f: f.text_if_present("hunter"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, str] | None:
        if reader.failed(kind, "hunter") or not present:
            return None
        targets = _attempt(
            sink, lambda: _cover_targets(round_, kind, ",".join(listed), files), None
        )
        if targets is None:  # the note is still checked, so one resend fixes both
            if not reader.failed("note"):
                check_coverage_note(one_line(note, warn=False))
            return None
        return _attempt(
            sink,
            lambda: _record_coverage(state, round_, targets, note, target_kind=kind, hunter=hunter, by=by),
            None,
        )

    return reader.finish(record)


def _cover_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {"--hunks": args.hunks, "--files": args.files, "--note": args.note, "--hunter": args.hunter}
    )
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    by = caller_identity(os.environ)

    def record_batch(current: Ledger) -> tuple[Ledger, list[str]]:
        round_ = require_current_round(current, snap)
        updated, messages = _record_entries(
            "cover",
            raw_entries,
            _store_due_waves(current),
            lambda _index, raw: _cover_entry_label(raw),
            lambda _index, state, raw: _cover_one_from_json(
                state,
                round_,
                raw,
                lambda value: normalize_round_files(ws.root, _cwd(args), value, round_, snap),
                by,
            ),
        )
        return _store_due_waves(updated), messages

    return _run_batch(ws, args, record_batch, print)


def _record_coverage(
    current: Ledger,
    round_: Round,
    targets: Sequence[str],
    note: str,
    *,
    target_kind: TargetKind,
    hunter: str | None = None,
    by: str | None = None,
) -> tuple[Ledger, str]:
    slot = _resolve_hunter(current, round_, hunter, allow_lead=False)
    if slot is not None:
        return _record_slot_coverage(current, slot, targets, note, target_kind=target_kind, by=by)
    open_ids, carried = plan_coverage(round_, targets, target_kind=target_kind)
    if not open_ids:
        left = len(uncovered_hunks(current, round_))
        return current, (
            f"Nothing to record: {', '.join(carried)} already covered in an earlier round "
            f"(carried over); {left} hunk(s) left to cover in this round."
        )
    skipped = f" ({len(carried)} carried hunk(s) skipped)" if carried else ""
    updated, _ = add_coverage(current, open_ids, note)
    left = len(uncovered_hunks(updated, round_))
    token = f"C{len(updated.coverage)}"  # the gate matches a reviewer's transcript to its records
    label = ", ".join(targets if target_kind == "files" else open_ids)
    return updated, (
        f"Coverage recorded ({token}): {label} in round {round_.number}{skipped}; "
        f"{left} hunk(s) left to cover in this round."
    )


def _record_slot_coverage(
    current: Ledger, slot: Slot, targets: Sequence[str], note: str, *, target_kind: TargetKind, by: str | None
) -> tuple[Ledger, str]:
    """Coverage tagged with `slot`: it may name only the slot's hunks, carried ones included."""
    round_ = current.rounds[-1]
    plan_coverage(round_, targets, target_kind=target_kind)  # unknown ids and files, empty lists
    owned = set(slot.hunks)
    if target_kind == "files":
        outside = [path for path in targets if not any(ref.path == path and ref.id in owned for ref in round_.hunks)]
        chosen = [ref.id for ref in round_.hunks if ref.path in set(targets) and ref.id in owned]
        field = "files"
    else:
        outside = [hunk_id for hunk_id in targets if hunk_id not in owned]
        chosen = list(dict.fromkeys(targets))
        field = "hunks"
    if outside:
        raise LedgerError(
            f"{', '.join(outside)} not part of slot {slot.id} in round {round_.number}; "
            f"the hunks of {slot.id}: {huntview.hunk_ranges(slot.hunks)}",
            field=field,
        )
    updated, _ = add_coverage(current, chosen, note, hunter=slot.id, by=by)
    digest_of = {ref.id: ref.digest for ref in round_.hunks}
    # Only what `check` could credit this caller with: records of the slot made since it was planned (a wave
    # slot ignores coverage recorded before its trigger) and, where the caller can prove who it is, by it.
    since = parse_timestamp(slot.since) if slot.since is not None else None
    seen = {
        digest
        for item in updated.coverage
        if item.round == round_.number
        and item.hunter == slot.id
        and (since is None or parse_timestamp(item.recorded_at) >= since)
        and (by is None or item.by == by)
        for digest in item.digests
    }
    left = sum(digest_of[hunk_id] not in seen for hunk_id in slot.hunks)
    token = f"C{len(updated.coverage)}"  # the gate matches a reviewer's transcript to its records
    label = ", ".join(targets if target_kind == "files" else chosen)
    return updated, (
        f"Coverage recorded ({token}) for {slot.id}: {label} in round {round_.number}; "
        f"slot {slot.id}: {left} hunk(s) left in your records (`craftsman review check` decides)."
    )


def _resolve_hunter(
    current: Ledger, round_: Round, hunter: str | None, *, allow_lead: bool
) -> Slot | None:
    """The slot a `--hunter` names, from the round's stored plan.

    No `--hunter` is an error in an enforced round that has a plan, and fine elsewhere. `lead` (the
    orchestrator, or extra work) names no slot. A slot the plan does not store is an error that lists
    the ones it does.
    """
    round_ = current.rounds[round_.number - 1]  # the caller's copy may predate an entry of the same batch
    number = round_.number
    if hunter is None:
        if round_.enforced and round_.plan is not None:
            lead = f" (or `{LEAD_HUNTER}` for your own findings)" if allow_lead else ""
            raise LedgerError(
                f"name your hunter slot with --hunter (your brief says it){lead}; "
                f"slots of round {number}: {_slot_list(round_.plan.slots) if round_.plan else 'none'}",
                field="hunter",
            )
        return None
    if hunter == LEAD_HUNTER:
        if not allow_lead:
            raise FieldError(
                "hunter", f"'{LEAD_HUNTER}' never credits coverage; name the slot that was examined"
            )
        return None
    if slot_generation(hunter) is None:
        raise FieldError("hunter", f"{hunter!r} is not a slot id (like W1-1)")
    if round_.plan is None:
        raise LedgerError(f"round {number} has no hunt plan, so there is no slot to name; leave out --hunter", field="hunter")
    slot = round_.plan.slot(hunter)
    if slot is None:
        raise LedgerError(
            f"unknown slot {hunter} in round {number}; slots of round {number}: {_slot_list(round_.plan.slots)}",
            field="hunter",
        )
    return slot


def _slot_list(slots: Sequence[Slot]) -> str:
    return ", ".join(slot.id for slot in slots) or "none"


# --- findings -------------------------------------------------------------------------------


def _add(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _add_from_json(args)
    return _add_one(args)


def _required_finding_flags(args: argparse.Namespace) -> dict[str, object]:
    return {
        "--lens": args.lens,
        "--file": args.file,
        "--line": args.line,
        "--severity": args.severity,
        "--title": args.title,
        "--scenario": args.scenario,
    }


def _add_one(args: argparse.Namespace) -> int:
    _require_flags(args, _required_finding_flags(args))
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    path = _finding_path(ws.root, _cwd(args), args.file, snap)
    lines = _source_lines(ws.root, path, args.line)

    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, Finding]:
        round_ = require_current_round(current, snap)
        current = _store_due_waves(current)  # a wave slot of the displays can be named at once
        hunter = _finding_hunter(current, round_, args.hunter)
        updated, finding = add_finding(
            current,
            lens=args.lens,
            path=path,
            line=args.line,
            severity=args.severity,
            title=args.title,
            scenario=args.scenario,
            suggestion=args.suggestion or "",
            hunter=hunter,
            by=by,
        )
        return _store_due_waves(updated), finding

    return _run_single(ws, bool(args.check), record, lambda finding: _print_recorded(finding, lines))


def _add_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {**_required_finding_flags(args), "--suggestion": args.suggestion}
    )
    _reject_flags_with_from_json({"--hunter": args.hunter})
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    cwd = _cwd(args)
    by = caller_identity(os.environ)

    def record_batch(current: Ledger) -> tuple[Ledger, list[tuple[Finding, list[str] | None]]]:
        require_current_round(current, snap)
        base_number = int(next_finding_id(current)[1:])
        updated, recorded = _record_entries(
            "add",
            raw_entries,
            _store_due_waves(current),
            lambda index, _raw: f"F{base_number + index - 1}",
            lambda _index, state, raw: _add_one_from_json(ws, cwd, snap, state, raw, by),
        )
        return _store_due_waves(updated), recorded

    return _run_batch(ws, args, record_batch, lambda pair: _print_recorded(pair[0], pair[1]))


def _add_one_from_json(
    ws: Workspace, cwd: Path, snap: DiffSnapshot, current: Ledger, raw: Any, by: str | None
) -> tuple[Ledger, tuple[Finding, list[str] | None]]:
    reader = _EntryReader(raw)
    lens = reader.read("lens", lambda f: f.choice("lens", tuple(sorted(LENS_IDS))), "intent")
    raw_file = reader.read("file", lambda f: f.text("file"), "")
    line = reader.read("line", lambda f: f.integer("line"), 1)
    severity: Severity = reader.read("severity", lambda f: f.choice("severity", SEVERITIES), "minor")
    title = reader.read("title", lambda f: f.text("title"), "")
    scenario = reader.read("scenario", lambda f: f.text("scenario"), "")
    suggestion = reader.read("suggestion", lambda f: f.text_or("suggestion", ""), "")
    named_hunter = reader.read("hunter", lambda f: f.text_if_present("hunter"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, tuple[Finding, list[str] | None]] | None:
        path = raw_file
        lines: list[str] | None = None
        if not reader.failed("file"):
            found = _attempt(sink, lambda: _finding_path(ws.root, cwd, raw_file, snap), None)
            if found is not None:
                path = found
                if not reader.failed("line"):
                    lines = _attempt(sink, lambda: _source_lines(ws.root, found, line), None)
        hunter: str | None = None
        if not reader.failed("hunter"):
            hunter = _attempt(sink, lambda: _finding_hunter(current, current.rounds[-1], named_hunter), None)
        updated, finding = add_finding(
            current,
            lens=lens,
            path=path,
            line=line,
            severity=severity,
            title=title,
            scenario=scenario,
            suggestion=suggestion,
            hunter=hunter,
            by=by,
        )
        return updated, (finding, lines)

    return reader.finish(record)


def _finding_hunter(current: Ledger, round_: Round, hunter: str | None) -> str | None:
    """The tag for a new finding: a slot of the round's stored plan, `lead` or none.

    The tag is required in an enforced round that has a plan.
    """
    if hunter == LEAD_HUNTER:
        return LEAD_HUNTER
    slot = _resolve_hunter(current, round_, hunter, allow_lead=True)
    return slot.id if slot is not None else None


def _print_recorded(finding: Finding, lines: Sequence[str] | None) -> None:
    print(f"{finding.id} recorded ({finding.severity}, {finding.lens}, {finding.path}:{finding.line})")
    print(_cited_line(finding, lines))


def _cwd(args: argparse.Namespace) -> Path:
    return Path(args.cwd or os.getcwd())


def _source_lines(root: Path, path: str, line: int) -> list[str] | None:
    """The cited file's lines (None when the diff deletes it), refusing a line past its end."""
    source = root / path
    lines = _git_lines(source) if source.is_file() else None
    if lines is not None and line > len(lines):
        raise FieldError("line", f"{line} is past the end of {path}, which has {len(lines)} lines")
    return lines


def _move(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    path = None if args.file is None else _finding_path(ws.root, _cwd(args), args.file, snap)

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
    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, tuple[Finding, tuple[Finding, ...]]]:
        updated, target, duplicates = merge_findings(
            current,
            args.target_id,
            args.duplicate_ids,
            title=args.title,
            scenario=args.scenario,
            suggestion=args.suggestion,
            by=by,
        )
        return _store_due_waves(updated), (target, duplicates)  # the merge may judge the last finding a wave waits for

    target, duplicates = mutate(ws.ledger_path, record)
    dup_ids = ", ".join(item.id for item in duplicates)
    print(f"{dup_ids} merged into {target.id} ({target.severity}, {target.path}:{target.line})")
    for item in duplicates:
        if item.verdict_log:  # the token a verifier's transcript shows it printed
            print(f"{item.id} verdict recorded ({item.verdict_log[-1].token}): dismissed (merged into {target.id})")
    return 0


def _cited_line(finding: Finding, lines: Sequence[str] | None) -> str:
    """Echo the cited source line so a wrong `--line` is noticed while the finding is fresh."""
    if lines is None:
        return (
            f"  ({finding.path} is deleted in the diff; line {finding.line} refers to the base version)"
        )
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


# --- verdicts and resolutions ---------------------------------------------------------------


def _verdict(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _verdict_from_json(args)
    return _verdict_one(args)


def _verdict_one(args: argparse.Namespace) -> int:
    _require_flags(args, {"ID": args.finding_id, "--as": args.verdict, "--evidence": args.evidence})
    ws = open_workspace(args.cwd)
    by = caller_identity(os.environ)

    def record(current: Ledger) -> tuple[Ledger, Finding]:
        updated, finding = set_verdict(current, args.finding_id, args.verdict, args.evidence, args.run, by=by)
        return _store_due_waves(updated), finding  # the verdict may be the one that calls for a wave

    return _run_single(ws, bool(args.check), record, _print_verdict)


def _verdict_one_from_json(state: Ledger, raw: Any, by: str | None) -> tuple[Ledger, Finding]:
    reader = _EntryReader(raw)
    finding_id = reader.read("id", lambda f: f.text("id"), "")
    verdict: Verdict = reader.read("as", lambda f: f.choice("as", VERDICTS), "dismissed")
    evidence = reader.read("evidence", lambda f: f.text("evidence"), "")
    run_id = reader.read("run", lambda f: f.text_if_present("run"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, Finding] | None:
        if reader.failed("id", "as", "evidence", "run"):
            return None
        return set_verdict(state, finding_id, verdict, evidence, run_id, by=by)

    return reader.finish(record)


def _verdict_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {"ID": args.finding_id, "--as": args.verdict, "--evidence": args.evidence, "--run": args.run}
    )
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    by = caller_identity(os.environ)

    def record_batch(current: Ledger) -> tuple[Ledger, list[Finding]]:
        updated, findings = _record_entries(
            "verdict",
            raw_entries,
            current,
            lambda _index, raw: _entry_label(raw, "id"),
            lambda _index, state, raw: _verdict_one_from_json(state, raw, by),
        )
        return _store_due_waves(updated), findings

    return _run_batch(ws, args, record_batch, _print_verdict)


def _print_verdict(finding: Finding) -> None:
    """`F3 verdict recorded (V7): proven (R2)`: the token is what a verifier's transcript shows it printed."""
    token = f" ({finding.verdict_log[-1].token})" if finding.verdict_log else ""
    run = f" ({finding.verdict_run})" if finding.verdict_run else ""
    print(f"{finding.id} verdict recorded{token}: {finding.verdict}{run}")


def _resolve(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
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


# --- inspection -----------------------------------------------------------------------------


def _show(args: argparse.Namespace) -> int:
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
        return _show_hunks(ws.root, latest, snap, _not_covered(ledger, slots), slots, as_json=args.json)
    if args.json:
        payload = {
            "round": _round_json(latest, snap) if latest else None,
            "coverage": _coverage_json(ledger, latest, slots) if latest else {},
            "hunks": _hunks_json(ws.root, latest, snap, _not_covered(ledger, slots)) if latest else [],
            **({} if slots is None else {"slots": _slots_json(slots)}),
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
        print()
        print("\n".join(_progress_lines(ledger, latest, slots)))
    print()
    print("\n".join(_findings_table(ledger.findings)))
    details = _finding_details(ledger.findings)
    if details:
        print()
        print("\n".join(details))
    print()
    print("\n".join(_run_lines(ledger.runs[-SHOW_RECENT_RUNS:], snap)))
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
            "round": _round_json(latest, snap) if latest else None,
            "stale": bool(latest is not None and latest.fingerprint != snap.fingerprint),
            "hunks": _hunks_json(root, latest, snap, missing) if latest else [],
            **({} if slots is None else {"slots": _slots_json(slots)}),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
        return 0
    print("\n".join(_hunk_map_lines(root, latest, snap, missing, slots)))
    return 0


def _hunk_span(new_start: int, new_len: int, old_start: int = 0, old_len: int = 0) -> tuple[int, int]:
    if new_len > 0:
        return new_start, new_start + new_len - 1
    if old_len > 0:
        return old_start, old_start + old_len - 1
    return 0, 0


def _hunks_json(
    root: Path, round_: Round, snap: DiffSnapshot, missing: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """The round's hunks; for a current round each says whether it is `covered` (when `missing` is given).

    A current round lists its open hunks, and with `missing` also the carried hunks that are in it (`"carried": true`).
    """
    if round_.fingerprint != snap.fingerprint:
        return [
            {
                "id": ref.id,
                "path": ref.path,
                "new_start": ref.new_start,
                "new_len": ref.new_len,
                "end_line": ref.new_start + ref.new_len - 1 if ref.new_len > 0 else ref.new_start,
                "carried": ref.carried,
            }
            for ref in round_.hunks
        ]
    index = WorktreeIndex(root)
    by_id = {hunk.id: hunk for hunk in snap.hunks}
    items: list[dict[str, Any]] = []
    for ref in round_.hunks:
        # A carried hunk is listed only when a slot still misses it (a carried sweep or wave slot lists it), so
        # that `hunks` and `slots` never disagree about what is left.
        if (ref.carried and (missing is None or ref.id not in missing)) or (hunk := by_id.get(ref.id)) is None:
            continue
        scope = index.hunk_scope(hunk)
        start, end = _hunk_span(hunk.new_start, hunk.new_len, hunk.old_start, hunk.old_len)
        items.append(
            {
                "id": ref.id,
                "path": ref.path,
                "new_start": ref.new_start,
                "new_len": ref.new_len,
                "start_line": start,
                "end_line": end,
                "carried": ref.carried,
                "status": hunk.status,
                "scope": scope.name if scope is not None and scope.kind != "block" else None,
                **({} if missing is None else {"covered": ref.id not in missing}),
            }
        )
    return items


def _format_span(path: str, start: int, end: int, has_range: bool, *, bare_zero: bool = False) -> str:
    if has_range:
        return f"{path}:{start}-{end}"
    return path if bare_zero else f"{path}:{start}"


def _hunk_map_lines(
    root: Path, round_: Round, snap: DiffSnapshot, missing: Sequence[str], slots: Sequence[SlotStatus] | None = None
) -> list[str]:
    """One compact `H#  path:start-end  scope|status` line per hunk still to cover, then `N left`.

    A stale round lists its HunkRef fields instead: its coverage no longer matches the code. A round judged
    by slots (`slots` is given) lists, per slot that is not done, the hunks that slot still misses.
    """
    entries = _hunks_json(root, round_, snap)
    if round_.fingerprint != snap.fingerprint:
        # What the stale round recorded, not what is covered now: nothing here says "open".
        lines = [
            f"{e['id']}  {_format_span(e['path'], e['new_start'], e['end_line'], e['new_len'] > 0)}  "
            f"[{'carried' if e['carried'] else f'round {round_.number}'}]"
            for e in entries
        ]
        lines.append(huntview.STALE_NOTE)
        return lines
    by_id = {e["id"]: e for e in entries}

    def hunk_line(hunk_id: str) -> str:
        entry = by_id.get(hunk_id)
        if entry is None or entry["carried"]:  # a carried hunk that a slot lists: shown as the round recorded it
            ref = next(ref for ref in round_.hunks if ref.id == hunk_id)
            end = ref.new_start + ref.new_len - 1 if ref.new_len > 0 else ref.new_start
            return f"{hunk_id}  {_format_span(ref.path, ref.new_start, end, ref.new_len > 0)}  [carried]"
        span = _format_span(entry["path"], entry["start_line"], entry["end_line"], entry["end_line"] > 0, bare_zero=True)
        return f"{hunk_id}  {span}  {entry['scope'] or entry['status']}"

    if slots is not None:
        pending = [status for status in slots if status.state != "done"]
        lines = [
            line
            for status in pending
            for line in (
                f"{status.slot.id} [{huntview.STATE_LABELS[status.state]}] still misses "
                f"{huntview.hunk_ranges(status.missing)}:",
                *(f"  {hunk_line(hunk_id)}" for hunk_id in sorted(status.missing, key=lambda item: int(item[1:]))),
            )
        ]
        return [*lines, f"{len(pending)} slot(s) left to cover" if pending else "0 left to cover: every slot is done"]
    lines = [hunk_line(e["id"]) for e in entries if e["id"] in missing]
    if not entries:
        return ["(no open hunks)"]
    return [*lines, f"{len(lines)} left to cover" if lines else "0 left to cover: every hunk is covered"]


def _round_line(round_: Round, snap: DiffSnapshot) -> str:
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
    if round_.fingerprint == snap.fingerprint:
        freshness = "current"
    elif snap.is_empty:
        freshness = f"the work tree matches base {snap.base_sha[:12]}: nothing left to review"
    else:
        freshness = "stale: the code changed, run `craftsman review prepare`"
    return (
        f"Round {round_.number}{' (deep)' if round_.deep else ''} prepared {round_.prepared_at} · "
        f"{len(round_.hunks)} hunks ({carried} carried) · {freshness}"
    )


def _round_json(round_: Round, snap: DiffSnapshot) -> dict[str, Any]:
    return {
        "number": round_.number,
        "deep": round_.deep,
        "prepared_at": round_.prepared_at,
        "current": round_.fingerprint == snap.fingerprint,
        "hunks": len(round_.hunks),
        "carried": len(round_.hunks) - len(round_.open_hunk_ids),
        "packet": round_.packet_path,
    }


def _coverage_json(ledger: Ledger, round_: Round, slots: Sequence[SlotStatus] | None = None) -> dict[str, Any]:
    """Hunk coverage of `round_`; for a round judged by `slots`, the slots done and the hunks they still miss."""
    if slots is not None:
        return {
            "unit": "slots",
            "done": sum(status.state == "done" for status in slots),
            "total": len(slots),
            "missing": _slot_missing(slots),
        }
    missing = uncovered_hunks(ledger, round_)
    total = len(round_.open_hunk_ids)
    return {"covered": total - len(missing), "total": total, "missing": missing}


def _coverage_line(ledger: Ledger, round_: Round) -> str:
    entry = _coverage_json(ledger, round_)
    missing = f" (missing {','.join(entry['missing'])})" if entry["missing"] else ""
    return f"  {entry['covered']}/{entry['total']} hunks{missing}"


def _slot_missing(slots: Sequence[SlotStatus]) -> list[str]:
    """The hunks that some slot still misses, in hunk order."""
    return sorted({hunk_id for status in slots for hunk_id in status.missing}, key=lambda hunk_id: int(hunk_id[1:]))


def _not_covered(ledger: Ledger, slots: Sequence[SlotStatus] | None) -> list[str]:
    """The hunks of the latest round that are not covered, by the measure that round is judged by.

    `slots` is `hunt_slots(ledger)`. A round judged by slots (enforced, with a plan) lacks the hunks that some
    slot misses: its hunk coverage proves nothing, since only a slot's own vouched coverage counts. Any other
    round lacks the hunks that no coverage record names.
    """
    if not ledger.rounds:
        return []
    return uncovered_hunks(ledger, ledger.rounds[-1]) if slots is None else _slot_missing(slots)


def _progress_lines(ledger: Ledger, round_: Round, slots: Sequence[SlotStatus] | None) -> list[str]:
    """How far the latest round is, as the gate counts it (see `_not_covered`)."""
    if slots is None:
        return ["Coverage (covered/total non-carried hunks):", _coverage_line(ledger, round_)]
    missing = ", ".join(_slot_missing(slots)) or "none"
    return [hunt_line(slots), f"Hunks still missing from some slot: {missing}"]


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


def _finding_details(findings: Sequence[Finding]) -> list[str]:
    """Each finding's scenario and verdict evidence, so a verifier needs no `--json` call."""
    lines: list[str] = []
    for finding in findings:
        lines.append(f"{finding.id} scenario (lens {finding.lens}): {_flat(finding.scenario)}")
        if finding.verdict is not None:
            run = f", {finding.verdict_run}" if finding.verdict_run else ""
            lines.append(f"{finding.id} evidence ({finding.verdict}{run}): {_flat(finding.verdict_evidence)}")
    return lines


def _flat(text: str) -> str:
    """Prose on one line, unable to pass for the output of a recording command (see `defang_tokens`)."""
    return " ".join(defang_tokens(text).split())


def _cell(text: str) -> str:
    """Text safe for one Markdown table cell: pipes escaped, line breaks flattened, record shapes defanged."""
    return defang_tokens(inline_md(text, table=True))


def _prose(text: str) -> str:
    """Stored prose inline in Markdown, unable to pass for the output of a recording command."""
    return defang_tokens(inline_md(text))


def _run_lines(runs: Sequence[Run], snap: DiffSnapshot) -> list[str]:
    if not runs:
        return ["Runs: none recorded."]
    lines = [f"Runs (last {len(runs)}):"]
    for run in runs:
        freshness = "current" if run.fingerprint == snap.fingerprint else "stale"
        lines.append(
            f"- {run.id} {run.kind} {run.exit_text} · {run.duration_s:.1f}s · {freshness} · "
            f"`{run.short_command}`"
        )
    return lines


def _evaluate(
    ws: Workspace, *, review_only: bool
) -> tuple[Ledger, DiffSnapshot, bool, GateReport]:
    if review_only and ws.state is not None:
        raise LedgerError(
            f"--review-only is not allowed: this checkout has an active /craftsman gate ({_gate_label(ws.state)}); "
            "the full gate applies to every review command here (run `craftsman review check` without --review-only)"
        )
    ledger = _with_due_waves(ws)
    deep = effective_deep(ledger, ws.state is not None and ws.state.deep)
    snap = ws.snapshot(ledger)
    policy = GatePolicy(deep=deep, isolation_evidence=ws.isolation_evidence(ledger))
    return ledger, snap, deep, evaluate(ledger, snap, policy, review_only=review_only)


def _store_due_waves(ledger: Ledger, now: datetime | None = None) -> Ledger:
    """`ledger` with the waves that are due stored in the plan of its latest round; the same object when none is."""
    if not ledger.rounds or ledger.rounds[-1].plan is None:
        return ledger
    return huntplan.plan_due_waves(ledger, datetime.now(timezone.utc) if now is None else now)


def _with_due_waves(ws: Workspace) -> Ledger:
    """The ledger with the waves that are due stored in the plan of its latest round (under the lock, only if any are)."""
    now = datetime.now(timezone.utc)
    ledger = load(ws.ledger_path)
    if _store_due_waves(ledger, now) is ledger:
        return ledger  # the usual case: take no lock

    def plan(current: Ledger) -> tuple[Ledger, Ledger]:
        planned = _store_due_waves(current, now)
        return planned, planned

    return mutate(ws.ledger_path, plan)


ABANDONED_BANNER = "REVIEW ABANDONED - NOT COMPLETE"
ABANDONED_NOTE = (
    "the Stop hook gave up on this review after repeated reminders; do not present it as complete"
)


def _abandoned(ws: Workspace, ledger: Ledger, report: GateReport) -> bool:
    """Whether the Stop hook gave up on this review and it is still not complete.

    The marker outlives the review it guarded, so its `abandoned_at` counts only when it is not older than the
    latest round (a `reset` or a newer round starts afresh) and the report has not passed since.
    """
    marker = read_marker(ws.ledger_path)
    if marker is None or marker.abandoned_at is None or not ledger.rounds or report.passed:
        return False
    try:
        return parse_timestamp(marker.abandoned_at) >= parse_timestamp(ledger.rounds[-1].prepared_at)
    except ValueError:
        return False  # a stamp that cannot be read proves nothing


def _check(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    sync_antigravity_runs(ws, os.environ)
    ledger, _, _, report = _evaluate(ws, review_only=args.review_only)
    abandoned = _abandoned(ws, ledger, report)
    if args.json:
        print(json.dumps(_report_json(report, ledger, abandoned=abandoned), indent=2, ensure_ascii=False))
    else:
        if abandoned:
            print(f"**{ABANDONED_BANNER}**: {ABANDONED_NOTE}.\n")
        print(render_report(report))
        if report.slots:
            print("\n**Hunt slots**")
            print("\n".join(huntview.plan_lines(report.slots, with_state=True)))
        if args.review_only:
            _warn_new_out_of_scope(ws, ledger)
    return 0 if report.passed else 1


def _slots_json(slots: Sequence[SlotStatus]) -> list[dict[str, Any]]:
    return [
        {
            "id": status.slot.id,
            "state": status.state,
            "lenses": list(status.slot.lenses),
            "hunks": list(status.slot.hunks),
            "missing": list(status.missing),
        }
        for status in slots
    ]


def _report_json(report: GateReport, ledger: Ledger, *, abandoned: bool = False) -> dict[str, Any]:
    latest = ledger.rounds[-1] if ledger.rounds else None
    return {
        "passed": report.passed,
        "partial": report.partial,
        "self_reviewed": report.self_reviewed,
        "enforced": report.enforced,
        "platform": latest.platform if latest is not None else "cli",
        "abandoned": abandoned,
        "slots": _slots_json(report.slots),
        "issues": [{"code": issue.code, "message": issue.message} for issue in report.issues],
        "warnings": list(report.warnings),
        "summary": report.summary,
    }


def _report(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    if args.partial and not args.review_only:
        if ws.state is not None:
            raise LedgerError(
                f"--partial is for a review-only report, which is refused here: this checkout has an active "
                f"/craftsman gate ({_gate_label(ws.state)}); run `craftsman review report`"
            )
        raise LedgerError(
            "--partial requires --review-only; pass `craftsman review report --review-only --partial`"
        )
    sync_antigravity_runs(ws, os.environ)
    ledger, snap, deep, report = _evaluate(ws, review_only=args.review_only)
    if args.partial:
        _require_hunters_launched(ws, ledger)
        report = replace(report, partial=True)
    abandoned = _abandoned(ws, ledger, report)
    groups = _finding_groups(ledger.findings)
    runs = passing_runs(ledger, snap.fingerprint)
    slots = hunt_slots(ledger)
    if args.json:
        payload = {
            **_report_json(report, ledger, abandoned=abandoned),
            "deep": deep,
            "rounds": len(ledger.rounds),
            "base_sha": snap.base_sha,
            "diff": {"files": len(snap.files), "added": snap.added, "deleted": snap.deleted},
            "not_covered": _not_covered(ledger, slots),
            "findings": {name: [f.to_json() for f in items] for name, items in groups.items()},
            "runs": [run.to_json() for run in runs],
            "waived_runs": [run.to_json() for run in ledger.runs if run.waiver is not None],
            "agent_usage": usage_json(ledger),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(_report_lines(ledger, snap, deep, report, groups, runs, slots, abandoned=abandoned)))
        _warn_new_out_of_scope(ws, ledger)
    if args.review_only and report.passed:
        # Only a review that passed is over. A report of an unfinished one ends nothing: pruning would delete the
        # repro scripts and inbox files of work in progress, and ending the guard would let a session out of an
        # enforced review (`--partial` after one hunter is not a way out; the guard has its own exits).
        _end_guard_of_caller(ws, os.environ)
        prune_all_temp_dirs(ws.ledger_path.parent)
    return 0


def _end_guard_of_caller(ws: Workspace, env: Mapping[str, str]) -> None:
    """End the review guard if it holds the session that runs this command, not another live session's.

    Only Claude Code exports a session id to the commands it runs. A command that cannot tell which
    session it serves can only be the one the single guard of this ledger holds, so it ends it.
    """
    caller = cli_caller(None, env)
    end_review_guard(ws.ledger_path, caller if caller.session_id is not None else None)


def _require_hunters_launched(ws: Workspace, ledger: Ledger) -> None:
    """A partial report needs a round, and one that is enforced needs at least one hunter launched in it (a claim or a start/stop).

    Without a round nothing was prepared, let alone reviewed. Otherwise nothing was reviewed independently and
    `--partial` would only be a way out of the review.

    A bare claim counts although the orchestrator could fetch a brief itself: a session whose hooks recorded
    no start event must not be trapped, and the Phase F PreToolUse hook denies `brief` to the main session.
    """
    latest = ledger.rounds[-1] if ledger.rounds else None
    if latest is None:
        raise CommandRefused(
            "No review round yet: run `craftsman review prepare` and launch the hunters it lists."
        )
    if not latest.enforced or latest.plan is None:
        return
    prepared = parse_timestamp(latest.prepared_at)
    if any(claim.round == latest.number for claim in ledger.claims) or any(
        run.role == "reviewer" and run.started >= prepared for run in agent_runs(ledger)
    ):
        return
    pending = [
        status.slot.id
        for status in slot_statuses(ledger, latest, datetime.now(timezone.utc))
        if status.state != "done"
    ]
    launch = (
        "\n".join(huntview.launch_lines(pending, check=_check_command(ws)))
        if pending
        else "Run `craftsman review prepare`."
    )
    raise CommandRefused(
        f"no Craftsman hunter was launched for round {latest.number}, so a partial report would describe a diff "
        f"nobody reviewed. Launch the hunters first.\n{launch}"
    )


def _warn_new_out_of_scope(ws: Workspace, ledger: Ledger) -> None:
    """Warn about changed files outside the scope that were not already changed when it was frozen.

    The baseline is the first scoped round: a later round records whatever the session changed
    in between as its own baseline, which would hide exactly the files this warning is for.
    """
    scoped = [round_ for round_ in ledger.rounds if round_.scope is not None]
    latest = ledger.rounds[-1] if ledger.rounds else None
    if not scoped or latest is None or latest.scope is None:
        return
    full_now = snapshot(ws.root, latest.base_sha)
    current_out = set(full_now.files) - set(latest.scope)
    new_out = sorted(current_out - set(scoped[0].out_of_scope))
    if new_out:
        paths = ", ".join(new_out)
        print(
            f"Warning: Files created or changed during this review outside its scope: {paths}\n"
            "delete or revert them if this session made them"
        )


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


def _report_status_line(report: GateReport) -> str:
    subject = "Review" if report.partial else report.subject.capitalize()
    line = f"- {subject}: {report.status_label}"
    if report.passed:
        return line
    # The check that judges what the report just judged (a review-only report is never made under a gate).
    check = (
        huntview.REVIEW_ONLY_CHECK_COMMAND if report.review_only or report.partial else huntview.CHECK_COMMAND
    )
    return f"{line} — {report.issue_codes}; see `{check}`"


def _not_covered_lines(
    ledger: Ledger, snap: DiffSnapshot, report: GateReport, slots: Sequence[SlotStatus] | None
) -> list[str]:
    missing = _not_covered(ledger, slots)
    if missing:
        what = "Hunks" if slots is None else "Hunks still missing from some slot"
        return ["", "## Not covered", f"- {what}: {', '.join(missing)}"]
    if report.partial and not ledger.rounds and not snap.is_empty:
        return ["", "## Not covered", "- No review round prepared yet."]
    return []


def _hunt_report_lines(report: GateReport) -> list[str]:
    if not report.slots:
        return []
    stale = [huntview.STALE_NOTE] if any(issue.code == "stale-round" for issue in report.issues) else []
    return [
        "",
        "## Hunt",
        hunt_line(report.slots),
        *stale,
        "```",
        *huntview.plan_lines(report.slots, with_state=True),
        "```",
    ]


def _report_finding_lines(ledger: Ledger, groups: dict[str, list[Finding]]) -> list[str]:
    if not ledger.findings:
        return ["No findings recorded."]
    detail: dict[str, Callable[[Finding], str]] = {
        "fixed": lambda f: f"Fix: {_prose(f.resolution_note)}",
        "accepted": lambda f: f"Accepted because: {_prose(f.resolution_note)}",
        "open": lambda f: (
            f"Verdict: {f.verdict}" if f.verdict is not None else "Verdict: none yet (unverified)"
        ),
        "question": lambda f: f"Question: {_prose(f.verdict_evidence)}",
        "dismissed": lambda f: f"Dismissed: {_prose(f.verdict_evidence)}",
    }
    lines: list[str] = []
    for name, items in groups.items():
        if items:
            lines.append(f"**{name.capitalize()} ({len(items)})**")
            lines.extend(
                f"- {f.id} · {f.severity} · `{f.path}:{f.line}` — {_prose(f.title)}. {detail[name](f)}"
                for f in items
            )
    return lines


def _not_assessed_lines(report: GateReport) -> list[str]:
    if report.self_reviewed:
        isolation = f"Reviewer isolation status: {SELF_REVIEWED_BANNER}."
    elif report.passed:
        isolation = "Reviewer isolation status: verified via subagent stop events in the ledger."
    else:
        isolation = "Reviewer isolation: not established yet (review incomplete)."
    return [
        "",
        "## Not assessed by the gate",
        "- Business requirements fit and domain behavior not exercised by recorded runs.",
        "- Naming quality and domain clarity.",
        "- Architecture and invariants outside the changed files.",
        f"- {isolation}",
    ]


def _usage_section(ledger: Ledger) -> list[str]:
    lines = usage_lines(ledger)
    return ["", "## Agent usage", *lines] if lines else []


def _waived_run_lines(ledger: Ledger) -> list[str]:
    waived = [(run, run.waiver) for run in ledger.runs if run.waiver is not None]
    if not waived:
        return []
    return [
        "",
        "## Waived runs",
        *(
            f"- {run.id} `{run.command}` ({run.kind}, exit {run.exit_code}) — {_prose(waiver.reason)}"
            for run, waiver in waived
        ),
    ]


def _report_lines(
    ledger: Ledger,
    snap: DiffSnapshot,
    deep: bool,
    report: GateReport,
    groups: dict[str, list[Finding]],
    runs: Sequence[Run],
    slots: Sequence[SlotStatus] | None,
    *,
    abandoned: bool = False,
) -> list[str]:
    banner = [f"**{SELF_REVIEWED_BANNER}**"] if report.self_reviewed else []
    if abandoned:
        banner.append(f"**{ABANDONED_BANNER}**: {ABANDONED_NOTE}.")
    run_lines = [
        f"- {run.id} `{run.command}` ({run.kind}) → exit {run.exit_code} in {run.duration_s:.1f}s"
        for run in runs
    ] or ["No passing test or e2e run at the final code."]
    warning_lines = ["", "## Warnings", *(f"- {w}" for w in report.warnings)] if report.warnings else []
    return [
        "# Craftsman review report",
        *banner,
        f"- Base `{snap.base_sha[:7]}` · {'deep · ' if deep else ''}{len(ledger.rounds)} review round(s) · "
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines",
        _report_status_line(report),
        *_hunt_report_lines(report),
        *_not_covered_lines(ledger, snap, report, slots),
        "",
        "## Findings",
        *_report_finding_lines(ledger, groups),
        "",
        "## Verification at the final code",
        *run_lines,
        *_waived_run_lines(ledger),
        *_usage_section(ledger),
        *warning_lines,
        *_not_assessed_lines(report),
    ]


def _reset(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    archived = archive_ledger(ws.ledger_path)
    prune_all_temp_dirs(ws.ledger_path.parent)
    if not archived:
        print("Nothing to archive: no ledger or review packets yet.")
    for path in archived:
        print(f"Archived to {path}")
    return 0


# --- argument parsing -----------------------------------------------------------------------


def literal_choice(choices: Sequence[S]) -> Callable[[str], S]:
    """argparse `type=` converter that narrows a string to one of the `Literal` choices."""

    def parse(value: str) -> S:
        chosen = as_choice(value, choices)
        if chosen is not None:
            return chosen
        raise argparse.ArgumentTypeError(f"invalid choice {value!r} (choose from {', '.join(choices)})")

    return parse


def _add_prepare_parser(sub: Any, common: argparse.ArgumentParser) -> None:
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
    prepare.add_argument(
        "--deep",
        action="store_true",
        help="Make the round deep: every lens is suggested (inside a gate it raises the gate to deep)",
    )
    prepare.add_argument("--base", help="Base commit (default: the active task's base or HEAD)")
    prepare.add_argument("--task", help="One-line description of what the change is for")
    prepare.add_argument(
        "--task-file",
        metavar="PATH",
        help="Read the description of what the change is for from a file (use - for stdin; at most 4000 characters)",
    )
    prepare.add_argument(
        "--paths", help="Comma-separated file or directory paths to freeze as the review scope"
    )
    prepare.add_argument(
        "--add-paths",
        help="Comma-separated file or directory paths to add to an existing review scope (e.g. files touched in --fix)",
    )
    prepare.add_argument(
        "--staged",
        action="store_true",
        help="Freeze the review scope to paths staged in the index (content still comes from the work tree)",
    )
    prepare.add_argument(
        "--tracked",
        action="store_true",
        help="Freeze the review scope to tracked files changed since the base (omit untracked files)",
    )
    prepare.set_defaults(handler=_prepare)


def _add_view_and_cover_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    view = sub.add_parser("view", parents=[common], help="Build a packet view of some files or hunks")
    view.add_argument("--files", help="Comma-separated changed file paths, e.g. pkg/a.py,pkg/b.py")
    view.add_argument("--hunks", help="Comma-separated hunk ids or ranges, e.g. H3-H17")
    view.set_defaults(handler=_view)

    scout = sub.add_parser(
        "scout",
        parents=[common],
        help="Base version, callers and tests for a hunk, a location or a symbol",
        description=(
            "Grounded context in at most 12,000 characters, no opinions: for hunks (H3, H3-H5, H3,H7) "
            "and for path:line, the base version of the enclosing scope, up to 3 callers per symbol "
            "it defines and up to 3 tests; for a symbol name, its definitions, references and tests."
        ),
    )
    scout.add_argument("target", metavar="TARGET", help="H3, H3-H5, H3,H7, path:line or a symbol name")
    scout.add_argument(
        "--base", help="Revision the base version comes from (default: the round's base, else HEAD)"
    )
    scout.set_defaults(handler=_scout)

    cover = sub.add_parser("cover", parents=[common], help="Record that hunks or files were reviewed")
    cover.add_argument("--hunks", help="Comma-separated hunk ids or ranges, e.g. H1,H3-H7")
    cover.add_argument(
        "--files", help="Comma-separated file paths; covers every open hunk of those files"
    )
    cover.add_argument("--note", help="What you examined and tried (one line, >= 20 characters)")
    cover.add_argument(
        "--hunter",
        metavar="SLOT",
        help="The hunt-plan slot this coverage answers (your brief names it; required in an enforced review)",
    )
    cover.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"hunks"|"files", "note", "hunter"?} objects (use - for stdin)',
    )
    cover.set_defaults(handler=_cover, subparser=cover)

    brief = sub.add_parser(
        "brief",
        parents=[common],
        help="The whole instruction of one hunter slot, or of a verifier",
        description=(
            "`brief SLOT` records that you took the slot, then prints your slice, lenses and the commands that "
            "record your work. `brief --verifier F1,F2` prints a verifier's instruction and records nothing."
        ),
    )
    brief.add_argument("slot", metavar="SLOT", nargs="?", help="Slot id from the hunt plan, e.g. W1-2")
    brief.add_argument("--verifier", metavar="IDS", help="Comma-separated finding ids to verify, e.g. F1,F2")
    brief.set_defaults(handler=_brief)


def _add_finding_parsers(sub: Any, common: argparse.ArgumentParser, lenses: Sequence[str]) -> None:
    add = sub.add_parser("add", parents=[common], help="Record a finding")
    add.add_argument("--lens", choices=lenses)
    add.add_argument("--file")
    add.add_argument("--line", type=int)
    add.add_argument("--severity", type=literal_choice(SEVERITIES))
    add.add_argument("--title")
    add.add_argument("--scenario", help="Trigger -> wrong outcome")
    add.add_argument("--suggestion", default=None)
    add.add_argument(
        "--hunter",
        metavar="SLOT",
        help="The hunt-plan slot that found it, or `lead` for the orchestrator's own (required in an enforced review)",
    )
    add.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"lens", "file", "line", "severity", "title", "scenario", "suggestion"?, "hunter"?} objects (use - for stdin)',
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
            "findings, even on the same line, file, or category (for example, a missing `--json` "
            "flag in `craftsman status` and a stale README example are two separate findings, not "
            "one 'housekeeping' finding)."
        ),
    )
    merge.add_argument("target_id", metavar="TARGET_ID", help="Finding id to keep (e.g. F1)")
    merge.add_argument(
        "duplicate_ids", metavar="DUP_ID", nargs="+", help="Duplicate finding ids to fold into TARGET_ID"
    )
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
    sub.choices["show"].add_argument(
        "--hunks",
        action="store_true",
        help="Print the hunks still to cover (H# -> file:lines and scope or status) and how many are left",
    )
    for name in ("check", "report"):
        sub.choices[name].add_argument(
            "--review-only",
            action="store_true",
            help=(
                "Check only review completeness (round, coverage, verdicts), without requiring fixes or test runs; "
                "refused while a /craftsman gate is active in the checkout"
            ),
        )
    sub.choices["report"].add_argument(
        "--partial",
        action="store_true",
        help=(
            "Print a partial review-only report when some hunks or verdicts could not be completed. It ends "
            "nothing: the review guard stays armed and scratch/inbox files are kept until the review passes"
        ),
    )
    reset = sub.add_parser("reset", parents=[common], help="Archive the ledger and packets")
    reset.set_defaults(handler=_reset)


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", help="Directory inside the repository (default: current)")
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Record and inspect code review evidence in the Craftsman ledger.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lenses = sorted(LENS_IDS)
    _add_prepare_parser(sub, common)
    _add_view_and_cover_parsers(sub, common)
    _add_finding_parsers(sub, common, lenses)
    _add_merge_and_verdict_parsers(sub, common)
    _add_resolution_and_status_parsers(sub, common)
    _add_inspection_parsers(sub, common)
    for name in ("cover", "add", "verdict"):
        sub.choices[name].add_argument(
            "--check",
            action="store_true",
            help="Validate without recording to the ledger or consuming an inbox file",
        )
    inbox = sub.add_parser(
        "inbox",
        parents=[common],
        help="Create a unique managed JSON file in <git-dir>/craftsman/inbox/ for --from-json",
    )
    inbox.add_argument("kind", choices=INBOX_KINDS, help="Batch command the file is for")
    inbox.set_defaults(handler=_inbox)
    scratch = sub.add_parser(
        "scratch",
        parents=[common],
        help="Create a unique managed scratch directory in <git-dir>/craftsman/scratch/ for repro scripts",
    )
    scratch.set_defaults(handler=_scratch)
    # Prefix matching would let `--lens` stand in for `--lenses` and similar near-misses.
    for command in sub.choices.values():
        command.allow_abbrev = False
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result: int = args.handler(args)
    except BatchRejected as exc:
        sys.stderr.write(f"{format_batch_rejection(exc)}\n")
        return 1
    except CommandRefused as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return exc.exit_code
    except (LedgerError, DiffScanError, GateStateError) as exc:
        sys.stderr.write(f"{PROG}: error: {describe_error(exc)}\n")
        return 1
    return result


def format_batch_rejection(exc: BatchRejected) -> str:
    """Render every rejected batch entry without the `error:` prefix, capped at 20 lines."""
    lines = [
        str(exc),
        "Fix all of them, then send the corrected list once:",
    ]
    shown = exc.problems[:MAX_REPORTED_BATCH_PROBLEMS]
    for item in shown:
        head = f"entry {item.index} ({item.label})" if item.label else f"entry {item.index}"
        detail = f"{item.field}: {item.problem}" if item.field else item.problem
        lines.append(f"  {head}: {detail}")
    hidden = len(exc.problems) - len(shown)
    if hidden > 0:
        lines.append(f"  ... and {hidden} more")
    return "\n".join(lines)


def describe_error(exc: LedgerError | DiffScanError | GateStateError) -> str:
    """The error message, naming the command-line flag when a flag's value was rejected."""
    if isinstance(exc, FieldError) and exc.field in FIELD_FLAGS:
        return f"{FIELD_FLAGS[exc.field]} {exc.problem}"
    return str(exc)
