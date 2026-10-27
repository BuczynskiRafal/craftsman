#!/usr/bin/env python3
"""`craftsman install`: Antigravity, Claude Code, GitHub Copilot, workspaces and global git hooks.

Every target records what it created and which user files it merged into in the manifest of its
base folder; `craftsman uninstall` works from those manifests alone.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from craftsman.export import (
    CURSOR_RULE_HEADER,
    DUPLICATE_SCOPE_USER_TO_WS_MSG,
    DUPLICATE_SCOPE_WS_TO_USER_MSG,
    HOOKS_FILENAME,
    ExportError,
    FileChange,
    absolute_hook_prefixes,
    claude_agent_templates,
    default_claude_dir,
    default_copilot_home,
    default_copilot_skills_dir,
    default_repo_dir,
    default_vscode_prompts_dir,
    default_vscode_user_dir,
    export_copilot_agents,
    export_copilot_home,
    export_to_claude_user,
    export_to_copilot_skills,
    export_vscode_instructions,
    has_user_copilot_install,
    has_workspace_copilot_install,
    is_craftsman_generated_file,
    iter_skills,
    load_agent_templates,
    merge_claude_hooks,
    read_json_object,
    render_agents_md,
    render_combined_rules,
    upsert_managed_markdown,
    user_home,
    vscode_prompt_dirs,
    write_copilot_hooks,
    write_generated,
    write_json_object,
)
from craftsman.files import UnsafePathError, copy_once_backup, exists
from craftsman.githooks import git_hooks_base_dir, install_git_hooks_target
from craftsman.manifest import InstallError, ModifiedKind, TargetKind, load_manifest
from craftsman.staging import (
    EventKind,
    InstallMode,
    TargetSession,
    Tracker,
    install_runtime,
    install_tree,
    path_key,
    require_within,
    revert_modified_file,
)


@dataclass(frozen=True)
class InstallConfig:
    repo_dir: Path
    mode: InstallMode
    auto_detect: bool
    antigravity: bool
    claude: bool
    claude_hooks: bool
    copilot: bool
    allow_duplicate_scopes: bool
    copilot_workspace: Path | None
    agents_workspace: Path | None
    git_hooks: bool
    dry_run: bool


# --- detection ------------------------------------------------------------------------------


def detect_antigravity(home: Path) -> bool:
    return (
        (home / ".gemini" / "antigravity").is_dir()
        or (home / ".gemini" / "config").is_dir()
        or bool(os.environ.get("AGY_MANAGED_ENV"))
        or bool(os.environ.get("ANTIGRAVITY_AGENT"))
    )


def detect_claude(home: Path) -> bool:
    return default_claude_dir(home).is_dir() or bool(os.environ.get("CLAUDECODE"))


def detect_vscode_copilot(home: Path) -> bool:
    return (
        default_copilot_home(home).is_dir()
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or bool(os.environ.get("VSCODE_PID"))
        or bool(os.environ.get("VSCODE_IPC_HOOK_CLI"))
        or default_vscode_user_dir(home).is_dir()
        or shutil.which("code") is not None
    )


def resolve_install_config(config: InstallConfig, home: Path, tracker: Tracker) -> InstallConfig:
    """Add the detected platforms to the requested ones; GitHub Copilot when none is found."""
    if not config.auto_detect:
        return config
    detected = {
        "Google Antigravity": detect_antigravity(home),
        "Claude Code": detect_claude(home),
        "GitHub Copilot (VS Code)": detect_vscode_copilot(home),
    }
    for name, found in detected.items():
        if found:
            tracker.add(EventKind.NOTE, f"Detected: {name}")
    nothing = not any(detected.values())
    if nothing:
        tracker.add(EventKind.NOTE, "No AI coding platform detected; installing for GitHub Copilot.")
    return replace(
        config,
        auto_detect=False,
        antigravity=config.antigravity or detected["Google Antigravity"],
        claude=config.claude or detected["Claude Code"],
        copilot=config.copilot or detected["GitHub Copilot (VS Code)"] or nothing,
    )


# --- Antigravity ----------------------------------------------------------------------------


def install_antigravity_target(repo_dir: Path, home: Path, mode: InstallMode, tracker: Tracker) -> None:
    config_dir = home / ".gemini" / "config"
    hooks_json = config_dir / "hooks.json"
    # Read before anything is written: a hooks.json that is no JSON object stops the install.
    existing_hooks = read_json_object(hooks_json) if exists(hooks_json) else None
    session = TargetSession.begin(config_dir, TargetKind.ANTIGRAVITY, home, tracker)
    for sub in ("skills", "rules", "scripts", "plugins"):
        session.make_dirs(config_dir / sub)
    install_runtime(session, repo_dir, mode)
    if mode is InstallMode.PLUGIN:
        _revert_hooks_json(session, hooks_json)
        install_tree(session, repo_dir, config_dir / "plugins" / "craftsman", mode)
    else:
        for rule in sorted((repo_dir / "rules").glob("*.md")):
            install_tree(session, rule, config_dir / "rules" / rule.name, mode)
        for skill in iter_skills(repo_dir):
            install_tree(session, skill.directory, config_dir / "skills" / skill.name, mode)
        _merge_hooks_json(session, read_json_object(repo_dir / "hooks.json"), hooks_json, existing_hooks)
    session.finish()


def _revert_hooks_json(session: TargetSession, hooks_json: Path) -> None:
    """Plugin mode ships its hooks in the plugin: take the ones a copy install merged back out."""
    previous = session.previous
    if previous is not None and previous.hooks_json_keys:
        revert_modified_file(hooks_json, ModifiedKind.HOOKS_JSON, previous, session.tracker)


def _merge_hooks_json(
    session: TargetSession, ours: dict[str, Any], target: Path, existing: dict[str, Any] | None
) -> None:
    """Add Craftsman's entries to the user's hooks.json; the user's `enabled` flags are kept."""
    stale = set(session.previous.hooks_json_keys if session.previous else ()) - set(ours)
    merged = {key: value for key, value in (existing or {}).items() if key not in stale}
    for key, entry in ours.items():
        merged[key] = _with_users_enabled_flag(entry, merged.get(key))
    session.merged_hooks_json(target, ours)
    if existing is None or _created_before(session, target):
        session.created(target)
    elif merged == existing:
        session.tracker.add(EventKind.UNCHANGED, target)
        return
    else:
        if (backup := copy_once_backup(target, dry_run=session.dry_run)) is not None:
            session.tracker.backed_up(target, backup)
        session.tracker.add(EventKind.INSTALLED, target)
    if not session.dry_run:
        write_json_object(target, merged)


