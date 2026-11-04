"""`craftsman review`: prepare review rounds and record or inspect evidence in the ledger."""

from __future__ import annotations

import argparse
import hashlib
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
    detect_deep_hint_signals,
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
    evaluate,
    large_diff_warning,
    passing_runs,
    render_report,
    require_current_round,
)
from craftsman.ledger import (
    FIXED_SCOPE_ERROR,
    TargetKind,
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
    set_verdict,
    stable_hunk_ids,
    uncovered_hunks,
    waive_run,
)
from craftsman.lenses import LENS_IDS, LensPick, deep_hint, select_lenses
from craftsman.packet import (
    PacketContext,
    bound_hints,
    collect_context,
    expand_hunk_spec,
    render_packet,
    render_packet_view,
)
from craftsman.quality import collect_quality_signals
from craftsman.records import (
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
    Round,
    Run,
    Severity,
    Verdict,
    as_choice,
    check_coverage_note,
    collecting_problems,
)
from craftsman.scout import Scout, parse_target
from craftsman.text import inline_md, one_line
from craftsman.usage import usage_json, usage_lines
from craftsman.workspace import Workspace, open_workspace

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


def resolve_deep(ws: Workspace, ledger: Ledger, requested: bool) -> bool:
    """Whether the round is deep: `--deep`, the active gate or an earlier round asked for it.

    Depth is never lowered inside a review.
    """
    return effective_deep(ledger, requested, ws.state is not None and ws.state.deep)


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
    ws = open_workspace(args.cwd)
    ledger = load(ws.ledger_path)
    base_sha = ws.base_sha(ledger, args.base)
    full_snap = snapshot(ws.root, base_sha)
    scope, snap, allow_scope_expansion = _prepare_scope_and_snapshot(
        ws, ledger, args, base_sha, full_snap
    )
    if snap.is_empty:
        return _print_empty_prepare(scope, snap, full_snap)
    snap = stable_hunk_ids(ledger, snap)
    owned = _owns_gate(ws)
    if not owned and args.deep:
        print(
            "Note: --deep ignored: the active gate belongs to another session, "
            "which alone can raise it."
        )
    deep = resolve_deep(ws, ledger, bool(args.deep) and owned)
    if ws.state is not None and owned and deep and not ws.state.deep:
        update_state(state_path_for(ws.root), lambda current: (_raised_to_deep(current), None))
        print("Gate raised to deep: a passing end-to-end run is now required.")
    index = WorktreeIndex(ws.root)
    symbols = {hunk.id: index.symbols(hunk) for hunk in snap.hunks}

    def open_next(current: Ledger) -> tuple[Ledger, tuple[Ledger, Round, bool]]:
        if stable_hunk_ids(current, snap) != snap:
            raise LedgerError("the ledger changed while the round was prepared; run prepare again")
        packet = packet_path(ws.ledger_path, len(current.rounds) + 1)
        out_of_scope = tuple(sorted(set(full_snap.files) - set(scope))) if scope is not None else ()
        updated, round_ = open_round(
            current, snap, deep, str(packet), symbols,
            scope=scope, allow_scope_expansion=allow_scope_expansion, out_of_scope=out_of_scope,
        )
        if len(updated.rounds) > len(current.rounds):
            # Carried hunks were reviewed already; the hints are about the code still to review.
            hints = collect_quality_signals(ws.root, snap.only(round_.open_hunk_ids))
            round_ = replace(round_, signals=hints)
            updated = replace(updated, rounds=(*updated.rounds[:-1], round_))
        return updated, (updated, round_, updated is not current)

    before = len(ledger.rounds)
    ledger, round_, created = mutate(ws.ledger_path, open_next)
    raised = created and round_.number <= before
    packet = Path(round_.packet_path)
    open_snap = snap.only(round_.open_hunk_ids)
    open_signals = detect_signals(open_snap.hunks)
    picks = select_lenses(open_signals, deep=round_.deep, docs_only=open_snap.docs_only)
    if created or not packet.is_file():
        ctx = _packet_context(
            ws, snap, ledger, round_, index, symbols, task=args.task or ws.task, picks=picks
        )
        _write_text(packet, render_packet(ctx))
    hint = None if round_.deep else deep_hint(detect_deep_hint_signals(open_snap.hunks))
    _print_prepare_summary(round_, packet, snap, picks, hint, created=created, raised=raised)
    _print_unscanned_files(snap)
    _print_scope_summary(ws, ledger, round_, snap, full_snap)
    if created and not raised:
        prune_round_files(ws.ledger_path.parent, round_.number)
        # Scratch directories outlive rounds: a repro written in round 1 is re-run after the fix.
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


