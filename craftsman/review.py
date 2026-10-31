"""`craftsman review`: prepare review rounds and record or inspect evidence in the ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from craftsman.diffscan import (
    IGNORE_FILE,
    DiffScanError,
    DiffSnapshot,
    Hunk,
    WorktreeIndex,
    detect_signals,
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
    effective_tier,
    packet_path,
    prune_inbox_files,
    prune_round_files,
)
from craftsman.gate import GateState, GateStateError, Stage, current_tier, state_path_for, update_state
from craftsman.gatecheck import (
    SELF_REVIEWED_BANNER,
    GatePolicy,
    GateReport,
    acks_in_force,
    evaluate,
    has_unresolved_diff_size,
    passing_runs,
    render_report,
    require_current_round,
)
from craftsman.ledger import (
    FIXED_SCOPE_ERROR,
    TargetKind,
    ack_signal,
    add_fact,
    add_finding,
    add_lenses,
    add_multi_coverage,
    coverage_units_for_targets,
    file_sha256,
    load,
    merge_findings,
    mutate,
    next_finding_id,
    open_round,
    open_units,
    relocate_finding,
    resolve,
    set_verdict,
    uncovered_by_lens,
    waive_run,
)
from craftsman.lenses import (
    LENS_IDS,
    REVIEW_ONLY_QUICK_MAX_LINES,
    REVIEWER_GROUPS,
    TIERS,
    LensPick,
    Tier,
    review_only_tier,
    select_lenses,
    strictest_tier,
)
from craftsman.packet import (
    PacketContext,
    collect_context,
    expand_hunk_spec,
    parse_shard_spec,
    render_packet,
    render_packet_view,
    select_shard,
    suggested_shard_count,
)
from craftsman.quality import MAX_PRODUCTION_ADDED_LINES, collect_quality_signals, production_diff_growth
from craftsman.records import (
    DESIGN_LENS,
    LENS_ADDITION_EXAMPLE,
    RESOLUTIONS,
    SEVERITIES,
    SWEEP_LENS,
    VERDICTS,
    BatchRejected,
    EntryProblem,
    Fact,
    FieldError,
    Fields,
    Finding,
    Ledger,
    LedgerError,
    Round,
    Run,
    SignalAck,
    SourceRef,
    as_choice,
    require_lens_addition,
)
from craftsman.workspace import Workspace, open_workspace

PROG = "craftsman review"
SHOW_RECENT_RUNS = 5
CITED_LINE_CHARS = 120
MAX_REPORTED_BATCH_PROBLEMS = 20
MAX_UNSCANNED_PATHS_SHOWN = 8
UTF8_SAVE_HINT = "save it as UTF-8 (PowerShell: Set-Content -Encoding utf8)"
# Ledger record fields that come straight from a command-line flag, for error messages.
FIELD_FLAGS = {
    "lens": "--lens",
    "lenses": "--lenses",
    "hunks": "--hunks",
    "shard": "--shard",
    "files": "--files",
    "paths": "--paths",
    "add_paths": "--add-paths",
    "add_lens": "--add-lens",
    "source": "--source",
    "text": "--text",
    "path": "--file",
    "line": "--line",
    "severity": "--severity",
    "title": "--title",
    "scenario": "--scenario",
    "signal_id": "--signal",
    "note": "--note",
    "verdict": "--as",
    "verdict_evidence": "--evidence",
    "verdict_run": "--run",
    "resolution": "--as",
    "resolution_note": "--note",
    "reason": "--reason",
}
S = TypeVar("S", bound=str)
R = TypeVar("R")
JSON_FIELD_NAMES = {
    "path": "file",
    "verdict": "as",
    "verdict_evidence": "evidence",
    "verdict_run": "run",
    "signal_id": "signal",
}


def _auto_tier_reason(snap: DiffSnapshot) -> str:
    if snap.docs_only:
        return "docs-only change"
    if snap.skipped:
        return f"skipped binary or oversized file ({snap.skipped[0]})"
    changed = snap.added + snap.deleted
    op = "<=" if changed <= REVIEW_ONLY_QUICK_MAX_LINES else ">"
    return f"{changed} changed lines {op} {REVIEW_ONLY_QUICK_MAX_LINES}"


def _size_tier(snap: DiffSnapshot) -> Tier:
    return review_only_tier(
        snap.added + snap.deleted, has_skipped=bool(snap.skipped), docs_only=snap.docs_only
    )


def resolve_tier(ws: Workspace, ledger: Ledger, explicit: Tier | None, snap: DiffSnapshot) -> Tier:
    """`--tier`, else the active task's gear, else size-based review-only tier (never lowered)."""
    if explicit is not None:
        return explicit
    if ws.state is not None:
        return current_tier(ws.state, ws.root)
    auto = _size_tier(snap)
    return strictest_tier(ledger.rounds[-1].tier, auto) if ledger.rounds else auto


def _tier_reason(ws: Workspace, ledger: Ledger, explicit: Tier | None, snap: DiffSnapshot) -> str:
    if explicit is not None:
        return "explicit --tier"
    if ws.state is not None:
        return "active /craftsman gate"
    auto = _size_tier(snap)
    reason = _auto_tier_reason(snap)
    if not ledger.rounds:
        return reason
    prev = ledger.rounds[-1].tier
    if TIERS.index(prev) > TIERS.index(auto):
        return f"kept from round {ledger.rounds[-1].number} ({reason})"
    if TIERS.index(auto) > TIERS.index(prev):
        return f"raised from {prev} ({reason})"
    return reason


