"""`craftsman uninstall`: remove what the install manifests record, and nothing else."""

from __future__ import annotations

import argparse
import errno
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from craftsman.files import exists, remove_path, require_no_symlinked_parent
from craftsman.generated import require_resolves_within
from craftsman.legacy_githooks import craftsman_home_dir, restore_hooks_path
from craftsman.manifest import (
    InstallManifest,
    ModifiedKind,
    TargetKind,
    load_manifest,
    save_manifest,
)
from craftsman.paths import (
    default_claude_dir,
    default_copilot_home,
    default_vscode_user_dir,
    user_home,
)
from craftsman.staging import (
    EventKind,
    Tracker,
    require_within,
    revert_modified_file,
    run_reported,
    stop_roots_for,
)


@dataclass(frozen=True)
class UninstallConfig:
    antigravity: bool
    claude: bool
    copilot: bool
    copilot_workspace: Path | None
    agents_workspace: Path | None
    dry_run: bool


@dataclass(frozen=True)
class BasePlan:
    """The targets to uninstall from one base folder, with every recorded path checked."""

    base_dir: Path
    manifest: InstallManifest
    kinds: tuple[TargetKind, ...]
    stop_roots: frozenset[Path]


def plan_base(
    base_dir: Path,
    kinds: Sequence[TargetKind],
    home: Path,
    *,
    roots: tuple[Path, ...],
    workspace: Path | None = None,
) -> BasePlan | None:
    """The uninstall of `kinds` from `base_dir`, or None when no manifest records them.

    Every path the targets recorded must lie inside `roots` (a folder they created may also be one
    of the configured folders install stops at) and be reached without a symlinked folder; a path
    that is not stops the uninstall before anything changes, since only a tampered manifest can
    name it.
    """
    manifest = load_manifest(base_dir)
    if manifest is None:
        return None
    matching = tuple(kind for kind in kinds if kind in manifest.targets)
    if not matching:
        return None
    stop_roots = stop_roots_for(home, *([workspace] if workspace else []))
    dir_roots = (*roots, *stop_roots)
    for kind in matching:
        target = manifest.targets[kind]
        for raw in (*target.modified_files, *target.created_paths):
            require_within(Path(raw), roots)
            require_no_symlinked_parent(Path(raw), stop_roots)
        for raw, modified_kind in target.modified_files.items():
            if modified_kind is ModifiedKind.MANAGED_MARKDOWN and exists(Path(raw)):
                require_resolves_within(Path(raw), base_dir)
        for raw in target.created_dirs:
            if Path(raw) not in dir_roots:
                require_within(Path(raw), dir_roots)
    return BasePlan(base_dir, manifest, matching, stop_roots)


def apply_plan(plan: BasePlan, home: Path, tracker: Tracker) -> None:
    targets = [plan.manifest.targets[kind] for kind in plan.kinds]
    remaining = plan.manifest.without(plan.kinds)
    handled = {*remaining.created_paths}
    for kind, target in zip(plan.kinds, targets, strict=True):
        if kind is TargetKind.GIT_HOOKS:
            restore_hooks_path(target, home, tracker)
        for raw_path, modified_kind in target.modified_files.items():
            revert_modified_file(Path(raw_path), modified_kind, target, tracker)
        handled.update(target.modified_files)
        for raw_path in sorted(target.created_paths, key=lambda p: len(Path(p).parts), reverse=True):
            if raw_path in handled:
                continue
            handled.add(raw_path)  # two targets of one base folder may share a path
            if remove_path(Path(raw_path), stop_roots=plan.stop_roots, dry_run=tracker.dry_run):
                tracker.add(EventKind.REMOVED, raw_path)
    save_manifest(plan.base_dir, remaining, dry_run=tracker.dry_run)
    if not tracker.dry_run:
        pruned = {raw for target in targets for raw in target.created_dirs}
        _prune_dirs(pruned - set(remaining.created_dirs))


def _prune_dirs(directories: set[str]) -> None:
    """Remove the folders Craftsman created once they are empty; a user's files keep theirs."""
    for raw in sorted(directories, key=lambda d: len(Path(d).parts), reverse=True):
        directory = Path(raw)
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            directory.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise


