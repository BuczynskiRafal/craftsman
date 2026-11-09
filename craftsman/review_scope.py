"""Which files and hunks a review covers: scope flags, path queries, `--files` spelling and view names."""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from craftsman.diffscan import (
    DiffSnapshot,
    Hunk,
    snapshot,
    staged_paths,
    untracked_paths,
)
from craftsman.ledger import (
    FIXED_SCOPE_ERROR,
)
from craftsman.packet import (
    expand_hunk_spec,
)
from craftsman.records import (
    FieldError,
    Ledger,
    LedgerError,
    Round,
)
from craftsman.review_common import comma_list, cwd_of
from craftsman.workspace import (
    Workspace,
)

MAX_VIEW_SLUG_CHARS = 60


VIEW_HASH_CHARS = 8


MAX_IGNORED_PATHS_SHOWN = 5


class UnmatchedPathError(FieldError):
    """A syntactically valid `--paths` entry matched no changed files in the current diff."""


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


def prepare_scope_and_snapshot(
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
            queries = _parse_path_queries(ws.root, cwd_of(args), args.add_paths, field="add_paths")
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
    except UnmatchedPathError:
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
        queries = _parse_path_queries(ws.root, cwd_of(args), args.paths)
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
    tokens = comma_list(raw_paths)
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
            raise UnmatchedPathError(
                field,
                f"{raw!r} matched no changed files in the diff since {base_sha[:12]} "
                f"(changed files: {changed})",
            )
        matched_hunks.extend(hits)
    return _scope_from_hunks(matched_hunks)


def format_path_sample(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:MAX_IGNORED_PATHS_SHOWN])
    extra = len(paths) - MAX_IGNORED_PATHS_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


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


def view_hunk_ids(
    round_: Round, args: argparse.Namespace, files: Sequence[str] | None = None
) -> tuple[str, ...]:
    if args.hunks is not None:
        return expand_hunk_spec(args.hunks, [hunk.id for hunk in round_.hunks])
    paths = list(files) if files is not None else comma_list(args.files)
    known = {hunk.path for hunk in round_.hunks}
    unknown = [path for path in paths if path not in known]
    if not paths or unknown:
        shown = ", ".join(unknown) or "(empty)"
        raise FieldError(
            "files", f"names files not in round {round_.number}: {shown}; changed files: {', '.join(sorted(known))}"
        )
    return tuple(hunk.id for hunk in round_.hunks if hunk.path in set(paths))


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
    return [_round_file(root, cwd, item, changed, renamed) for item in comma_list(raw)]


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


def finding_path(root: Path, cwd: Path, raw: str, snap: DiffSnapshot) -> str:
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