def evaluation_tier(ws: Workspace, ledger: Ledger, explicit: Tier | None) -> Tier:
    """The strictest of `--tier`, the active task's gear and the latest round's tier.

    An explicit `--tier` can raise the bar for `check`, never lower it.
    """
    requested = [explicit] if explicit is not None else []
    if ws.state is not None:
        requested.append(current_tier(ws.state, ws.root))
    if not requested:
        return ledger.rounds[-1].tier if ledger.rounds else "standard"
    return effective_tier(ledger, *requested)


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


def _prepare(args: argparse.Namespace) -> int:
    requested = _parse_lens_additions(args.add_lens)
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    base_sha = ws.base_sha(ledger, args.base)
    full_snap = snapshot(ws.root, base_sha)
    scope, snap, allow_scope_expansion = _prepare_scope_and_snapshot(
        ws, ledger, args, base_sha, full_snap
    )
    if snap.is_empty:
        if requested:
            print("--add-lens ignored: there is no change to review.")
        return _print_empty_prepare(scope, snap, full_snap)
    tier = resolve_tier(ws, ledger, args.tier, snap)
    if not ledger.rounds or (
        ledger.rounds[-1].fingerprint, ledger.rounds[-1].tier, ledger.rounds[-1].scope
    ) != (snap.fingerprint, tier, scope):
        print(f"Tier {tier}: {_tier_reason(ws, ledger, args.tier, snap)}")
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    signals = collect_quality_signals(ws.root, snap)

    def open_next(
        current: Ledger,
    ) -> tuple[Ledger, tuple[Ledger, Round, bool, dict[str, str], list[str]]]:
        new, notes = _new_lens_additions(current, tier, snap, requested)
        lens_ids = [pick.lens.id for pick in _lens_picks(tier, snap, {**current.added_lenses, **new})]
        packet = packet_path(ws.ledger_path, len(current.rounds) + 1)
        updated, round_ = open_round(
            current, snap, tier, lens_ids, str(packet), symbols,
            signals=signals, scope=scope, allow_scope_expansion=allow_scope_expansion,
        )
        if new:
            updated = add_lenses(updated, round_.number, new)
        return updated, (updated, round_, updated is not current, new, notes)

    ledger, round_, created, added, notes = mutate(ws.ledger_path, open_next)
    packet = Path(round_.packet_path)
    if created or not packet.is_file():
        ctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=args.task or ws.task)
        _write_text(packet, render_packet(ctx))
    _mark_needs_human_if_oversized(ws, ledger)
    for note in notes:
        print(note)
    for lens, reason in added.items():
        print(f"Added lens `{lens}`: {reason}")
    _print_prepare_summary(round_, packet, created)
    _print_unscanned_files(snap)
    _print_scope_summary(ws, ledger, round_, snap, full_snap)
    if created:
        prune_round_files(ws.ledger_path.parent, round_.number)
        prune_inbox_files(ws.ledger_path.parent)
    return 0


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
            GatePolicy(tier=round_.tier, isolation_evidence=ws.isolation_evidence),
            review_only=True,
        ).passed:
            print(
                f"Hint: round {round_.number} is already complete; run `craftsman review reset` "
                "to archive it before starting a new review (or `craftsman review prepare "
                "--add-paths <paths>` to include files added by a fix)."
            )


def _lens_picks(
    tier: Tier, snap: DiffSnapshot, added: Mapping[str, str] | None = None
) -> tuple[LensPick, ...]:
    return select_lenses(tier, detect_signals(snap.hunks), docs_only=snap.docs_only, added=added)


def _parse_lens_additions(values: Sequence[str] | None) -> dict[str, str]:
    """The `--add-lens LENS=REASON` values as {lens: reason}, each one validated."""
    reasons: dict[str, str] = {}
    for value in values or ():
        lens, separator, reason = value.partition("=")
        lens, reason = lens.strip(), reason.strip()
        if not separator:
            raise FieldError(
                "add_lens", f"must be LENS=REASON, e.g. {LENS_ADDITION_EXAMPLE!r} (got {value!r})"
            )
        if lens in reasons:
            raise FieldError("add_lens", f"names `{lens}` more than once")
        try:
            require_lens_addition(lens, reason)
        except FieldError as exc:
            raise FieldError("add_lens", f"{lens}: {exc.field} {exc.problem}") from None
        reasons[lens] = reason
    return reasons


def _new_lens_additions(
    ledger: Ledger, tier: Tier, snap: DiffSnapshot, requested: Mapping[str, str]
) -> tuple[dict[str, str], list[str]]:
    """The requested lenses that are not in the review yet, and a note for each one skipped."""
    selected = {pick.lens.id: pick.reason for pick in _lens_picks(tier, snap)}
    recorded = {addition.lens: addition for addition in ledger.lens_additions}
    new: dict[str, str] = {}
    notes: list[str] = []
    for lens, reason in requested.items():
        if lens in recorded:
            earlier = recorded[lens]
            notes.append(f"Lens `{lens}` was already added in round {earlier.round}: {earlier.reason}")
        elif lens in selected:
            notes.append(f"Lens `{lens}` is already selected ({selected[lens]}); nothing to add.")
        else:
            new[lens] = reason
    return new, notes


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
        "risks the selected lenses miss and add the matching lens with "
        '`craftsman review prepare --add-lens "LENS=file:line why"`.'
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
) -> PacketContext:
    """What the packet of `round_` shows, with the lenses the round selected."""
    picks = tuple(
        pick
        for pick in _lens_picks(round_.tier, snap, ledger.added_lenses)
        if pick.lens.id in round_.lenses
    )
    return collect_context(
        ws.root,
        snap,
        round_,
        picks,
        task,
        symbols=symbols,
        ledger=ledger,
        index=index,
    )