def _owns_gate(ws: Workspace) -> bool:
    """Whether this command acts for the session that owns the active gate (true without one)."""
    return ws.state is None or ws.state.owner.admits(cli_caller(None, os.environ))


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


def _lens_picks(round_: Round, snap: DiffSnapshot) -> tuple[LensPick, ...]:
    """The lenses suggested for the hunks still open in `round_`; carried hunks were reviewed."""
    open_snap = snap.only(round_.open_hunk_ids)
    return select_lenses(
        detect_signals(open_snap.hunks), deep=round_.deep, docs_only=open_snap.docs_only
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
    picks: Sequence[LensPick] | None = None,
) -> PacketContext:
    """What the packet of `round_` shows, with the lenses suggested for the round."""
    return collect_context(
        ws.root,
        snap,
        round_,
        _lens_picks(round_, snap) if picks is None else picks,
        task,
        symbols=symbols,
        ledger=ledger,
        index=index,
    )


def _print_prepare_summary(
    round_: Round,
    packet: Path,
    snap: DiffSnapshot,
    picks: Sequence[LensPick],
    hint: str | None,
    *,
    created: bool,
    raised: bool = False,
) -> None:
    carried = len(round_.hunks) - len(round_.open_hunk_ids)
    changed = snap.added + snap.deleted
    deep = " (deep)" if round_.deep and not raised else ""
    state = "raised to deep (same diff)" if raised else "prepared" if created else "unchanged (same diff)"
    print(
        f"Round {round_.number} {state}{deep}: {len(snap.files)} files, {len(snap.hunks)} hunks, "
        f"{changed} changed lines; {carried} hunks carried, {len(round_.open_hunk_ids)} to review."
    )
    print(f"Packet: {packet}")
    print("Risk map (suggested lenses):")
    for pick in picks:
        print(f"  {pick.lens.id} — {pick.reason}")
    if round_.signals:
        listed, summary = bound_hints(round_.signals)
        print(f"Quality hints: {summary}")
        for sig in listed:
            print(f"  {sig.id} {sig.kind.value} {sig.file}:{sig.line} — {sig.detail}")
    if hint is not None:
        print(hint)
    warning = large_diff_warning(round_)
    if warning is not None:
        print(f"Warning: {warning}")
    print(
        "Next: decide the hunt (lenses, hunters, slices), then have hunters record "
        "`craftsman review add` / `craftsman review cover`."
    )


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
    ctx = _packet_context(ws, snap, ledger, round_, index, symbols, task=ws.task)
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

    def record(current: Ledger) -> tuple[Ledger, str]:
        round_ = require_current_round(current, snap)
        raw = args.files if args.files is not None else args.hunks
        targets = _cover_targets(round_, target_kind, raw, files_of(round_))
        return _record_coverage(current, round_, targets, args.note, target_kind=target_kind)

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
    state: Ledger, round_: Round, raw: Any, files: Callable[[str], list[str]]
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
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, str] | None:
        if reader.failed(kind) or not present:
            return None
        targets = _attempt(
            sink, lambda: _cover_targets(round_, kind, ",".join(listed), files), None
        )
        if targets is None:  # the note is still checked, so one resend fixes both
            if not reader.failed("note"):
                check_coverage_note(one_line(note, warn=False))
            return None
        return _attempt(
            sink, lambda: _record_coverage(state, round_, targets, note, target_kind=kind), None
        )

    return reader.finish(record)


def _cover_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json({"--hunks": args.hunks, "--files": args.files, "--note": args.note})
    raw_entries = _load_json_entries(args.from_json)
    ws = open_workspace(args.cwd)
    snap = ws.snapshot()

    def record_batch(current: Ledger) -> tuple[Ledger, list[str]]:
        round_ = require_current_round(current, snap)
        return _record_entries(
            "cover",
            raw_entries,
            current,
            lambda _index, raw: _cover_entry_label(raw),
            lambda _index, state, raw: _cover_one_from_json(
                state, round_, raw, lambda value: normalize_round_files(ws.root, _cwd(args), value, round_, snap)
            ),
        )

    return _run_batch(ws, args, record_batch, print)


def _record_coverage(
    current: Ledger, round_: Round, targets: Sequence[str], note: str, *, target_kind: TargetKind
) -> tuple[Ledger, str]:
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
    label = ", ".join(targets if target_kind == "files" else open_ids)
    return updated, (
        f"Coverage recorded: {label} in round {round_.number}{skipped}; "
        f"{left} hunk(s) left to cover in this round."
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
        )

    return _run_single(ws, bool(args.check), record, lambda finding: _print_recorded(finding, lines))


