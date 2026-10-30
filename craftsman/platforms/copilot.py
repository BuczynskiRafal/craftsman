"""GitHub Copilot (VS Code and Copilot CLI) discovery, exporter, and installer."""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from craftsman.assets import (
    COPY_IGNORED_NAMES,
    AgentTemplate,
    Skill,
    TemplateRole,
    craftsman_entity_names,
    export_agents,
    iter_skills,
    load_agent_templates,
    render_combined_rules,
    single_quoted,
    yaml_list,
)
from craftsman.files import exists, move_aside, write_bytes_atomic, write_text_atomic
from craftsman.generated import (
    CRAFTSMAN_GENERATED_MARKER,
    FileChange,
    is_craftsman_generated_file,
    upsert_managed_markdown,
    write_generated,
    write_marked,
)
from craftsman.hook_command import absolute_hook_prefixes
from craftsman.manifest import TargetKind
from craftsman.paths import (
    default_copilot_home,
    default_copilot_skills_dir,
    default_vscode_prompts_dir,
    default_vscode_user_dir,
    user_home,
    vscode_prompt_dirs,
)
from craftsman.staging import TargetSession, Tracker, upsert_markdown

DUPLICATE_SCOPE_USER_TO_WS_MSG = (
    "Craftsman is already installed for your user in ~/.copilot; a workspace copy would show "
    "every agent and command twice. Uninstall one scope or pass --allow-duplicate-scopes."
)
DUPLICATE_SCOPE_WS_TO_USER_MSG = (
    "Craftsman is already installed for this workspace in .github; a user-level copy in "
    "~/.copilot would show every agent and command twice. Uninstall one scope or pass "
    "--allow-duplicate-scopes."
)

CopilotClient = Literal["vscode", "copilot-cli"]
CopilotEntityKind = Literal["agent", "skill", "prompt"]
CopilotLocationScope = Literal["user", "profile", "workspace", "claude-user", "claude-workspace"]


class DiscoveryRoot(Enum):
    """A folder Copilot reads agents, skills or prompts from: its scope and where it lives."""

    COPILOT_AGENTS = ("user", ".copilot/agents")
    COPILOT_SKILLS = ("user", ".copilot/skills")
    VSCODE_PROMPTS = ("user", "Code/User/prompts")
    VSCODE_PROFILE_PROMPTS = ("profile", "Code/User/profiles/*/prompts")
    WORKSPACE_AGENTS = ("workspace", ".github/agents")
    WORKSPACE_SKILLS = ("workspace", ".github/skills")
    WORKSPACE_PROMPTS = ("workspace", ".github/prompts")
    CLAUDE_AGENTS = ("claude-user", ".claude/agents")
    CLAUDE_SKILLS = ("claude-user", ".claude/skills")
    WORKSPACE_CLAUDE_AGENTS = ("claude-workspace", ".claude/agents")
    WORKSPACE_CLAUDE_SKILLS = ("claude-workspace", ".claude/skills")

    @property
    def scope(self) -> CopilotLocationScope:
        scope: CopilotLocationScope = self.value[0]
        return scope

    @property
    def rel_path(self) -> str:
        rel_path: str = self.value[1]
        return rel_path


class EntityPattern(str, Enum):
    SKILL_DIR = "*/SKILL.md"
    AGENT = "*.agent.md"
    PROMPT = "*.prompt.md"
    MARKDOWN = "*.md"


@dataclass(frozen=True)
class CopilotDiscoveryLocation:
    root: DiscoveryRoot
    pattern: EntityPattern
    canonical: bool

    @property
    def scope(self) -> CopilotLocationScope:
        return self.root.scope

    @property
    def rel_path(self) -> str:
        return self.root.rel_path


def _locations(
    *entries: tuple[DiscoveryRoot, EntityPattern, bool],
) -> tuple[CopilotDiscoveryLocation, ...]:
    return tuple(CopilotDiscoveryLocation(*entry) for entry in entries)