def _mark_needs_human_if_oversized(ws: Workspace, ledger: Ledger) -> None:
    """An oversized change needs the user's decision; the active gate records that."""
    if ws.state is None or not has_unresolved_diff_size(ledger):
        return
    update_state(state_path_for(ws.root), lambda current: (_needing_human(current), None))


def _needing_human(state: GateState | None) -> GateState | None:
    return replace(state, stage=Stage.NEEDS_HUMAN) if state is not None and state.active else state


def _print_prepare_summary(round_: Round, packet: Path, created: bool) -> None:
    if not created:
        print(f"Round {round_.number} unchanged (same diff and tier). Packet: {packet}")
        if round_.tier in ("standard", "deep"):
            print(_suggested_shards_line(round_, packet))
        return
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
    print(
        f"Round {round_.number} prepared ({round_.tier}): {len(round_.hunks)} hunks, "
        f"{carried} carried, {len(round_.open_hunk_ids)} to review."
    )
    print(f"Lenses: {', '.join(round_.lenses)}")
    print(f"Packet: {packet}")
    if round_.tier in ("standard", "deep"):
        print(_suggested_shards_line(round_, packet))
    sweep_last = "; run the `sweep` lens last" if SWEEP_LENS in round_.lenses else ""
    print(
        "Next: read the packet, apply every lens to each non-carried hunk, and record "
        f"`craftsman review add` / `craftsman review cover`{sweep_last}."
    )


def _suggested_shards_line(round_: Round, packet: Path) -> str:
    try:
        packet_chars = len(packet.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LedgerError(f"cannot read {packet}: {exc}") from exc
    shards = suggested_shard_count(len(round_.open_files), packet_chars)
    detail = "1 (no sharding needed)" if shards == 1 else str(shards)
    return f"Suggested reviewer shards: {detail}"


def _write_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"cannot write {path}: {exc}") from exc


def view_slug(
    lenses: Sequence[str], hunks_raw: str | None = None, *, shard: tuple[int, int] | None = None
) -> str:
    """The file-name part of a reviewer view: its lenses, then its shard or hunk selection."""
    lens_slug = "-".join(lenses)
    if shard is not None:
        return f"{lens_slug}.shard-{shard[0]}-of-{shard[1]}"
    if hunks_raw is None:
        return lens_slug
    hunk_slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in hunks_raw.strip()).strip("-")
    return f"{lens_slug}.{hunk_slug}" if hunk_slug else lens_slug


def parse_view_lenses(raw: str, round_: Round) -> list[str]:
    """Lens ids and reviewer-group ids from `--lenses`, resolved to lenses selected in `round_`."""
    tokens = _comma_list(raw)
    if not tokens:
        raise FieldError("lenses", "must name at least one lens or reviewer group")
    lenses: list[str] = []
    for token in tokens:
        candidates = REVIEWER_GROUPS.get(token, (token,))
        if token not in REVIEWER_GROUPS and token not in LENS_IDS:
            known = ", ".join([*REVIEWER_GROUPS, *sorted(LENS_IDS)])
            raise FieldError("lenses", f"names unknown lens or group {token!r} (choose from {known})")
        selected = [lens for lens in candidates if lens in round_.lenses]
        if not selected:
            raise FieldError(
                "lenses",
                f"{token!r} has no lens selected in round {round_.number} "
                f"(selected: {', '.join(round_.lenses)})",
            )
        lenses.extend(selected)
    return list(dict.fromkeys(lenses))


def _view(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    snap = ws.snapshot(ledger)
    round_ = require_current_round(ledger, snap)
    lenses = parse_view_lenses(args.lenses, round_)
    if args.shard is not None and args.hunks is not None:
        raise LedgerError("pass either --shard or --hunks, not both")
    if args.shard is not None:
        parse_shard_spec(args.shard, len(round_.open_files))
    assigned = (
        round_.open_hunk_ids
        if args.hunks is None
        else expand_hunk_spec(args.hunks, [hunk.id for hunk in round_.hunks])
    )
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}
    ctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=ws.task)
    shard = select_shard(ctx, args.shard) if args.shard is not None else None
    if shard is not None:
        assigned = shard.hunk_ids
    slug = view_slug(lenses, args.hunks, shard=(shard.index, shard.total) if shard else None)
    view_path = Path(round_.packet_path).with_name(f"packet-{round_.number}.{slug}.md")
    _write_text(view_path, render_packet_view(ctx, lenses=lenses, assigned_hunks=assigned, shard=shard))
    print(view_path)
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


def _entry_problem(index: int, label: str | None, exc: LedgerError) -> EntryProblem:
    field = JSON_FIELD_NAMES.get(exc.field, exc.field)
    problem = exc.problem if isinstance(exc, FieldError) else str(exc)
    return EntryProblem(index=index, label=label, field=field, problem=problem, example=exc.example)


