#!/usr/bin/env python3
"""`craftsman install`: Antigravity, Claude Code, GitHub Copilot and workspaces.

Every target records what it created and which user files it merged into in the manifest of its
base folder; `craftsman uninstall` works from those manifests alone.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from craftsman.files import UnsafePathError
from craftsman.generated import ExportError
from craftsman.legacy_githooks import legacy_git_hooks_bases
from craftsman.manifest import InstallError, TargetKind
from craftsman.paths import default_claude_dir, default_repo_dir, user_home
from craftsman.platforms.agents_md import install_agents_workspace_target
from craftsman.platforms.antigravity import detect_antigravity, install_antigravity_target
from craftsman.platforms.claude import (
    detect_claude,
    install_claude_target,
    is_claude_plugin_active,
)
from craftsman.platforms.copilot import (
    DUPLICATE_SCOPE_USER_TO_WS_MSG,
    DUPLICATE_SCOPE_WS_TO_USER_MSG,
    detect_vscode_copilot,
    has_user_copilot_install,
    has_workspace_copilot_install,
    install_copilot_target,
    install_copilot_workspace_target,
)
from craftsman.staging import EventKind, InstallMode, Tracker, run_reported
from craftsman.uninstall import apply_plan, plan_base


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
    dry_run: bool


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
    claude = config.claude or detected["Claude Code"]
    if claude and not config.claude and _plugin_would_duplicate_hooks(config, home):
        tracker.add(
            EventKind.NOTE,
            "Skipping Claude Code: the craftsman Claude Code plugin is active and already runs "
            "the gate hooks (pass --claude --allow-duplicate-scopes to install both).",
        )
        claude = False
    return replace(
        config,
        auto_detect=False,
        antigravity=config.antigravity or detected["Google Antigravity"],
        claude=claude,
        copilot=config.copilot or detected["GitHub Copilot (VS Code)"] or nothing,
    )


EPHEMERAL_UV_MESSAGE = (
    "Refusing to install lifecycle hooks using an ephemeral uvx interpreter ({exe}): "
    "the cache environment may be deleted by uv, causing hook failures (exit code 127). "
    "Use 'uv tool install craftsman-gate' or 'pipx install craftsman-gate' instead."
)
_UV_CACHE_MARKERS = ("/.cache/uv/", "/library/caches/uv/", "/appdata/local/uv/cache/", "/uv/cache/")


def _normalized(path: str) -> str:
    return path.replace("\\", "/").lower()


def _is_in_uv_cache(path: str, cache_dirs: Sequence[str]) -> bool:
    text = _normalized(path)
    # Persistent uv tools live in e.g. ~/.local/share/uv/tools/ or %LOCALAPPDATA%/uv/tools/.
    if "/uv/tools/" in text:
        return False
    if any(cache in text for cache in cache_dirs):
        return True
    parts = text.split("/")
    return any(marker in text for marker in _UV_CACHE_MARKERS) or (
        "/uv/" in text
        and (
            "archive-" in text
            or "environments-" in text
            or "archive" in parts
            or "environments" in parts
        )
    )


def check_ephemeral_uv_interpreter(python: str | None = None) -> None:
    """Refuse an interpreter in uv's cache, which the hook commands would embed by path.

    uvx runs a cache environment whose `bin/python` is a symlink to the base interpreter, so the
    path is checked as written, which is what the hooks embed, and also once resolved.
    """
    exe = python or sys.executable
    candidates = {os.path.abspath(exe), str(Path(exe).resolve())}
    uv_cache = os.environ.get("UV_CACHE_DIR")
    cache_dirs = (
        [
            _normalized(cache).rstrip("/") + "/"
            for cache in (os.path.abspath(uv_cache), str(Path(uv_cache).resolve()))
        ]
        if uv_cache
        else []
    )
    if any(_is_in_uv_cache(candidate, cache_dirs) for candidate in candidates):
        raise InstallError(EPHEMERAL_UV_MESSAGE.format(exe=exe))


def _embeds_interpreter(config: InstallConfig) -> bool:
    """Whether a selected target writes hook commands that run this interpreter by its path."""
    return (config.claude and config.claude_hooks) or config.copilot


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


def _plugin_would_duplicate_hooks(config: InstallConfig, home: Path) -> bool:
    """Whether installing the Claude Code hooks would run them a second time beside the plugin's."""
    return (
        config.claude_hooks
        and not config.allow_duplicate_scopes
        and is_claude_plugin_active(default_claude_dir(home))
    )