_R, _P = DiscoveryRoot, EntityPattern
# Where VS Code and the Copilot CLI discover each kind of entity; a canonical location is the one
# Craftsman installs into. Documented at code.visualstudio.com/docs/copilot/customization
# (custom-agents, agent-skills, prompt-files) and docs.github.com/en/copilot (copilot-cli).
COPILOT_DISCOVERY: dict[
    tuple[CopilotClient, CopilotEntityKind], tuple[CopilotDiscoveryLocation, ...]
] = {
    ("vscode", "agent"): _locations(
        (_R.COPILOT_AGENTS, _P.AGENT, True),
        (_R.VSCODE_PROMPTS, _P.AGENT, False),
        (_R.VSCODE_PROFILE_PROMPTS, _P.AGENT, False),
        (_R.WORKSPACE_AGENTS, _P.AGENT, True),
        (_R.CLAUDE_AGENTS, _P.MARKDOWN, False),
        (_R.WORKSPACE_CLAUDE_AGENTS, _P.MARKDOWN, False),
    ),
    ("vscode", "skill"): _locations(
        (_R.COPILOT_SKILLS, _P.SKILL_DIR, True),
        (_R.WORKSPACE_SKILLS, _P.SKILL_DIR, True),
        (_R.CLAUDE_SKILLS, _P.SKILL_DIR, False),
        (_R.WORKSPACE_CLAUDE_SKILLS, _P.SKILL_DIR, False),
    ),
    ("vscode", "prompt"): _locations(
        (_R.VSCODE_PROMPTS, _P.PROMPT, False),
        (_R.VSCODE_PROFILE_PROMPTS, _P.PROMPT, False),
        (_R.WORKSPACE_PROMPTS, _P.PROMPT, False),
    ),
    ("copilot-cli", "agent"): _locations(
        (_R.COPILOT_AGENTS, _P.AGENT, True),
        (_R.WORKSPACE_AGENTS, _P.AGENT, True),
    ),
    ("copilot-cli", "skill"): _locations(
        (_R.COPILOT_SKILLS, _P.SKILL_DIR, True),
        (_R.WORKSPACE_SKILLS, _P.SKILL_DIR, True),
    ),
}

COPILOT_AGENT_TOOLS: dict[TemplateRole, tuple[str, ...]] = {
    "orchestrator": ("read", "edit", "search", "execute", "agent", "todo", "web"),
    "reviewer": ("read", "search", "execute"),
    "verifier": ("read", "search", "execute"),
}
COPILOT_TERMINAL_NOTE = (
    "Terminal commands run in the integrated terminal: keep single-line `craftsman` commands on "
    "one line (`'...'` in bash, zsh, and PowerShell, escaping inner `'` as `''` in PowerShell; "
    '`"..."` with `\\"` in `cmd.exe`, where `\'` is not a quote character), while for '
    "`--from-json` batches in PowerShell use "
    "`@' ... '@ | craftsman review <cover|add|verdict> --from-json -` (ASCII notes in Windows "
    "PowerShell 5.1, with `@'` and `'@` on their own lines) or a path from "
    "`craftsman review inbox <cover|add|verdict>` written with "
    "`Set-Content -LiteralPath <path> -Encoding utf8` and passed via `--from-json <path>`."
)
COPILOT_SUBAGENT_NOTE = (
    "Subagents: use the `craftsman-reviewer` and `craftsman-verifier` custom agents (agent tool / "
    "runSubagent)."
)
# Copilot CLI event name -> `craftsman hook` event, with the timeout in seconds.
COPILOT_HOOK_EVENTS: dict[str, tuple[str, int]] = {
    "userPromptSubmitted": ("prompt", 30),
    "agentStop": ("stop", 60),
    "subagentStart": ("subagent-start", 30),
    "subagentStop": ("subagent-stop", 30),
    "preToolUse": ("pre-tool", 30),
}
HOOKS_FILENAME = "craftsman.json"
_COPILOT_HOOK_RE = re.compile(r" hook [a-z-]+ --platform copilot$")


def copilot_note(with_subagents: bool) -> str:
    sentences = (
        (COPILOT_SUBAGENT_NOTE, COPILOT_TERMINAL_NOTE) if with_subagents else (COPILOT_TERMINAL_NOTE,)
    )
    return f"> **GitHub Copilot:** {' '.join(sentences)}\n\n"


def adapt_body_for_copilot(body: str, *, with_subagents: bool = True) -> str:
    """Prefix the generated marker and the Copilot platform note."""
    return f"{CRAFTSMAN_GENERATED_MARKER}\n\n" + copilot_note(with_subagents) + body + "\n"


def render_copilot_skill_file(skill: Skill) -> str:
    adapted = adapt_body_for_copilot(skill.body)
    return f"---\nname: {skill.name}\ndescription: {single_quoted(skill.description)}\n---\n\n{adapted}"


