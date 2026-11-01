"""Claude Code exporter, settings.json hook merger, and installer (`~/.claude`)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from craftsman.assets import (
    AgentTemplate,
    Skill,
    export_agents,
    iter_skills,
    load_agent_templates,
    render_combined_rules,
    single_quoted,
    with_inlined_siblings,
)
from craftsman.files import exists
from craftsman.generated import (
    CRAFTSMAN_GENERATED_MARKER,
    ExportError,
    FileChange,
    read_json_object,
    rewrite_json,
    write_marked,
)
from craftsman.hook_command import absolute_hook_prefixes
from craftsman.manifest import ModifiedKind, TargetKind
from craftsman.paths import default_claude_dir
from craftsman.staging import (
    TargetSession,
    Tracker,
    created_before,
    path_key,
    report_change,
    revert_json_file,
)

CLAUDE_AGENT_TOOLS = "Read, Grep, Glob, Bash"
CLAUDE_SUBAGENT_NOTE = (
    "Subagents: the Agent/Task tool with `subagent_type` `craftsman-reviewer` / `craftsman-verifier`."
)
CLAUDE_HOOK_EVENTS: dict[str, tuple[str, int]] = {
    "UserPromptSubmit": ("prompt", 30),
    "Stop": ("stop", 60),
    "SubagentStart": ("subagent-start", 30),
    "SubagentStop": ("subagent-stop", 30),
    "PreToolUse": ("pre-tool", 30),
}
# Claude Code runs a PreToolUse hook for matched tools; pre-tool enforces both the work-tree
# guard (on Bash) and the repository search scope (on Bash, Read, Grep, Glob, and LS).
CLAUDE_HOOK_MATCHERS: dict[str, str] = {"PreToolUse": "Bash|Read|Grep|Glob|LS"}

_QUOTED = r'"(?:[^"\\]|\\.)*"'
_QUOTED_SHIM = r'"(?:[^"\\]|\\.)*craftsman-cli\.py"'
# Exactly the Claude Code hook commands `merge_claude_hooks` writes: a quoted interpreter running
# `-m craftsman.cli` or the checkout's shim, then `hook <event> --platform claude`.
OUR_HOOK_COMMAND_RE = re.compile(
    rf"^{_QUOTED} (?:-m craftsman\.cli|{_QUOTED_SHIM}) hook "
    rf"(?:{'|'.join(re.escape(event) for event, _ in CLAUDE_HOOK_EVENTS.values())}) "
    r"--platform claude$"
)


def adapt_body_for_claude(body: str) -> str:
    """Prefix the generated marker and the Claude Code platform note."""
    return f"{CRAFTSMAN_GENERATED_MARKER}\n\n> **Claude Code:** {CLAUDE_SUBAGENT_NOTE}\n\n{body}\n"


def render_claude_command_file(skill: Skill) -> str:
    adapted = adapt_body_for_claude(with_inlined_siblings(skill))
    return f"---\ndescription: {single_quoted(skill.description)}\n---\n\n{adapted}"


def claude_agent_templates(repo_dir: Path) -> list[AgentTemplate]:
    """The reviewer and verifier subagents; Claude users run the orchestrator as `/craftsman`."""
    return [t for t in load_agent_templates(repo_dir) if t.role != "orchestrator"]


def render_claude_agent(template: AgentTemplate) -> str:
    lines = ["---", f"name: {template.name}", f"description: {single_quoted(template.description)}"]
    if template.role != "orchestrator":
        lines.append(f"tools: {CLAUDE_AGENT_TOOLS}")
    lines += ["---", "", CRAFTSMAN_GENERATED_MARKER, ""]
    return "\n".join(lines) + "\n" + template.body + "\n"


def export_claude_agents(repo_dir: Path, agents_dir: Path) -> list[FileChange]:
    return export_agents(claude_agent_templates(repo_dir), agents_dir, ".md", render_claude_agent)


def is_craftsman_hook_command(command: Any) -> bool:
    return isinstance(command, str) and OUR_HOOK_COMMAND_RE.match(command) is not None


def merge_claude_hooks(settings_path: Path, bash_prefix: str, *, dry_run: bool = False) -> FileChange:
    """Merge the Craftsman hooks into Claude Code settings.

    Only Craftsman's own commands are replaced; other hooks and settings keep their order. The
    original file is copied to `settings.json.bak` once, before its first change.
    """
    original = read_json_object(settings_path) if exists(settings_path) else {}
    hooks = original.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ExportError(f"refusing to modify {settings_path}: `hooks` is not a JSON object")
    merged_hooks = dict(hooks)
    for event, (hook_event, timeout) in CLAUDE_HOOK_EVENTS.items():
        groups = merged_hooks.get(event, [])
        if not isinstance(groups, list):
            raise ExportError(f"refusing to modify {settings_path}: `hooks.{event}` is not a list")
        entry = {
            "type": "command",
            "command": f"{bash_prefix} hook {hook_event} --platform claude",
            "timeout": timeout,
        }
        ours: dict[str, Any] = {"hooks": [entry]}
        if event in CLAUDE_HOOK_MATCHERS:
            ours = {"matcher": CLAUDE_HOOK_MATCHERS[event], **ours}
        merged_hooks[event] = _replace_our_group(groups, ours)
    updated = {**original, "hooks": merged_hooks}
    return rewrite_json(settings_path, original, updated, backup=exists(settings_path), dry_run=dry_run)


def strip_claude_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """Claude Code settings without the Craftsman hook entries (and events left empty by that)."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    cleaned: dict[str, Any] = {}
    for event, groups in hooks.items():
        kept = list(_without_our_hooks(groups)) if isinstance(groups, list) else groups
        if kept or not groups:
            cleaned[event] = kept
    updated = {key: value for key, value in settings.items() if key != "hooks"}
    if cleaned or not hooks:
        updated["hooks"] = cleaned
    return updated