def _record_entries(
    command: str,
    raw_entries: Sequence[Any],
    current: Ledger,
    label_for: Callable[[int, Any], str | None],
    record_one: Callable[[int, Ledger, Any], tuple[Ledger, R]],
    on_rejected: Callable[[int, Any], None] | None = None,
) -> tuple[Ledger, list[R]]:
    results: list[R] = []
    problems: list[EntryProblem] = []
    for index, raw in enumerate(raw_entries, 1):
        try:
            current, item = record_one(index, current, raw)
        except LedgerError as exc:
            problems.append(_entry_problem(index, label_for(index, raw), exc))
            if on_rejected is not None:
                on_rejected(index, raw)
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


# --- coverage -------------------------------------------------------------------------------


def _cover(args: argparse.Namespace) -> int:
    if args.from_json is not None:
        return _cover_from_json(args)
    return _cover_one(args)


def _cover_one(args: argparse.Namespace) -> int:
    _require_flags(args, {"--lens": args.lens})
    if args.hunks is not None and args.files is not None:
        raise LedgerError("pass either --hunks or --files, not both")
    lenses = _comma_list(args.lens)
    if lenses == [DESIGN_LENS] and args.hunks is not None:
        raise LedgerError(
            "the `design` lens covers changed files, not hunks; pass --files instead of --hunks"
        )
    if args.hunks is None and args.files is None:
        missing_flag = "--files" if lenses == [DESIGN_LENS] else "--hunks"
        _require_flags(args, {missing_flag: None, "--note": args.note})
    _require_flags(args, {"--note": args.note})
    target_kind: TargetKind = "files" if args.files is not None else "hunks"
    raw_targets = args.files if args.files is not None else args.hunks
    assert raw_targets is not None
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    targets = _comma_list(raw_targets)

    def record(current: Ledger) -> tuple[Ledger, str]:
        round_ = require_current_round(current, snap)
        return _record_coverage(current, round_, lenses, targets, args.note, target_kind=target_kind)

    return _run_single(ws, bool(args.check), record, print)


def _cover_entry_label(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("lens"), str):
        cleaned = raw["lens"].strip()
        return cleaned or None
    lenses = raw.get("lenses")
    if isinstance(lenses, list) and all(isinstance(item, str) for item in lenses):
        cleaned_list = [item.strip() for item in lenses if item.strip()]
        return ",".join(cleaned_list) if cleaned_list else None
    return None


def _raw_cover_pairs(round_: Round, raw: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    lenses: list[str] = []
    if isinstance(raw.get("lens"), str):
        lenses.append(str(raw["lens"]))
    elif isinstance(raw.get("lenses"), list):
        lenses.extend(item for item in raw["lenses"] if isinstance(item, str))
    targets: list[str] = []
    for key in ("hunks", "files"):
        items = raw.get(key)
        if isinstance(items, list):
            targets.extend(item for item in items if isinstance(item, str))
    return coverage_units_for_targets(round_, lenses, targets)


def _parse_json_lenses(raw: Any, f: Fields) -> tuple[str, ...]:
    has_lens = isinstance(raw, dict) and "lens" in raw
    has_lenses = isinstance(raw, dict) and "lenses" in raw
    if has_lens and has_lenses:
        raise FieldError("lenses", "cannot be combined with 'lens'; pass one or the other")
    if has_lenses:
        return f.texts("lenses")
    return (f.text("lens"),)


def _cover_one_from_json(
    state: Ledger,
    round_: Round,
    raw: Any,
    rejected_by: Mapping[tuple[str, str], int],
) -> tuple[Ledger, str]:
    f = Fields(raw)
    lenses = _parse_json_lenses(raw, f)
    target_key = _target_key(raw, lenses)
    targets = [item.strip() for item in f.texts(target_key) if item.strip()]
    note = f.text("note")
    f.reject_unknown()
    return _record_coverage(
        state, round_, lenses, targets, note, target_kind=target_key, rejected_by=rejected_by
    )


def _cover_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {"--lens": args.lens, "--hunks": args.hunks, "--files": args.files, "--note": args.note}
    )
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    def record_batch(current: Ledger) -> tuple[Ledger, list[str]]:
        round_ = require_current_round(current, snap)
        rejected_by: dict[tuple[str, str], int] = {}

        def note_rejected(index: int, raw: Any) -> None:
            for pair in _raw_cover_pairs(round_, raw):
                rejected_by.setdefault(pair, index)

        return _record_entries(
            "cover",
            raw_entries,
            current,
            lambda _index, raw: _cover_entry_label(raw),
            lambda _index, state, raw: _cover_one_from_json(state, round_, raw, rejected_by),
            on_rejected=note_rejected,
        )

    return _run_batch(ws, args, record_batch, print)


def _target_key(raw: Mapping[str, Any], lenses: Sequence[str]) -> TargetKind:
    """JSON target key for a coverage entry: `design` requires `files`; others accept `hunks` (or `files` in quick)."""
    if tuple(lenses) == (DESIGN_LENS,):
        return "files"
    if "files" in raw and "hunks" in raw:
        raise LedgerError("pass either 'hunks' or 'files', not both")
    return "files" if "files" in raw else "hunks"