def render_copilot_agent(template: AgentTemplate) -> str:
    lines = [
        "---",
        f"name: {template.name}",
        f"description: {single_quoted(template.description)}",
        f"tools: {yaml_list(COPILOT_AGENT_TOOLS[template.role])}",
    ]
    if template.role == "orchestrator":
        lines.append(f"agents: {yaml_list(template.subagents)}")
    else:
        lines.append("user-invocable: false")
    lines += ["---", ""]
    body = adapt_body_for_copilot(template.body, with_subagents=template.role == "orchestrator")
    return "\n".join(lines) + "\n" + body


def _is_generated_skill_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and is_craftsman_generated_file(path / "SKILL.md")


def export_to_copilot_skills(repo_dir: Path, skills_out_dir: Path) -> list[FileChange]:
    """Native Copilot Agent Skills: `<skills_out_dir>/<skill>/` with every file of the skill."""
    changes: list[FileChange] = []
    for skill in iter_skills(repo_dir):
        dest = skills_out_dir / skill.name
        in_the_way = exists(dest) and not _is_generated_skill_dir(dest)
        backup = move_aside(dest, dry_run=False) if in_the_way else None
        _copy_tree_files(skill.directory, dest)
        write_text_atomic(dest / "SKILL.md", render_copilot_skill_file(skill))
        changes.append(FileChange(dest, changed=True, backup=backup))
    return changes


def _copy_tree_files(source: Path, dest: Path) -> None:
    """Copy every file below `source` but its SKILL.md into `dest`, replacing same-named files."""
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if (
            not path.is_file()
            or relative == Path("SKILL.md")
            or any(part in COPY_IGNORED_NAMES for part in relative.parts)
        ):
            continue
        target = dest / relative
        if target.is_symlink():  # never write through a link into the file it points to
            target.unlink()
        write_bytes_atomic(target, path.read_bytes())


def export_copilot_agents(repo_dir: Path, out_dir: Path) -> list[FileChange]:
    """Every agent as `<name>.agent.md` in `out_dir`."""
    return export_agents(load_agent_templates(repo_dir), out_dir, ".agent.md", render_copilot_agent)


def export_vscode_instructions(repo_dir: Path, prompts_dir: Path) -> list[FileChange]:
    """The global instructions in the VS Code prompts folder and in each profile's."""
    rendered = render_combined_rules(repo_dir / "rules", with_frontmatter=True)
    return [
        write_marked(directory / "craftsman-rules.instructions.md", rendered)
        for directory in vscode_prompt_dirs(prompts_dir)
    ]


def render_copilot_hooks(command_prefix_bash: str, command_prefix_powershell: str) -> dict[str, Any]:
    """Copilot CLI hook file (VS Code reads the same format) calling `craftsman hook`."""
    hooks: dict[str, Any] = {}
    for event, (hook_event, timeout) in COPILOT_HOOK_EVENTS.items():
        suffix = f" hook {hook_event} --platform copilot"
        hooks[event] = [
            {
                "type": "command",
                "bash": command_prefix_bash + suffix,
                "powershell": command_prefix_powershell + suffix,
                "timeoutSec": timeout,
            }
        ]
    return {"version": 1, "hooks": hooks}


def write_copilot_hooks(hooks_dir: Path, bash_prefix: str, powershell_prefix: str) -> FileChange:
    payload = render_copilot_hooks(bash_prefix, powershell_prefix)
    return write_generated(
        hooks_dir / HOOKS_FILENAME, json.dumps(payload, indent=2) + "\n", owned=is_craftsman_hooks_file
    )


