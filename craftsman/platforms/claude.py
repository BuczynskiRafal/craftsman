"""Claude Code exporter, settings.json hook merger, and installer (`~/.claude`)."""

from __future__ import annotations

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
    revert_modified_file,
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
        revert_modified_file(settings, ModifiedKind.CLAUDE_SETTINGS, session.previous, tracker)
    session.finish()


def _merge_claude_settings(session: TargetSession, settings: Path, bash_prefix: str) -> None:
    existed = exists(settings)
    change = merge_claude_hooks(settings, bash_prefix, dry_run=session.dry_run)
    session.modified(settings, ModifiedKind.CLAUDE_SETTINGS)
    if not existed or created_before(session, settings):
        session.created(settings)
    else:
        report_change(session.tracker, change)