def _created_before(session: TargetSession, path: Path) -> bool:
    return session.previous is not None and path_key(path) in session.previous.created_paths


def _with_users_enabled_flag(ours: Any, theirs: Any) -> Any:
    if isinstance(ours, dict) and isinstance(theirs, dict) and isinstance(theirs.get("enabled"), bool):
        return {**ours, "enabled": theirs["enabled"]}
    return ours


# --- Claude Code ----------------------------------------------------------------------------


def install_claude_target(repo_dir: Path, home: Path, *, with_hooks: bool, tracker: Tracker) -> None:
    claude_dir = default_claude_dir(home)
    settings = claude_dir / "settings.json"
    # Settings Craftsman cannot merge into, or take its hooks back out of, stop the install up front.
    bash_prefix = absolute_hook_prefixes()[0] if with_hooks else None
    if bash_prefix is not None:
        merge_claude_hooks(settings, bash_prefix, dry_run=True)
    elif exists(settings):
        read_json_object(settings)
    session = TargetSession.begin(claude_dir, TargetKind.CLAUDE, home, tracker)
    for sub in ("commands", "rules", "agents"):
        session.make_dirs(claude_dir / sub)
    for skill in iter_skills(repo_dir):
        session.stage(claude_dir / "commands" / f"{skill.name}.md")
    session.stage(claude_dir / "rules" / "craftsman-rules.md")
    for template in claude_agent_templates(repo_dir):
        session.stage(claude_dir / "agents" / f"{template.name}.md")
    if not tracker.dry_run:
        export_to_claude_user(repo_dir, claude_dir)
    if bash_prefix is not None:
        _merge_claude_settings(session, settings, bash_prefix)
    elif session.previous is not None and session.previous.modified_files.get(path_key(settings)):
        revert_modified_file(settings, ModifiedKind.CLAUDE_SETTINGS, session.previous, tracker)
    session.finish()