def _replace_our_group(groups: list[Any], ours: dict[str, Any]) -> list[Any]:
    """`groups` with Craftsman's hooks replaced by `ours`, at the position of the first one."""
    position = next(
        (
            index
            for index, group in enumerate(groups)
            if isinstance(group, dict)
            and isinstance(group.get("hooks"), list)
            and any(
                isinstance(entry, dict) and is_craftsman_hook_command(entry.get("command"))
                for entry in group["hooks"]
            )
        ),
        len(groups),
    )
    before = list(_without_our_hooks(groups[:position]))
    return [*before, ours, *_without_our_hooks(groups[position:])]


def _without_our_hooks(groups: list[Any]) -> Iterator[Any]:
    """Matcher groups without Craftsman hook commands; groups left empty by that are dropped."""
    for group in groups:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            yield group
            continue
        kept = [
            entry
            for entry in entries
            if not (isinstance(entry, dict) and is_craftsman_hook_command(entry.get("command")))
        ]
        if len(kept) == len(entries):
            yield group
        elif kept:
            yield {**group, "hooks": kept}


@dataclass(frozen=True)
class ClaudeExport:
    commands: list[FileChange]
    agents: list[FileChange]
    rules: FileChange


def export_to_claude_user(repo_dir: Path, claude_dir: Path) -> ClaudeExport:
    rules = f"{CRAFTSMAN_GENERATED_MARKER}\n\n" + render_combined_rules(
        repo_dir / "rules", with_frontmatter=False
    )
    return ClaudeExport(
        commands=[
            write_marked(
                claude_dir / "commands" / f"{skill.name}.md", render_claude_command_file(skill)
            )
            for skill in iter_skills(repo_dir)
        ],
        agents=export_claude_agents(repo_dir, claude_dir / "agents"),
        rules=write_marked(claude_dir / "rules" / "craftsman-rules.md", rules),
    )


def detect_claude(home: Path) -> bool:
    return default_claude_dir(home).is_dir() or bool(os.environ.get("CLAUDECODE"))


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
        revert_json_file(settings, session.previous, tracker, strip_claude_hooks)
    session.finish()


def _merge_claude_settings(session: TargetSession, settings: Path, bash_prefix: str) -> None:
    existed = exists(settings)
    change = merge_claude_hooks(settings, bash_prefix, dry_run=session.dry_run)
    session.modified(settings, ModifiedKind.CLAUDE_SETTINGS)
    if not existed or created_before(session, settings):
        session.created(settings)
    else:
        report_change(session.tracker, change)