def _record_coverage(
    current: Ledger,
    round_: Round,
    lenses: Sequence[str],
    targets: Sequence[str],
    note: str,
    *,
    target_kind: TargetKind | None = None,
    rejected_by: Mapping[tuple[str, str], int] | None = None,
) -> tuple[Ledger, str]:
    updated, records = add_multi_coverage(
        current, lenses, targets, note, target_kind=target_kind, rejected_by=rejected_by
    )
    left = sum(len(ids) for ids in uncovered_by_lens(updated, round_).values())
    covered_lenses = ", ".join(f"`{record.lens}`" for record in records)
    return updated, (
        f"Coverage recorded: {covered_lenses} covered {', '.join(targets)} in round {round_.number}; "
        f"{left} lens item(s) left in this round."
    )


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

    return _run_single(ws, bool(args.check), record, lambda finding: _print_recorded(finding, lines))


def _add_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {**_required_finding_flags(args), "--suggestion": args.suggestion, "--signal": args.signal}
    )
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    cwd = _cwd(args)

    def record_batch(current: Ledger) -> tuple[Ledger, list[tuple[Finding, list[str] | None]]]:
        require_current_round(current, snap)
        base_number = int(next_finding_id(current)[1:])
        return _record_entries(
            "add",
            raw_entries,
            current,
            lambda index, _raw: f"F{base_number + index - 1}",
            lambda _index, state, raw: _add_one_from_json(ws, cwd, snap, state, raw),
        )

    return _run_batch(ws, args, record_batch, lambda pair: _print_recorded(pair[0], pair[1]))


def _add_one_from_json(
    ws: Workspace, cwd: Path, snap: DiffSnapshot, current: Ledger, raw: Any
) -> tuple[Ledger, tuple[Finding, list[str] | None]]:
    f = Fields(raw)
    lens = f.choice("lens", tuple(sorted(LENS_IDS)))
    raw_file = f.text("file")
    line = f.integer("line")
    severity = f.choice("severity", SEVERITIES)
    title = f.text("title")
    scenario = f.text("scenario")
    suggestion = f.text_or("suggestion", "")
    signal_id = f.text_if_present("signal")
    f.reject_unknown()
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
    return updated, (finding, lines)


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


def _ack(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    def record(current: Ledger) -> tuple[Ledger, SignalAck]:
        require_current_round(current, snap)
        return ack_signal(current, args.signal_id, args.reason)

    ack = mutate(ws.ledger_path, record)
    print(f"{ack.signal_id} acknowledged in round {ack.round}: {ack.reason}")
    return 0


def _parse_fact_source(ws: Workspace, cwd: Path, raw: str, snap: DiffSnapshot) -> tuple[SourceRef, str]:
    raw_path, separator, raw_line = raw.rpartition(":")
    if not separator:
        raise FieldError("source", "must look like path:line (e.g. pkg/a.py:42)")
    try:
        line = int(raw_line)
    except ValueError:
        raise FieldError("source", f"line {raw_line!r} must be an integer") from None
    if line < 1:
        raise FieldError("source", f"line must be >= 1 (got {line})")
    try:
        rel_path = _finding_path(ws.root, cwd, raw_path, snap)
        _source_lines(ws.root, rel_path, line)
    except FieldError as exc:
        raise FieldError("source", exc.problem) from None
    digest = file_sha256(ws.root, rel_path)
    if digest is None:
        raise FieldError("source", f"{rel_path} does not exist in the work tree")
    return SourceRef(path=rel_path, line=line), digest


def _fact_add(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()
    source, digest = _parse_fact_source(ws, _cwd(args), args.source, snap)

    def record(current: Ledger) -> tuple[Ledger, Fact]:
        round_ = require_current_round(current, snap)
        return add_fact(
            current,
            hunks=expand_hunk_spec(args.hunks, [hunk.id for hunk in round_.hunks]),
            source=source,
            text=args.text,
            source_digest=digest,
            author_lens=args.lens,
        )

    fact = mutate(ws.ledger_path, record)
    print(f"{fact.id} recorded ({', '.join(fact.hunks)}, source {fact.source.path}:{fact.source.line})")
    return 0


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
    return _run_single(
        ws,
        bool(args.check),
        lambda current: set_verdict(current, args.finding_id, args.verdict, args.evidence, args.run),
        _print_verdict,
    )


def _verdict_one_from_json(state: Ledger, raw: Any) -> tuple[Ledger, Finding]:
    f = Fields(raw)
    finding_id = f.text("id")
    verdict = f.choice("as", VERDICTS)
    evidence = f.text("evidence")
    run_id = f.text_if_present("run")
    f.reject_unknown()
    return set_verdict(state, finding_id, verdict, evidence, run_id)


def _verdict_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {"ID": args.finding_id, "--as": args.verdict, "--evidence": args.evidence, "--run": args.run}
    )
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)

    def record_batch(current: Ledger) -> tuple[Ledger, list[Finding]]:
        return _record_entries(
            "verdict",
            raw_entries,
            current,
            lambda _index, raw: _entry_label(raw, "id"),
            lambda _index, state, raw: _verdict_one_from_json(state, raw),
        )

    return _run_batch(ws, args, record_batch, _print_verdict)