def is_craftsman_hooks_file(path: Path) -> bool:
    """Whether `path` is a Copilot hooks file Craftsman wrote: JSON has no room for the marker, so
    it is one whose every command runs `craftsman hook ... --platform copilot`."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        commands = [
            entry[key]
            for group in data["hooks"].values()
            for entry in group
            for key in ("bash", "powershell")
        ]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return bool(commands) and all(_COPILOT_HOOK_RE.search(str(command)) for command in commands)


def resolve_discovery_dirs(
    loc: CopilotDiscoveryLocation,
    *,
    home: Path | None = None,
    workspace: Path | None = None,
    profile: Path | None = None,
    include_all_profiles: bool = False,
) -> list[Path]:
    """The folders on disk a discovery location stands for."""
    root = loc.root
    if root is DiscoveryRoot.VSCODE_PROMPTS:
        return [] if profile is not None else [default_vscode_prompts_dir(home)]
    if root is DiscoveryRoot.VSCODE_PROFILE_PROMPTS:
        if profile is not None:
            return [profile / "prompts"]
        return vscode_prompt_dirs(default_vscode_prompts_dir(home))[1:] if include_all_profiles else []
    if root.scope in ("user", "claude-user"):
        return [user_home(home) / root.rel_path]
    return [workspace / root.rel_path] if workspace is not None else []


def iter_discovered_files(directory: Path, pattern: EntityPattern) -> Iterator[tuple[str, Path]]:
    """`(entity_name, file_path)` for every file matching `pattern` directly inside `directory`."""
    if not directory.is_dir():
        return
    if pattern is EntityPattern.SKILL_DIR:
        for skill_md in sorted(directory.glob(pattern.value)):
            if skill_md.is_file():
                yield skill_md.parent.name, skill_md
        return
    suffix = pattern.value.removeprefix("*")
    for path in sorted(directory.glob(pattern.value)):
        if path.is_file():
            yield path.name.removesuffix(suffix), path


def _has_copilot_install(
    scopes: tuple[CopilotLocationScope, ...],
    repo_dir: Path,
    *,
    home: Path | None = None,
    workspace: Path | None = None,
) -> bool:
    valid_names = craftsman_entity_names(repo_dir)
    for kind in ("agent", "skill", "prompt"):
        for loc in COPILOT_DISCOVERY[("vscode", kind)]:
            if loc.scope not in scopes:
                continue
            for directory in resolve_discovery_dirs(
                loc, home=home, workspace=workspace, include_all_profiles=True
            ):
                for name, file_path in iter_discovered_files(directory, loc.pattern):
                    if name in valid_names and is_craftsman_generated_file(file_path):
                        return True
    return False


def has_user_copilot_install(repo_dir: Path, home: Path | None = None) -> bool:
    """Whether Craftsman-generated user-level Copilot files exist under `home`."""
    return _has_copilot_install(("user", "profile"), repo_dir, home=home)


def has_workspace_copilot_install(workspace_dir: Path, repo_dir: Path) -> bool:
    """Whether Craftsman-generated workspace-level Copilot files exist in `workspace_dir`."""
    return _has_copilot_install(("workspace",), repo_dir, workspace=workspace_dir.resolve())


@dataclass(frozen=True)
class CopilotHomeExport:
    agents: list[FileChange]
    hooks: FileChange


def export_copilot_home(
    repo_dir: Path, copilot_home: Path, prefixes: tuple[str, str]
) -> CopilotHomeExport:
    """User-level Copilot agents and hooks running the (bash, PowerShell) command `prefixes`,
    from `absolute_hook_prefixes`."""
    agents = export_copilot_agents(repo_dir, copilot_home / "agents")
    return CopilotHomeExport(agents, write_copilot_hooks(copilot_home / "hooks", *prefixes))


@dataclass(frozen=True)
class WorkspaceExport:
    skills: list[FileChange]
    agents: list[FileChange]
    hooks: FileChange
    instructions: FileChange


def export_to_workspace(repo_dir: Path, workspace_dir: Path) -> WorkspaceExport:
    github_dir = workspace_dir / ".github"
    rules = render_combined_rules(repo_dir / "rules", with_frontmatter=False)
    return WorkspaceExport(
        skills=export_to_copilot_skills(repo_dir, github_dir / "skills"),
        agents=export_copilot_agents(repo_dir, github_dir / "agents"),
        hooks=write_copilot_hooks(github_dir / "hooks", "craftsman", "craftsman"),
        instructions=upsert_managed_markdown(
            github_dir / "copilot-instructions.md", rules, within=workspace_dir
        ),
    )


def detect_vscode_copilot(home: Path) -> bool:
    return (
        default_copilot_home(home).is_dir()
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or bool(os.environ.get("VSCODE_PID"))
        or bool(os.environ.get("VSCODE_IPC_HOOK_CLI"))
        or default_vscode_user_dir(home).is_dir()
        or shutil.which("code") is not None
    )


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
    upsert_markdown(session, github_dir / "copilot-instructions.md", rules)
    session.finish()