def _check_claude_scopes(config: InstallConfig, home: Path) -> None:
    if config.claude and _plugin_would_duplicate_hooks(config, home):
        raise InstallError(
            "Refusing to install Claude Code user-level hooks while the craftsman Claude Code "
            "plugin is active (hooks would fire twice). Pass --allow-duplicate-scopes to override, "
            "or uninstall the plugin first."
        )


def _remove_legacy_git_hooks(config: InstallConfig, home: Path, tracker: Tracker) -> None:
    """Remove Craftsman 3.x global git hooks recorded in any manifest on upgrade to 4.0."""
    antigravity_base = (home / ".gemini" / "config").resolve()
    for base in legacy_git_hooks_bases(home):
        installing_here = config.antigravity and base.resolve() == antigravity_base
        try:
            plan = plan_base(base, [TargetKind.GIT_HOOKS], home, roots=(base,))
            if plan is None:
                continue
            apply_plan(plan, home, tracker)
        except (InstallError, UnsafePathError) as exc:
            if installing_here:
                raise
            tracker.add(EventKind.WARNING, f"could not clean up legacy git hooks in {base}: {exc}")
            continue
        tracker.add(EventKind.NOTE, "removed the global git hooks of Craftsman 3.x")


def run_install(config: InstallConfig, tracker: Tracker, home: Path | None = None) -> None:
    home = user_home(home)
    repo_dir = config.repo_dir
    if not (repo_dir / "skills").is_dir() or not (repo_dir / "rules").is_dir():
        raise InstallError(f"Repository assets (skills/, rules/) not found under {repo_dir}")
    config = resolve_install_config(config, home, tracker)
    if _embeds_interpreter(config):
        check_ephemeral_uv_interpreter()
    _check_copilot_scopes(config, home)
    _check_claude_scopes(config, home)
    _remove_legacy_git_hooks(config, home, tracker)
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


def _build_install_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craftsman install",
        description="Install Craftsman across Antigravity, Claude Code, GitHub Copilot, and workspaces.",
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
        help="Target all platforms: Antigravity + Claude Code + GitHub Copilot.",
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
        help=(
            "Allow installing Copilot files in both user (~/.copilot) and workspace (.github) "
            "scopes, or Claude Code hooks beside the active craftsman Claude Code plugin."
        ),
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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--copy", action="store_true", help="Copy the Antigravity files (the default)."
    )
    modes.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink the Antigravity files to this checkout.",
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
    mode = (
        InstallMode.SYMLINK if args.symlink else InstallMode.PLUGIN if args.plugin else InstallMode.COPY
    )
    explicit = (
        args.all
        or args.antigravity
        or args.claude
        or args.copilot
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
        dry_run=args.dry_run,
    )


def _install_summary(tracker: Tracker) -> str:
    return (
        f"Summary: {tracker.count(EventKind.INSTALLED)} installed, "
        f"{tracker.count(EventKind.BACKED_UP)} backed up, "
        f"{tracker.count(EventKind.UNCHANGED)} unchanged."
    )


def install_main(argv: Sequence[str] | None = None) -> int:
    try:
        config = parse_install_args(list(sys.argv[1:] if argv is None else argv))
    except (InstallError, ExportError) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    return run_reported(config.dry_run, lambda tracker: run_install(config, tracker), _install_summary)