def _merge_claude_settings(session: TargetSession, settings: Path, bash_prefix: str) -> None:
    existed = exists(settings)
    change = merge_claude_hooks(settings, bash_prefix, dry_run=session.dry_run)
    session.modified(settings, ModifiedKind.CLAUDE_SETTINGS)
    if not existed or _created_before(session, settings):
        session.created(settings)
    else:
        _report_change(session.tracker, change)


def _report_change(tracker: Tracker, change: FileChange) -> None:
    if change.backup is not None:
        tracker.backed_up(change.path, change.backup)
    tracker.add(EventKind.INSTALLED if change.changed else EventKind.UNCHANGED, change.path)


# --- GitHub Copilot -------------------------------------------------------------------------


def install_copilot_target(repo_dir: Path, home: Path, tracker: Tracker) -> None:
    copilot_home = default_copilot_home(home)
    prompts = default_vscode_prompts_dir(home)
    prefixes = absolute_hook_prefixes()  # a failure here stops the install before any write
    session = TargetSession.begin(copilot_home, TargetKind.COPILOT, home, tracker)
    for prompts_dir in vscode_prompt_dirs(prompts):
        session.make_dirs(prompts_dir)
        session.stage(prompts_dir / "craftsman-rules.instructions.md")
    _stage_copilot_files(session, repo_dir, copilot_home)
    if not tracker.dry_run:
        export_vscode_instructions(repo_dir, prompts)
        export_to_copilot_skills(repo_dir, default_copilot_skills_dir(home))
        export_copilot_home(repo_dir, copilot_home, prefixes)
    session.finish()


def _stage_copilot_files(session: TargetSession, repo_dir: Path, root: Path) -> None:
    """Skills, agents and the hooks file below `root` (`~/.copilot` or a workspace's `.github`)."""
    for sub in ("skills", "agents", "hooks"):
        session.make_dirs(root / sub)
    for skill in iter_skills(repo_dir):
        session.stage(root / "skills" / skill.name)
    for template in load_agent_templates(repo_dir):
        session.stage(root / "agents" / f"{template.name}.agent.md")
    session.stage(root / "hooks" / HOOKS_FILENAME)


def install_copilot_workspace_target(
    repo_dir: Path, workspace: Path, home: Path, tracker: Tracker
) -> None:
    session = TargetSession.begin(workspace, TargetKind.COPILOT_WORKSPACE, home, tracker, workspace)
    github_dir = workspace / ".github"
    _stage_copilot_files(session, repo_dir, github_dir)
    if not tracker.dry_run:
        export_to_copilot_skills(repo_dir, github_dir / "skills")
        export_copilot_agents(repo_dir, github_dir / "agents")
        write_copilot_hooks(github_dir / "hooks", "craftsman", "craftsman")
    rules = render_combined_rules(repo_dir / "rules", with_frontmatter=False)
    _upsert_markdown(session, github_dir / "copilot-instructions.md", rules)
    session.finish()