def is_claude_plugin_active(claude_dir: Path) -> bool:
    """Check whether the Craftsman Claude Code plugin is installed and active."""
    plugin_found = False
    installed_plugins_file = claude_dir / "plugins" / "installed_plugins.json"
    if installed_plugins_file.is_file():
        try:
            data = json.loads(installed_plugins_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                plugin_found = any("craftsman" in str(k).lower() for k in data)
            elif isinstance(data, list):
                plugin_found = any(
                    (isinstance(x, str) and "craftsman" in x.lower())
                    or (isinstance(x, dict) and "craftsman" in str(x.get("name", "")).lower())
                    for x in data
                )
        except (OSError, ValueError):
            pass

    settings_file = claude_dir / "settings.json"
    if settings_file.is_file():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(settings, dict):
                enabled = settings.get("enabledPlugins", {})
                if isinstance(enabled, dict):
                    for k, v in enabled.items():
                        if "craftsman" in str(k).lower():
                            if v is False:
                                return False
                            if v is True:
                                plugin_found = True
        except (OSError, ValueError):
            pass

    return plugin_found


def _render_plugin_manifest() -> str:
    from craftsman import __version__

    manifest = {
        "name": "craftsman",
        "version": __version__,
        "description": "Make AI coding agents prove they reviewed and tested their change.",
        "author": {
            "name": "Rafał Buczyński",
            "email": "buczynski.rafal2@gmail.com",
        },
        "homepage": "https://github.com/BuczynskiRafal/craftsman",
        "repository": "https://github.com/BuczynskiRafal/craftsman",
        "license": "MIT",
        "keywords": [
            "ai-agents",
            "claude-code",
            "code-review",
            "quality-gate",
            "testing",
        ],
        "hooks": "./hooks/hooks.json",
    }
    return json.dumps(manifest, indent=2) + "\n"


def _render_plugin_hooks() -> str:
    hooks_data = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": '"${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" hook prompt --platform claude',
                            "timeout": 30,
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": '"${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" hook stop --platform claude',
                            "timeout": 60,
                        }
                    ]
                }
            ],
            "SubagentStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": '"${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" hook subagent-start --platform claude',
                            "timeout": 30,
                        }
                    ]
                }
            ],
            "SubagentStop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": '"${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" hook subagent-stop --platform claude',
                            "timeout": 30,
                        }
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash|Read|Grep|Glob|LS",
                    "hooks": [
                        {
                            "type": "command",
                            "command": '"${CLAUDE_PLUGIN_ROOT}/bin/launcher.sh" hook pre-tool --platform claude',
                            "timeout": 30,
                        }
                    ],
                }
            ],
        }
    }
    return json.dumps(hooks_data, indent=2) + "\n"


_LAUNCHER_SH = """#!/bin/sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PYTHON_CMD="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_CMD" ]; then
  echo "Craftsman gate inactive: Python 3.10+ not found" >&2
  exit 0
fi

PYTHONPATH="$PLUGIN_ROOT" exec "$PYTHON_CMD" -m craftsman.cli "$@"
"""

_LAUNCHER_CMD = """@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PLUGIN_ROOT=%%~fI"

set "PYTHON_CMD="
for %%C in (py python3 python) do (
  if "%%C"=="py" (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_CMD=py -3"
      goto :found
    )
  ) else (
    where %%C >nul 2>&1
    if not errorlevel 1 (
      %%C -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
      if not errorlevel 1 (
        set "PYTHON_CMD=%%C"
        goto :found
      )
    )
  )
)

:found
if "%PYTHON_CMD%"=="" (
  echo Craftsman gate inactive: Python 3.10+ not found >&2
  exit /b 0
)

set "PYTHONPATH=%PLUGIN_ROOT%"
%PYTHON_CMD% -m craftsman.cli %*
"""

_BIN_CRAFTSMAN_SH = """#!/bin/sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/launcher.sh" "$@"
"""

_BIN_CRAFTSMAN_CMD = """@echo off
"%~dp0launcher.cmd" %*
"""


def _render_marketplace_manifest() -> str:
    from craftsman import __version__

    catalog = {
        "name": "craftsman",
        "description": "Make AI coding agents prove they reviewed and tested their change.",
        "plugins": [
            {
                "name": "craftsman",
                "description": "Make AI coding agents prove they reviewed and tested their change.",
                "version": __version__,
                "source": "./plugins/claude/craftsman",
            }
        ],
    }
    return json.dumps(catalog, indent=2) + "\n"