def _add_from_json(args: argparse.Namespace) -> int:
    _reject_flags_with_from_json(
        {**_required_finding_flags(args), "--suggestion": args.suggestion}
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
    reader = _EntryReader(raw)
    lens = reader.read("lens", lambda f: f.choice("lens", tuple(sorted(LENS_IDS))), "intent")
    raw_file = reader.read("file", lambda f: f.text("file"), "")
    line = reader.read("line", lambda f: f.integer("line"), 1)
    severity: Severity = reader.read("severity", lambda f: f.choice("severity", SEVERITIES), "minor")
    title = reader.read("title", lambda f: f.text("title"), "")
    scenario = reader.read("scenario", lambda f: f.text("scenario"), "")
    suggestion = reader.read("suggestion", lambda f: f.text_or("suggestion", ""), "")
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
        updated, finding = add_finding(
            current,
            lens=lens,
            path=path,
            line=line,
            severity=severity,
            title=title,
            scenario=scenario,
            suggestion=suggestion,
        )
        return updated, (finding, lines)

    return reader.finish(record)


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
    reader = _EntryReader(raw)
    finding_id = reader.read("id", lambda f: f.text("id"), "")
    verdict: Verdict = reader.read("as", lambda f: f.choice("as", VERDICTS), "dismissed")
    evidence = reader.read("evidence", lambda f: f.text("evidence"), "")
    run_id = reader.read("run", lambda f: f.text_if_present("run"), None)
    reader.reject_unknown()

    def record(sink: list[LedgerError]) -> tuple[Ledger, Finding] | None:
        if reader.failed("id", "as", "evidence", "run"):
            return None
        return set_verdict(state, finding_id, verdict, evidence, run_id)

    return reader.finish(record)


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
        missing = uncovered_hunks(ledger, latest) if latest else []
        return _show_hunks(ws.root, latest, snap, missing, as_json=args.json)
    if args.json:
        payload = {
            "round": _round_json(latest, snap) if latest else None,
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
        print()
        print("Coverage (covered/total non-carried hunks):")
        print(_coverage_line(ledger, latest))
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
    root: Path, latest: Round | None, snap: DiffSnapshot, missing: Sequence[str], *, as_json: bool
) -> int:
    if as_json:
        payload = {
            "round": _round_json(latest, snap) if latest else None,
            "stale": bool(latest is not None and latest.fingerprint != snap.fingerprint),
            "hunks": _hunks_json(root, latest, snap, missing) if latest else [],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if latest is None:
        print("No review round yet; run `craftsman review prepare`.")
        return 0
    print("\n".join(_hunk_map_lines(root, latest, snap, missing)))
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
    """The round's hunks; for a current round each says whether it is `covered` (when `missing` is given)."""
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
                **({} if missing is None else {"covered": ref.id not in missing}),
            }
        )
    return items


def _format_span(path: str, start: int, end: int, has_range: bool, *, bare_zero: bool = False) -> str:
    if has_range:
        return f"{path}:{start}-{end}"
    return path if bare_zero else f"{path}:{start}"


def _hunk_map_lines(root: Path, round_: Round, snap: DiffSnapshot, missing: Sequence[str]) -> list[str]:
    """One compact `H#  path:start-end  scope|status` line per hunk still to cover, then `N left`.

    A stale round lists its HunkRef fields instead: its coverage no longer matches the code.
    """
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
        if e["id"] in missing
    ]
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


def _coverage_json(ledger: Ledger, round_: Round) -> dict[str, Any]:
    missing = uncovered_hunks(ledger, round_)
    total = len(round_.open_hunk_ids)
    return {"covered": total - len(missing), "total": total, "missing": missing}


def _coverage_line(ledger: Ledger, round_: Round) -> str:
    entry = _coverage_json(ledger, round_)
    missing = f" (missing {','.join(entry['missing'])})" if entry["missing"] else ""
    return f"  {entry['covered']}/{entry['total']} hunks{missing}"


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
    return " ".join(text.split())


def _cell(text: str) -> str:
    """Text safe for one Markdown table cell: pipes escaped, line breaks flattened."""
    return inline_md(text, table=True)


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
    ws: Workspace, *, review_only: bool
) -> tuple[Ledger, DiffSnapshot, bool, GateReport]:
    if review_only and ws.state is not None:
        raise LedgerError(
            "--review-only is not allowed during an active /craftsman gate session; "
            "in /craftsman the full gate applies (run `craftsman review check` without --review-only)"
        )
    ledger = load(ws.ledger_path)
    deep = effective_deep(ledger, ws.state is not None and ws.state.deep)
    snap = ws.snapshot(ledger)
    policy = GatePolicy(deep=deep, isolation_evidence=ws.isolation_evidence(ledger))
    return ledger, snap, deep, evaluate(ledger, snap, policy, review_only=review_only)