def _print_verdict(finding: Finding) -> None:
    run = f" (run {finding.verdict_run})" if finding.verdict_run else ""
    print(f"{finding.id} verdict recorded: {finding.verdict}{run}")


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
    if args.hunks:
        return _show_hunks(ws.root, latest, snap, as_json=args.json)
    if args.json:
        payload = {
            "round": _round_json(latest, snap) if latest else None,
            "lens_additions": [addition.to_json() for addition in ledger.lens_additions],
            "coverage": _coverage_json(ledger, latest) if latest else {},
            "hunks": _hunks_json(ws.root, latest, snap) if latest else [],
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
        for line in _lens_addition_lines(ledger):
            print(line)
        print()
        print(f"Coverage (covered/total non-carried hunks, files for `{DESIGN_LENS}`):")
        print("\n".join(_coverage_lines(ledger, latest)))
    print()
    print("\n".join(_findings_table(ledger.findings)))
    print()
    print("\n".join(_run_lines(ledger.runs[-SHOW_RECENT_RUNS:], snap)))
    return 0


def _show_hunks(root: Path, latest: Round | None, snap: DiffSnapshot, *, as_json: bool) -> int:
    if as_json:
        payload = {
            "round": _round_json(latest, snap) if latest else None,
            "stale": bool(latest is not None and latest.fingerprint != snap.fingerprint),
            "hunks": _hunks_json(root, latest, snap) if latest else [],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
        return 0
    print("\n".join(_hunk_map_lines(root, latest, snap)))
    return 0


def _hunk_span(new_start: int, new_len: int, old_start: int = 0, old_len: int = 0) -> tuple[int, int]:
    if new_len > 0:
        return new_start, new_start + new_len - 1
    if old_len > 0:
        return old_start, old_start + old_len - 1
    return 0, 0


def _hunks_json(root: Path, round_: Round, snap: DiffSnapshot) -> list[dict[str, Any]]:
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
        if ref.carried or (hunk := by_id.get(ref.id)) is None:
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
                "carried": False,
                "status": hunk.status,
                "scope": scope.name if scope is not None and scope.kind != "block" else None,
            }
        )
    return items


def _format_span(path: str, start: int, end: int, has_range: bool, *, bare_zero: bool = False) -> str:
    if has_range:
        return f"{path}:{start}-{end}"
    return path if bare_zero else f"{path}:{start}"


def _hunk_map_lines(root: Path, round_: Round, snap: DiffSnapshot) -> list[str]:
    """One compact `H#  path:start-end  scope|status` line per open hunk, or HunkRef fields if stale."""
    entries = _hunks_json(root, round_, snap)
    if round_.fingerprint != snap.fingerprint:
        lines = [
            f"{e['id']}  {_format_span(e['path'], e['new_start'], e['end_line'], e['new_len'] > 0)}  "
            f"[{'carried' if e['carried'] else 'open'}]"
            for e in entries
        ]
        lines.append("stale: run `craftsman review prepare`")
        return lines
    lines = [
        f"{e['id']}  "
        f"{_format_span(e['path'], e['start_line'], e['end_line'], e['end_line'] > 0, bare_zero=True)}  "
        f"{e['scope'] or e['status']}"
        for e in entries
    ]
    return lines or ["(no open hunks)"]


def _round_line(round_: Round, snap: DiffSnapshot) -> str:
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
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
        "carried": len(round_.hunks) - len(round_.open_hunk_ids),
        "lenses": list(round_.lenses),
        "packet": round_.packet_path,
    }


def _coverage_json(ledger: Ledger, round_: Round) -> dict[str, Any]:
    coverage: dict[str, Any] = {}
    for lens, missing in uncovered_by_lens(ledger, round_).items():
        total = len(open_units(round_, lens))
        coverage[lens] = {"covered": total - len(missing), "total": total, "missing": missing}
    return coverage


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
    ws: Workspace, tier_override: Tier | None, *, review_only: bool
) -> tuple[Ledger, DiffSnapshot, Tier, GateReport]:
    if review_only and ws.state is not None:
        raise LedgerError(
            "--review-only is not allowed during an active /craftsman gate session; "
            "in /craftsman the full gate applies (run `craftsman review check` without --review-only)"
        )
    ledger = load(ws.ledger_path)
    tier = evaluation_tier(ws, ledger, tier_override)
    snap = ws.snapshot(ledger)
    policy = GatePolicy(tier=tier, isolation_evidence=ws.isolation_evidence)
    return ledger, snap, tier, evaluate(ledger, snap, policy, review_only=review_only)


def _check(args: argparse.Namespace) -> int:
    _, _, _, report = _evaluate(open_workspace(args.cwd), args.tier, review_only=args.review_only)
    if args.json:
        print(json.dumps(_report_json(report), indent=2, ensure_ascii=False))
    else:
        print(render_report(report))
    return 0 if report.passed else 1


def _report_json(report: GateReport) -> dict[str, Any]:
    return {
        "passed": report.passed,
        "partial": report.partial,
        "self_reviewed": report.self_reviewed,
        "issues": [{"code": issue.code, "message": issue.message} for issue in report.issues],
        "warnings": list(report.warnings),
        "summary": report.summary,
    }