def install_agents_workspace_target(
    repo_dir: Path, workspace: Path, home: Path, tracker: Tracker
) -> None:
    session = TargetSession.begin(workspace, TargetKind.AGENTS_WORKSPACE, home, tracker, workspace)
    cursor_rule = workspace / ".cursor" / "rules" / "craftsman.mdc"
    session.make_dirs(cursor_rule.parent)
    session.stage(cursor_rule)
    content = render_agents_md(repo_dir)
    if not tracker.dry_run:
        write_generated(cursor_rule, CURSOR_RULE_HEADER + content, owned=is_craftsman_generated_file)
    _upsert_markdown(session, workspace / "AGENTS.md", content)
    session.finish()


def _upsert_markdown(session: TargetSession, target: Path, content: str) -> None:
    """Merge the Craftsman block into a workspace Markdown file (where a symlink inside the
    workspace points, as `AGENTS.md -> CLAUDE.md` setups do)."""
    require_within(target, (session.base_dir,))
    existed = target.is_file()
    session.make_dirs(target.parent)
    change = upsert_managed_markdown(target, content, within=session.base_dir, dry_run=session.dry_run)
    session.modified(target, ModifiedKind.MANAGED_MARKDOWN)
    if not existed or _created_before(session, target):
        session.created(target)
    else:
        _report_change(session.tracker, change)


# --- running --------------------------------------------------------------------------------


def _check_copilot_scopes(config: InstallConfig, home: Path) -> None:
    if config.allow_duplicate_scopes:
        return
    if config.copilot_workspace is not None and (
        config.copilot or has_user_copilot_install(config.repo_dir, home)
    ):
        raise InstallError(DUPLICATE_SCOPE_USER_TO_WS_MSG)
    workspace = config.copilot_workspace or Path.cwd()
    if config.copilot and has_workspace_copilot_install(workspace, config.repo_dir):
        raise InstallError(DUPLICATE_SCOPE_WS_TO_USER_MSG)


def run_install(config: InstallConfig, tracker: Tracker, home: Path | None = None) -> None:
    home = user_home(home)
    repo_dir = config.repo_dir
    if not (repo_dir / "skills").is_dir() or not (repo_dir / "rules").is_dir():
        raise InstallError(f"Repository assets (skills/, rules/) not found under {repo_dir}")
    config = resolve_install_config(config, home, tracker)
    _check_copilot_scopes(config, home)
    hooks_base = (
        git_hooks_base_dir(home, with_antigravity=_has_antigravity(config, home))
        if config.git_hooks
        else None
    )
    if config.antigravity:
        install_antigravity_target(repo_dir, home, config.mode, tracker)
    if config.claude:
        install_claude_target(repo_dir, home, with_hooks=config.claude_hooks, tracker=tracker)
    if config.copilot:
        install_copilot_target(repo_dir, home, tracker)
    if config.copilot_workspace is not None:
        install_copilot_workspace_target(repo_dir, config.copilot_workspace.resolve(), home, tracker)
    if config.agents_workspace is not None:
        install_agents_workspace_target(repo_dir, config.agents_workspace.resolve(), home, tracker)
    if hooks_base is not None:
        install_git_hooks_target(repo_dir, hooks_base, home, config.mode, tracker)


def _has_antigravity(config: InstallConfig, home: Path) -> bool:
    """Whether this run installs Antigravity or its manifest records an earlier install."""
    if config.antigravity:
        return True
    manifest = load_manifest(home / ".gemini" / "config")
    return manifest is not None and TargetKind.ANTIGRAVITY in manifest.targets


