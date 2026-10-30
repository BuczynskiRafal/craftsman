#!/usr/bin/env python3
"""Command-line entrypoint for `craftsman export`.

Targets:
1. GitHub Copilot, user level: the global instructions in the VS Code prompts folder and its
   profiles (`--user-dir`), Agent Skills in `~/.copilot/skills/<skill>/` (`--copilot-skills-dir`),
   and custom agents plus hooks that run this interpreter by absolute path in `~/.copilot`
   (`--copilot-home`).
2. GitHub Copilot, workspace (`--workspace`): `.github/skills`, `.github/agents`,
   `.github/hooks/craftsman.json` (runs `craftsman` from PATH) and `.github/copilot-instructions.md`.
3. Claude Code (`--claude-dir`): slash commands, rules, the reviewer/verifier subagents and the
   Craftsman hooks merged into `settings.json` (skip them with `--no-claude-hooks`).
4. OpenAI Codex / Cursor (`--agents-workspace`): `AGENTS.md` and `.cursor/rules/craftsman.mdc`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from craftsman.assets import iter_skills
from craftsman.generated import ExportError, FileChange
from craftsman.hook_command import absolute_hook_prefixes
from craftsman.paths import (
    default_claude_dir,
    default_copilot_home,
    default_copilot_skills_dir,
    default_repo_dir,
    default_vscode_prompts_dir,
)
from craftsman.platforms.agents_md import export_to_agents_workspace
from craftsman.platforms.claude import export_to_claude_user, merge_claude_hooks
from craftsman.platforms.copilot import (
    DUPLICATE_SCOPE_USER_TO_WS_MSG,
    DUPLICATE_SCOPE_WS_TO_USER_MSG,
    export_copilot_home,
    export_to_copilot_skills,
    export_to_workspace,
    export_vscode_instructions,
    has_user_copilot_install,
    has_workspace_copilot_install,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craftsman export",
        description="Export Craftsman to GitHub Copilot, Claude Code, OpenAI Codex, and Cursor.",
    )
    parser.add_argument("--repo-dir", type=Path, help="Path to the Craftsman repository root.")
    parser.add_argument(
        "--user-dir",
        nargs="?",
        const="",
        help="Write the global Copilot instructions into a VS Code User prompts folder and its "
        "profiles (default: this operating system's Code/User/prompts).",
    )
    parser.add_argument(
        "--copilot-skills-dir",
        nargs="?",
        const="",
        help="Write the Copilot Agent Skills (default: ~/.copilot/skills).",
    )
    parser.add_argument(
        "--copilot-home",
        nargs="?",
        const="",
        help="Write the Copilot custom agents and hooks/craftsman.json (default: ~/.copilot); "
        "the hooks run this Python interpreter by absolute path.",
    )
    parser.add_argument(
        "--allow-duplicate-scopes",
        action="store_true",
        help="Allow exporting Copilot files in both user (~/.copilot) and workspace (.github) scopes.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Export to a project workspace (.github/skills, agents, hooks and copilot-instructions.md).",
    )
    parser.add_argument(
        "--claude-dir",
        nargs="?",
        const="",
        help="Export commands, rules, subagents and the hooks in settings.json to a Claude Code "
        "directory (default: ~/.claude); settings.json.bak is written before its first change.",
    )
    parser.add_argument(
        "--no-claude-hooks",
        action="store_true",
        help="With --claude-dir: do not touch <claude-dir>/settings.json.",
    )
    parser.add_argument(
        "--agents-workspace",
        type=Path,
        help="Export universal AGENTS.md and .cursor/rules/craftsman.mdc into a project workspace.",
    )
    return parser


def _validate_export_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.no_claude_hooks and args.claude_dir is None:
        parser.error("--no-claude-hooks requires --claude-dir")
    targets = (
        args.user_dir,
        args.copilot_skills_dir,
        args.copilot_home,
        args.workspace,
        args.claude_dir,
        args.agents_workspace,
    )
    if all(target is None for target in targets):
        parser.error(
            "Specify at least one target: --user-dir, --copilot-skills-dir, --copilot-home, "
            "--workspace <path>, --claude-dir, or --agents-workspace <path>"
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_export_args(parser, args)
    try:
        repo_dir = (args.repo_dir or default_repo_dir()).resolve()
        if not iter_skills(repo_dir):
            raise ExportError(f"no skill manifests (skills/*/SKILL.md) found under {repo_dir}")
        _run_exports(args, repo_dir)
    except (ExportError, OSError) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    return 0


def _chosen(raw: str | None, default: Callable[[], Path]) -> Path | None:
    """The folder a `nargs="?"` option names: None when not given, its default when bare."""
    if raw is None:
        return None
    return Path(raw) if raw else default()


def _check_copilot_scope_conflict(args: argparse.Namespace, repo_dir: Path) -> None:
    if args.allow_duplicate_scopes:
        return
    user_options = (args.user_dir, args.copilot_skills_dir, args.copilot_home)
    if args.workspace and (
        any(option is not None for option in user_options) or has_user_copilot_install(repo_dir)
    ):
        raise ExportError(DUPLICATE_SCOPE_USER_TO_WS_MSG)
    if "" in user_options and has_workspace_copilot_install(Path.cwd(), repo_dir):
        raise ExportError(DUPLICATE_SCOPE_WS_TO_USER_MSG)


def _report(label: str, target: Path, changes: Sequence[FileChange]) -> None:
    print(f"[OK] Exported {label} -> {target}")
    for change in changes:
        if change.backup is not None:
            print(f"  ! kept a copy of {change.path} -> {change.backup}")


def _run_exports(args: argparse.Namespace, repo_dir: Path) -> None:
    _check_copilot_scope_conflict(args, repo_dir)
    if (prompts := _chosen(args.user_dir, default_vscode_prompts_dir)) is not None:
        _report("global Copilot instructions", prompts, export_vscode_instructions(repo_dir, prompts))
    if (skills_dir := _chosen(args.copilot_skills_dir, default_copilot_skills_dir)) is not None:
        skills = export_to_copilot_skills(repo_dir, skills_dir)
        _report(f"{len(skills)} Copilot Agent Skills", skills_dir, skills)
    if (copilot_home := _chosen(args.copilot_home, default_copilot_home)) is not None:
        home_export = export_copilot_home(repo_dir, copilot_home, absolute_hook_prefixes())
        _report(
            "Copilot custom agents and hooks", copilot_home, [*home_export.agents, home_export.hooks]
        )
    if args.workspace is not None:
        workspace = export_to_workspace(repo_dir, args.workspace.resolve())
        changes = [*workspace.skills, *workspace.agents, workspace.hooks, workspace.instructions]
        _report("workspace skills, agents, hooks and instructions", args.workspace / ".github", changes)
    if (claude_dir := _chosen(args.claude_dir, default_claude_dir)) is not None:
        claude = export_to_claude_user(repo_dir, claude_dir)
        changes = [*claude.commands, *claude.agents, claude.rules]
        if not args.no_claude_hooks:
            bash_prefix, _ = absolute_hook_prefixes()
            changes.append(merge_claude_hooks(claude_dir / "settings.json", bash_prefix))
        _report("Claude Code commands, subagents, rules and hooks", claude_dir, changes)
    if args.agents_workspace is not None:
        agents = export_to_agents_workspace(repo_dir, args.agents_workspace.resolve())
        _report(
            "AGENTS.md and the Cursor rule",
            args.agents_workspace,
            [agents.agents_md, agents.cursor_rule],
        )


if __name__ == "__main__":
    raise SystemExit(main())