def _report(args: argparse.Namespace) -> int:
    if args.partial and not args.review_only:
        raise LedgerError(
            "--partial requires --review-only; pass `craftsman review report --review-only --partial`"
        )
    ws = open_workspace(args.cwd)
    ledger, snap, tier, report = _evaluate(ws, None, review_only=args.review_only)
    if args.partial:
        report = replace(report, partial=True)
    groups = _finding_groups(ledger.findings)
    runs = passing_runs(ledger, snap.fingerprint)
    if args.json:
        payload = {
            **_report_json(report),
            "tier": tier,
            "rounds": len(ledger.rounds),
            "base_sha": snap.base_sha,
            "diff": {"files": len(snap.files), "added": snap.added, "deleted": snap.deleted},
            "not_covered": _not_covered_by_lens(ledger),
            "findings": {name: [f.to_json() for f in items] for name, items in groups.items()},
            "runs": [run.to_json() for run in runs],
            "waived_runs": [run.to_json() for run in ledger.runs if run.waiver is not None],
            "signal_acks": {sid: ack.to_json() for sid, ack in acks_in_force(ledger).items()},
            "lens_additions": [addition.to_json() for addition in ledger.lens_additions],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(_report_lines(ledger, snap, tier, report, groups, runs)))
    if args.review_only:
        prune_inbox_files(ws.ledger_path.parent)
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


def _report_status_line(report: GateReport) -> str:
    subject = "Review" if report.partial else report.subject.capitalize()
    line = f"- {subject}: {report.status_label}"
    if report.passed:
        return line
    check = (
        "craftsman review check --review-only"
        if report.review_only or report.partial
        else "craftsman review check"
    )
    return f"{line} — {report.issue_codes}; see `{check}`"


def _lens_addition_lines(ledger: Ledger) -> list[str]:
    return [
        f"- Lens `{addition.lens}` added by the agent in round {addition.round}: {addition.reason}"
        for addition in ledger.lens_additions
    ]


def _not_covered_by_lens(ledger: Ledger) -> dict[str, list[str]]:
    if not ledger.rounds:
        return {}
    return {
        lens: ids for lens, ids in uncovered_by_lens(ledger, ledger.rounds[-1]).items() if ids
    }


def _not_covered_lines(ledger: Ledger, snap: DiffSnapshot, report: GateReport) -> list[str]:
    missing = _not_covered_by_lens(ledger)
    if missing:
        return [
            "",
            "## Not covered",
            *(f"- `{lens}`: {', '.join(ids)}" for lens, ids in missing.items()),
        ]
    if report.partial and not ledger.rounds and not snap.is_empty:
        return ["", "## Not covered", "- No review round prepared yet."]
    return []