def _build_install_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craftsman install",
        description="Install Craftsman across Antigravity, Claude Code, GitHub Copilot, and global Git hooks.",
    )
    parser.add_argument("--repo-dir", type=Path, help="Path to the Craftsman repository root.")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Also install for every detected platform (the default when no target is given).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Target all platforms: Antigravity + Claude Code + GitHub Copilot + Git Hooks.",
    )
    parser.add_argument(
        "--antigravity",
        action="store_true",
        help="Target skills, rules, scripts, and hooks.json in ~/.gemini/config/.",
    )
    parser.add_argument(
        "--claude",
        action="store_true",
        help="Target Claude Code slash commands, rules, reviewer/verifier subagents and gate hooks in ~/.claude/.",
    )
    parser.add_argument(
        "--no-claude-hooks",
        action="store_true",
        help="With the Claude Code target: leave the Craftsman gate hooks out of ~/.claude/settings.json.",
    )
    parser.add_argument(
        "--copilot",
        action="store_true",
        help="Target GitHub Copilot user-level instructions, skills, custom agents and hooks.",
    )
    parser.add_argument(
        "--allow-duplicate-scopes",
        action="store_true",
        help="Allow installing Copilot files in both user (~/.copilot) and workspace (.github) scopes.",
    )
    parser.add_argument(
        "--copilot-workspace",
        type=Path,
        metavar="<path>",
        help="Target .github/{skills,agents,hooks} and copilot-instructions.md in a workspace.",
    )
    parser.add_argument(
        "--agents-workspace",
        type=Path,
        metavar="<path>",
        help="Target universal AGENTS.md and .cursor/rules/craftsman.mdc in a workspace.",
    )
    hooks = parser.add_mutually_exclusive_group()
    hooks.add_argument(
        "--git-hooks",
        action="store_true",
        help="Target global Git hooks and git config --global core.hooksPath.",
    )
    hooks.add_argument(
        "--no-git-hooks",
        action="store_true",
        help="Leave the global Git hooks out of an auto-detected install.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--copy", action="store_true", help="Copy the Antigravity files and git hooks (the default)."
    )
    modes.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink the Antigravity files and git hooks to this checkout.",
    )
    modes.add_argument(
        "--plugin",
        action="store_true",
        help="Install Antigravity as a toggleable plugin in ~/.gemini/config/plugins/craftsman.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full plan without modifying any files or Git configuration.",
    )
    return parser


def parse_install_args(argv: Sequence[str]) -> InstallConfig:
    parser = _build_install_parser()
    args = parser.parse_args(list(argv))
    if args.no_git_hooks and args.all:
        parser.error("--no-git-hooks cannot be combined with --all")
    mode = (
        InstallMode.SYMLINK if args.symlink else InstallMode.PLUGIN if args.plugin else InstallMode.COPY
    )
    explicit = (
        args.all
        or args.antigravity
        or args.claude
        or args.copilot
        or args.git_hooks
        or args.copilot_workspace is not None
        or args.agents_workspace is not None
    )
    if args.no_claude_hooks and not (args.claude or args.all or not explicit):
        parser.error("--no-claude-hooks applies to the Claude Code target only")
    auto_detect = args.auto or not explicit
    return InstallConfig(
        repo_dir=(args.repo_dir or default_repo_dir()).resolve(),
        mode=mode,
        auto_detect=auto_detect,
        antigravity=args.all or args.antigravity,
        claude=args.all or args.claude,
        claude_hooks=not args.no_claude_hooks,
        copilot=args.all or args.copilot,
        allow_duplicate_scopes=args.allow_duplicate_scopes,
        copilot_workspace=args.copilot_workspace,
        agents_workspace=args.agents_workspace,
        git_hooks=args.all or args.git_hooks or (auto_detect and not args.no_git_hooks),
        dry_run=args.dry_run,
    )


def report(tracker: Tracker, summary: str) -> None:
    tracker.render(sys.stdout, sys.stderr)
    prefix = "[DRY-RUN] " if tracker.dry_run else ""
    print(f"{prefix}{summary}")


def install_main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        config = parse_install_args(args)
        tracker = Tracker(dry_run=config.dry_run)
        try:
            run_install(config, tracker)
        except BaseException:
            tracker.render(sys.stdout, sys.stderr)  # what was done before the failure
            raise
        report(
            tracker,
            f"Summary: {tracker.count(EventKind.INSTALLED)} installed, "
            f"{tracker.count(EventKind.BACKED_UP)} backed up, "
            f"{tracker.count(EventKind.UNCHANGED)} unchanged.",
        )
    except (InstallError, ExportError, UnsafePathError, OSError) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    return 0