def run_uninstall(config: UninstallConfig, tracker: Tracker, home: Path | None = None) -> None:
    """Check every selected manifest first, then uninstall: a refusal changes nothing."""
    home = user_home(home)
    plans = [plan for plan in _plans(config, home) if plan is not None]
    for plan in plans:
        apply_plan(plan, home, tracker)


def _plans(config: UninstallConfig, home: Path) -> Iterator[BasePlan | None]:
    config_dir = home / ".gemini" / "config"
    gemini_kinds = [
        *([TargetKind.ANTIGRAVITY] if config.antigravity else []),
        TargetKind.GIT_HOOKS,
    ]
    yield plan_base(config_dir, gemini_kinds, home, roots=(config_dir,))
    craftsman_home = craftsman_home_dir(home)
    if craftsman_home.resolve() != config_dir.resolve():
        yield plan_base(craftsman_home, [TargetKind.GIT_HOOKS], home, roots=(craftsman_home,))
    if config.claude:
        claude_dir = default_claude_dir(home)
        yield plan_base(claude_dir, [TargetKind.CLAUDE], home, roots=(claude_dir,))
    if config.copilot:
        # The instructions live in VS Code's folder, in profiles that may be gone by now.
        copilot_home = default_copilot_home(home)
        roots = (copilot_home, default_vscode_user_dir(home))
        yield plan_base(copilot_home, [TargetKind.COPILOT], home, roots=roots)
    for workspace, kind in (
        (config.copilot_workspace, TargetKind.COPILOT_WORKSPACE),
        (config.agents_workspace, TargetKind.AGENTS_WORKSPACE),
    ):
        if workspace is not None:
            resolved = workspace.resolve()
            yield plan_base(resolved, [kind], home, roots=(resolved,), workspace=resolved)


def _build_uninstall_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craftsman uninstall",
        description="Remove what the Craftsman install manifests record and restore any 3.x core.hooksPath. "
        "Without a target, every user-level target is uninstalled.",
    )
    parser.add_argument("--all", action="store_true", help="Every user-level target.")
    parser.add_argument(
        "--antigravity", action="store_true", help="The Antigravity install in ~/.gemini/config."
    )
    parser.add_argument("--claude", action="store_true", help="The Claude Code install in ~/.claude.")
    parser.add_argument("--copilot", action="store_true", help="The GitHub Copilot user-level install.")
    parser.add_argument(
        "--copilot-workspace", type=Path, metavar="<path>", help="A workspace's Copilot install."
    )
    parser.add_argument(
        "--agents-workspace", type=Path, metavar="<path>", help="A workspace's AGENTS.md install."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without modifying any files or Git configuration.",
    )
    return parser


def parse_uninstall_args(argv: Sequence[str]) -> UninstallConfig:
    parser = _build_uninstall_parser()
    args = parser.parse_args(list(argv))
    explicit = (
        args.all
        or args.antigravity
        or args.claude
        or args.copilot
        or args.copilot_workspace is not None
        or args.agents_workspace is not None
    )
    every = args.all or not explicit
    return UninstallConfig(
        antigravity=every or args.antigravity,
        claude=every or args.claude,
        copilot=every or args.copilot,
        copilot_workspace=args.copilot_workspace,
        agents_workspace=args.agents_workspace,
        dry_run=args.dry_run,
    )


def _uninstall_summary(tracker: Tracker) -> str:
    removed, restored = tracker.count(EventKind.REMOVED), tracker.count(EventKind.RESTORED)
    if not removed and not restored:
        return "Nothing to uninstall."
    return f"Uninstall summary: {removed} path(s) removed, {restored} configuration(s) restored."


def uninstall_main(argv: Sequence[str] | None = None) -> int:
    config = parse_uninstall_args(list(sys.argv[1:] if argv is None else argv))
    return run_reported(
        config.dry_run, lambda tracker: run_uninstall(config, tracker), _uninstall_summary
    )