def _report_finding_lines(ledger: Ledger, groups: dict[str, list[Finding]]) -> list[str]:
    if not ledger.findings:
        return ["No findings recorded."]
    detail: dict[str, Callable[[Finding], str]] = {
        "fixed": lambda f: f"Fix: {f.resolution_note}",
        "accepted": lambda f: f"Accepted because: {f.resolution_note}",
        "open": lambda f: (
            f"Verdict: {f.verdict}" if f.verdict is not None else "Verdict: none yet (unverified)"
        ),
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
    acks = acks_in_force(ledger)
    if not acks:
        return []
    latest = ledger.rounds[-1]
    lines = ["", "## Acknowledged quality signals"]
    for signal_id, ack in acks.items():
        sig = latest.signal(signal_id)
        lines.append(f"- {signal_id} (`{sig.kind.value}` at `{sig.file}:{sig.line}`) — {ack.reason}")
    return lines


def _diff_size_lines(ledger: Ledger, snap: DiffSnapshot) -> list[str]:
    if not has_unresolved_diff_size(ledger):
        return []
    total, top = production_diff_growth(snap)
    return [
        "",
        "## Diff size budget exceeded (needs human decision)",
        f"- Added production lines: {total} (budget: {MAX_PRODUCTION_ADDED_LINES})",
        "- Top files by line growth:",
        *(f"  {index}. `{path}` (+{count} lines)" for index, (path, count) in enumerate(top, 1)),
    ]


def _not_assessed_lines(tier: Tier, report: GateReport) -> list[str]:
    if tier == "quick":
        isolation = "quick tier (inline review allowed)"
    elif report.self_reviewed:
        isolation = SELF_REVIEWED_BANNER
    else:
        isolation = "verified via subagent stop events in the ledger"
    return [
        "",
        "## Not assessed by the gate",
        "- Business requirements fit and domain behavior not exercised by recorded runs.",
        "- Naming quality and domain clarity.",
        "- Architecture and invariants outside the changed files.",
        f"- Reviewer isolation status: {isolation}.",
    ]


def _waived_run_lines(ledger: Ledger) -> list[str]:
    waived = [(run, run.waiver) for run in ledger.runs if run.waiver is not None]
    if not waived:
        return []
    return [
        "",
        "## Waived runs",
        *(
            f"- {run.id} `{run.command}` ({run.kind}, exit {run.exit_code}) — {waiver.reason}"
            for run, waiver in waived
        ),
    ]


def _report_lines(
    ledger: Ledger,
    snap: DiffSnapshot,
    tier: Tier,
    report: GateReport,
    groups: dict[str, list[Finding]],
    runs: Sequence[Run],
) -> list[str]:
    banner = [f"**{SELF_REVIEWED_BANNER}**"] if report.self_reviewed else []
    run_lines = [
        f"- {run.id} `{run.command}` ({run.kind}) → exit {run.exit_code} in {run.duration_s:.1f}s"
        for run in runs
    ] or ["No passing test or e2e run at the final code."]
    warning_lines = ["", "## Warnings", *(f"- {w}" for w in report.warnings)] if report.warnings else []
    return [
        "# Craftsman review report",
        *banner,
        f"- Base `{snap.base_sha[:7]}` · tier `{tier}` · {len(ledger.rounds)} review round(s) · "
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines",
        _report_status_line(report),
        *_lens_addition_lines(ledger),
        *_diff_size_lines(ledger, snap),
        *_not_covered_lines(ledger, snap, report),
        "",
        "## Findings",
        *_report_finding_lines(ledger, groups),
        *_signal_ack_lines(ledger),
        "",
        "## Verification at the final code",
        *run_lines,
        *_waived_run_lines(ledger),
        *warning_lines,
        *_not_assessed_lines(tier, report),
    ]


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
        chosen = as_choice(value, choices)
        if chosen is not None:
            return chosen
        raise argparse.ArgumentTypeError(f"invalid choice {value!r} (choose from {', '.join(choices)})")

    return parse


def _tier_option(value: str) -> Tier | None:
    """`--tier`: a concrete tier, or `auto` for the tier the gate resolved for the active task."""
    if value == "auto":
        return None
    tier = as_choice(value, TIERS)
    if tier is not None:
        return tier
    raise argparse.ArgumentTypeError(f"invalid choice {value!r} (choose from auto, {', '.join(TIERS)})")


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
    prepare.add_argument("--tier", type=_tier_option, help="auto (default), quick, standard or deep")
    prepare.add_argument("--base", help="Base commit (default: the active task's base or HEAD)")
    prepare.add_argument("--task", help="One-line description of what the change is for")
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
    prepare.add_argument(
        "--add-lens",
        action="append",
        metavar="LENS=REASON",
        help=(
            "Add a lens the risk signals did not select, citing the code that needs it as file:line "
            f"(repeatable), e.g. {LENS_ADDITION_EXAMPLE!r}. Added lenses stay in every later round."
        ),
    )
    prepare.set_defaults(handler=_prepare)


def _add_view_and_cover_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    view = sub.add_parser("view", parents=[common], help="Build a per-reviewer packet view")
    view.add_argument("--lenses", required=True, help="Comma-separated lens ids or groups, e.g. R1")
    view.add_argument("--hunks", help="Comma-separated hunk ids or ranges, e.g. H3-H17")
    view.add_argument("--shard", help="File-aligned open-hunk shard K/N (1 <= K <= N), e.g. 1/2")
    view.set_defaults(handler=_view)

    cover = sub.add_parser("cover", parents=[common], help="Record that lens(es) reviewed hunks or files")
    cover.add_argument("--lens", help="Lens id or comma-separated lens ids, e.g. intent,contracts")
    cover.add_argument("--hunks", help="Comma-separated hunk ids, e.g. H1,H3")
    cover.add_argument(
        "--files",
        help="Comma-separated file paths (for `--lens design` or any lens in a quick round), e.g. pkg/a.py",
    )
    cover.add_argument("--note", help="What you tried on each named hunk or file")
    cover.add_argument(
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"lens"|"lenses", "hunks"|"files", "note"} objects (use - for stdin)',
    )
    cover.set_defaults(handler=_cover, subparser=cover)


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
        "--signal", default=None, help="Quality signal id resolved by this finding (e.g. S1)"
    )
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

    fact = sub.add_parser(
        "fact", parents=[common], help="Record a source-cited fact for other reviewers"
    )
    fact.add_argument("action", nargs="?", choices=("add",), default="add")
    fact.add_argument("--hunks", required=True, help="Comma-separated hunk ids or ranges, e.g. H3")
    fact.add_argument(
        "--source", required=True, help="Source file:line in the work tree, e.g. pkg/a.py:42"
    )
    fact.add_argument("--text", required=True, help="Verifiable fact (>= 30 chars, no judgement words)")
    fact.add_argument(
        "--lens", default="contracts", choices=lenses, help="Author lens (default: contracts)"
    )
    fact.set_defaults(handler=_fact_add)


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

    ack = sub.add_parser(
        "ack", parents=[common], help="Acknowledge a quality signal with a concrete reason"
    )
    ack.add_argument("signal_id", metavar="ID", help="Quality signal id (e.g. S1)")
    ack.add_argument(
        "--reason", required=True, help="Why the signal is acceptable (>= 40 chars, concrete)"
    )
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
    sub.choices["show"].add_argument(
        "--hunks",
        action="store_true",
        help="Print a compact map of open hunks (H# -> file:lines and scope or status)",
    )
    sub.choices["check"].add_argument("--tier", type=_tier_option)
    for name in ("check", "report"):
        sub.choices[name].add_argument(
            "--review-only",
            action="store_true",
            help="Check only review completeness (round, lenses, sweep, verdicts), without requiring fixes or test runs",
        )
    sub.choices["report"].add_argument(
        "--partial",
        action="store_true",
        help="Render a partial review-only report when some lenses or verdicts could not be completed",
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
    for example in dict.fromkeys(item.example for item in exc.problems if item.example):
        lines.append(f'Note format: "{example}"')
    return "\n".join(lines)


def describe_error(exc: LedgerError | DiffScanError | GateStateError) -> str:
    """The error message, naming the command-line flag when a flag's value was rejected."""
    if isinstance(exc, FieldError) and exc.field in FIELD_FLAGS:
        base = f"{FIELD_FLAGS[exc.field]} {exc.problem}"
    else:
        base = str(exc)
    if isinstance(exc, LedgerError) and exc.example:
        return f'{base}. Example: "{exc.example}"'
    return base