def _check(args: argparse.Namespace) -> int:
    ws = open_workspace(args.cwd)
    ledger, _, _, report = _evaluate(ws, review_only=args.review_only)
    if args.json:
        print(json.dumps(_report_json(report), indent=2, ensure_ascii=False))
    else:
        print(render_report(report))
        if args.review_only:
            _warn_new_out_of_scope(ws, ledger)
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
    ledger, snap, deep, report = _evaluate(ws, review_only=args.review_only)
    if args.partial:
        report = replace(report, partial=True)
    groups = _finding_groups(ledger.findings)
    runs = passing_runs(ledger, snap.fingerprint)
    if args.json:
        payload = {
            **_report_json(report),
            "deep": deep,
            "rounds": len(ledger.rounds),
            "base_sha": snap.base_sha,
            "diff": {"files": len(snap.files), "added": snap.added, "deleted": snap.deleted},
            "not_covered": _not_covered(ledger),
            "findings": {name: [f.to_json() for f in items] for name, items in groups.items()},
            "runs": [run.to_json() for run in runs],
            "waived_runs": [run.to_json() for run in ledger.runs if run.waiver is not None],
            "agent_usage": usage_json(ledger),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("\n".join(_report_lines(ledger, snap, deep, report, groups, runs)))
        _warn_new_out_of_scope(ws, ledger)
    if args.review_only:
        prune_all_temp_dirs(ws.ledger_path.parent)
    return 0


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
    check = (
        "craftsman review check --review-only"
        if report.review_only or report.partial
        else "craftsman review check"
    )
    return f"{line} — {report.issue_codes}; see `{check}`"


def _not_covered(ledger: Ledger) -> list[str]:
    return uncovered_hunks(ledger, ledger.rounds[-1]) if ledger.rounds else []


def _not_covered_lines(ledger: Ledger, snap: DiffSnapshot, report: GateReport) -> list[str]:
    missing = _not_covered(ledger)
    if missing:
        return ["", "## Not covered", f"- Hunks: {', '.join(missing)}"]
    if report.partial and not ledger.rounds and not snap.is_empty:
        return ["", "## Not covered", "- No review round prepared yet."]
    return []


def _report_finding_lines(ledger: Ledger, groups: dict[str, list[Finding]]) -> list[str]:
    if not ledger.findings:
        return ["No findings recorded."]
    detail: dict[str, Callable[[Finding], str]] = {
        "fixed": lambda f: f"Fix: {inline_md(f.resolution_note)}",
        "accepted": lambda f: f"Accepted because: {inline_md(f.resolution_note)}",
        "open": lambda f: (
            f"Verdict: {f.verdict}" if f.verdict is not None else "Verdict: none yet (unverified)"
        ),
        "question": lambda f: f"Question: {inline_md(f.verdict_evidence)}",
        "dismissed": lambda f: f"Dismissed: {inline_md(f.verdict_evidence)}",
    }
    lines: list[str] = []
    for name, items in groups.items():
        if items:
            lines.append(f"**{name.capitalize()} ({len(items)})**")
            lines.extend(
                f"- {f.id} · {f.severity} · `{f.path}:{f.line}` — {inline_md(f.title)}. {detail[name](f)}"
                for f in items
            )
    return lines


def _not_assessed_lines(report: GateReport) -> list[str]:
    if report.self_reviewed:
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
            f"- {run.id} `{run.command}` ({run.kind}, exit {run.exit_code}) — {inline_md(waiver.reason)}"
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
        f"- Base `{snap.base_sha[:7]}` · {'deep · ' if deep else ''}{len(ledger.rounds)} review round(s) · "
        f"{len(snap.files)} files, +{snap.added}/−{snap.deleted} lines",
        _report_status_line(report),
        *_not_covered_lines(ledger, snap, report),
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
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"hunks"|"files", "note"} objects (use - for stdin)',
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
        "--from-json",
        metavar="PATH",
        help='Bulk JSON list of {"lens", "file", "line", "severity", "title", "scenario", "suggestion"?} objects (use - for stdin)',
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
            help="Check only review completeness (round, coverage, verdicts), without requiring fixes or test runs",
        )
    sub.choices["report"].add_argument(
        "--partial",
        action="store_true",
        help="Render a partial review-only report when some hunks or verdicts could not be completed",
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