def build_plugin_files(repo_dir: Path) -> dict[str, str]:
    """Build a mapping of plugin-relative file paths to text content."""
    files: dict[str, str] = {
        ".claude-plugin/plugin.json": _render_plugin_manifest(),
        "hooks/hooks.json": _render_plugin_hooks(),
        "bin/launcher.sh": _LAUNCHER_SH,
        "bin/launcher.cmd": _LAUNCHER_CMD,
        "bin/craftsman": _BIN_CRAFTSMAN_SH,
        "bin/craftsman.cmd": _BIN_CRAFTSMAN_CMD,
    }

    # Skills: copy all files from skills/*/
    skills_root = repo_dir / "skills"
    if skills_root.is_dir():
        for skill_dir in sorted(skills_root.iterdir()):
            if skill_dir.is_dir():
                for p in sorted(skill_dir.rglob("*")):
                    if (
                        p.is_file()
                        and not any(part in ("__pycache__", ".DS_Store", "Thumbs.db") for part in p.parts)
                        and p.suffix != ".pyc"
                    ):
                        rel = p.relative_to(skills_root).as_posix()
                        files[f"skills/{rel}"] = p.read_text(encoding="utf-8")

    # Agents: render all templates
    for template in load_agent_templates(repo_dir):
        files[f"agents/{template.name}.md"] = render_claude_agent(template)

    # Craftsman source package: copy all .py files
    craftsman_root = repo_dir / "craftsman"
    if craftsman_root.is_dir():
        for p in sorted(craftsman_root.rglob("*.py")):
            if p.is_file() and "__pycache__" not in p.parts:
                rel = p.relative_to(craftsman_root).as_posix()
                files[f"craftsman/{rel}"] = p.read_text(encoding="utf-8")

    return files


def export_claude_plugin(
    repo_dir: Path, plugin_dir: Path, *, check: bool = False
) -> list[str]:
    """Export or verify standalone Claude Code plugin tree."""
    files = build_plugin_files(repo_dir)
    marketplace_file = repo_dir / ".claude-plugin" / "marketplace.json"
    marketplace_expected = _render_marketplace_manifest()

    if check:
        diffs: list[str] = []
        for rel_path, expected_text in files.items():
            dest = plugin_dir / rel_path
            if not dest.is_file():
                diffs.append(f"missing: {rel_path}")
            else:
                actual = dest.read_text(encoding="utf-8").replace("\r\n", "\n")
                if actual != expected_text.replace("\r\n", "\n"):
                    diffs.append(f"differs: {rel_path}")

        # Check for unexpected files in plugin_dir
        if plugin_dir.is_dir():
            for p in sorted(plugin_dir.rglob("*")):
                if (
                    p.is_file()
                    and not any(part in ("__pycache__", ".DS_Store", "Thumbs.db") for part in p.parts)
                    and p.suffix != ".pyc"
                ):
                    rel = p.relative_to(plugin_dir).as_posix()
                    if rel not in files:
                        diffs.append(f"extra: {rel}")

        # Check marketplace.json
        if not marketplace_file.is_file():
            diffs.append("missing: .claude-plugin/marketplace.json")
        else:
            actual_market = marketplace_file.read_text(encoding="utf-8").replace("\r\n", "\n")
            if actual_market != marketplace_expected.replace("\r\n", "\n"):
                diffs.append("differs: .claude-plugin/marketplace.json")

        if diffs:
            raise ExportError(
                "Claude Code plugin drift detected (run 'craftsman export --claude-plugin plugins/claude/craftsman'):\n  "
                + "\n  ".join(diffs)
            )
        return sorted(files.keys())

    # Write files
    for rel_path, content in files.items():
        dest = plugin_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        if rel_path in ("bin/launcher.sh", "bin/craftsman") and hasattr(os, "chmod"):
            try:
                dest.chmod(dest.stat().st_mode | 0o755)
            except OSError:
                pass

    marketplace_file.parent.mkdir(parents=True, exist_ok=True)
    marketplace_file.write_text(marketplace_expected, encoding="utf-8")
    return sorted(files.keys())
