#!/usr/bin/env python3
"""Multi-platform exporter for Craftsman skills, rules, agents and lifecycle hooks.

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

Exporting replaces files Craftsman generated before; a user's file in the way is moved into a
`.craftsman-backup` folder next to it, so nothing a user wrote is lost.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

from craftsman.files import (
    copy_once_backup,
    exists,
    move_aside,
    write_bytes_atomic,
    write_text_atomic,
)
from craftsman.lenses import render_reviewer_groups_markdown

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
CRAFTSMAN_BEGIN_MARKER = "<!-- craftsman:begin -->"
CRAFTSMAN_END_MARKER = "<!-- craftsman:end -->"
CRAFTSMAN_GENERATED_MARKER = "<!-- craftsman:generated -->"
INCLUDE_RE = re.compile(r"^\{\{include\s+(\S+)\s*\}\}[ \t]*$", re.MULTILINE)
MALFORMED_INCLUDE_RE = re.compile(r"^[ \t]*\{\{\s*include\b.*$", re.MULTILINE)
SIBLING_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([\w.-]+\.md)\)")
REVIEWER_GROUPS_PLACEHOLDER = "{{reviewer_groups}}"
AGENTS_MD_TEMPLATE = Path("templates") / "AGENTS.md"
COPY_IGNORED_NAMES = frozenset({"__pycache__", ".DS_Store", "Thumbs.db"})

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


class ExportError(RuntimeError):
    """An export target cannot be written without losing or corrupting data."""


# --- Copilot discovery ----------------------------------------------------------------------


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

TemplateRole = Literal["orchestrator", "reviewer", "verifier"]
TEMPLATE_ROLES: tuple[TemplateRole, ...] = get_args(TemplateRole)
COPILOT_AGENT_TOOLS: dict[TemplateRole, tuple[str, ...]] = {
    "orchestrator": ("read", "edit", "search", "execute", "agent", "todo", "web"),
    "reviewer": ("read", "search", "execute"),
    "verifier": ("read", "search", "execute"),
}
CLAUDE_AGENT_TOOLS = "Read, Grep, Glob, Bash"
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
CLAUDE_SUBAGENT_NOTE = (
    "Subagents: the Agent/Task tool with `subagent_type` `craftsman-reviewer` / `craftsman-verifier`."
)
# Copilot CLI event name -> `craftsman hook` event, with the timeout in seconds.
COPILOT_HOOK_EVENTS: dict[str, tuple[str, int]] = {
    "userPromptSubmitted": ("prompt", 30),
    "agentStop": ("stop", 60),
    "subagentStart": ("subagent-start", 30),
    "subagentStop": ("subagent-stop", 30),
    "preToolUse": ("pre-tool", 30),
}
CLAUDE_HOOK_EVENTS: dict[str, tuple[str, int]] = {
    "UserPromptSubmit": ("prompt", 30),
    "Stop": ("stop", 60),
    "SubagentStart": ("subagent-start", 30),
    "SubagentStop": ("subagent-stop", 30),
    "PreToolUse": ("pre-tool", 30),
}
# Claude Code runs a PreToolUse hook for matched tools; pre-tool enforces both the git guard
# (on Bash) and the repository search scope (on Bash, Read, Grep, Glob, and LS).
CLAUDE_HOOK_MATCHERS: dict[str, str] = {"PreToolUse": "Bash|Read|Grep|Glob|LS"}
HOOKS_FILENAME = "craftsman.json"
CLI_SHIM = Path("scripts") / "craftsman-cli.py"
IMPORT_CHECK_TIMEOUT_S = 30

_QUOTED = r'"(?:[^"\\]|\\.)*"'
_QUOTED_SHIM = r'"(?:[^"\\]|\\.)*craftsman-cli\.py"'
# Exactly the Claude Code hook commands `merge_claude_hooks` writes: a quoted interpreter running
# `-m craftsman.cli` or the checkout's shim, then `hook <event> --platform claude`.
OUR_HOOK_COMMAND_RE = re.compile(
    rf"^{_QUOTED} (?:-m craftsman\.cli|{_QUOTED_SHIM}) hook "
    rf"(?:{'|'.join(re.escape(event) for event, _ in CLAUDE_HOOK_EVENTS.values())}) "
    r"--platform claude$"
)


_COPILOT_HOOK_RE = re.compile(r" hook [a-z-]+ --platform copilot$")


# --- locations ------------------------------------------------------------------------------


def user_home(home: Path | None = None) -> Path:
    if home is not None:
        return home
    env_home = os.environ.get("HOME", "").strip() or os.environ.get("USERPROFILE", "").strip()
    if env_home:
        return Path(env_home)
    try:
        return Path.home()
    except RuntimeError as exc:
        raise ExportError(f"cannot determine the home directory: {exc}") from exc


def env_var_for_home(env_key: str, home: Path | None = None) -> str:
    """`env_key` from the environment, or "" when it points outside an explicitly given `home`."""
    val = os.environ.get(env_key, "").strip()
    if not val:
        return ""
    abs_val = Path(os.path.abspath(val))
    abs_home = Path(os.path.abspath(user_home(home)))
    if abs_val == abs_home or abs_home in abs_val.parents:
        return val
    if abs_home == Path(os.path.abspath(user_home(None))):
        return val
    return ""


def _has_assets(directory: Path) -> bool:
    return (directory / "skills").is_dir() and (directory / "agents").is_dir()


def resolve_craftsman_pkg_dir(repo_dir: Path) -> Path:
    """The `craftsman` package that ships with the assets in `repo_dir`."""
    candidate = repo_dir / "craftsman"
    if (candidate / "__init__.py").is_file():
        return candidate
    if repo_dir.name == "_assets" and (repo_dir.parent / "__init__.py").is_file():
        return repo_dir.parent
    raise ExportError(f"no craftsman package next to the assets in {repo_dir}")


def default_repo_dir() -> Path:
    """This checkout, or the assets bundled into the installed package."""
    pkg_dir = Path(__file__).resolve().parent
    for candidate in (pkg_dir.parent, pkg_dir / "_assets"):
        if _has_assets(candidate):
            return candidate
    raise ExportError(f"no Craftsman assets (skills/, agents/) next to {pkg_dir}; pass --repo-dir")


def default_vscode_user_dir(home: Path | None = None) -> Path:
    """VS Code's `Code/User` folder in this operating system's layout."""
    if sys.platform == "win32":
        appdata = env_var_for_home("APPDATA", home)
        if not appdata:
            raise ExportError("APPDATA is not set; cannot locate the VS Code user folder")
        return Path(appdata) / "Code" / "User"
    if sys.platform == "darwin":
        return user_home(home) / "Library" / "Application Support" / "Code" / "User"
    xdg_config = env_var_for_home("XDG_CONFIG_HOME", home)
    return (Path(xdg_config) if xdg_config else user_home(home) / ".config") / "Code" / "User"


def default_vscode_prompts_dir(home: Path | None = None) -> Path:
    return default_vscode_user_dir(home) / "prompts"


def default_copilot_home(home: Path | None = None) -> Path:
    return user_home(home) / ".copilot"


def default_copilot_skills_dir(home: Path | None = None) -> Path:
    return default_copilot_home(home) / "skills"


def default_claude_dir(home: Path | None = None) -> Path:
    return user_home(home) / ".claude"


def vscode_prompt_dirs(prompts_dir: Path) -> list[Path]:
    """The prompts folder plus, for a VS Code `User/prompts` folder, those of its profiles."""
    dirs = [prompts_dir]
    profiles_dir = prompts_dir.parent / "profiles"
    if prompts_dir.name == "prompts" and profiles_dir.is_dir():
        dirs.extend(
            profile / "prompts" for profile in sorted(profiles_dir.iterdir()) if profile.is_dir()
        )
    return dirs


# --- frontmatter, skills and agents ---------------------------------------------------------


def parse_frontmatter(text: str, source: Path) -> tuple[dict[str, str], str]:
    """Split simple YAML frontmatter (scalars, folded `>-` blocks, `[a, b]` lists) from the body."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise ExportError(f"{source}: missing frontmatter block")
    fields: dict[str, str] = {}
    key: str | None = None
    for line in match.group(1).splitlines():
        if key is not None and line[:1] in (" ", "\t"):
            fields[key] = f"{fields[key]} {line.strip()}".strip()
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise ExportError(f"{source}: cannot parse frontmatter line {line!r}")
        key = name.strip()
        value = value.strip()
        fields[key] = "" if value in (">-", ">", "|", "|-") else _unquote(value)
    return fields, match.group(2)


def _unquote(value: str) -> str:
    """A YAML scalar without its enclosing quote pair; quotes inside the text are kept."""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"')
    return value


def _required_fields(fields: dict[str, str], source: Path, *names: str) -> None:
    missing = [name for name in names if not fields.get(name)]
    if missing:
        raise ExportError(f"{source}: frontmatter needs {', '.join(f'`{n}`' for n in missing)}")


@dataclass(frozen=True)
class Skill:
    """A bundled skill: its folder, frontmatter name and description, and the full SKILL.md."""

    name: str
    description: str
    body: str
    text: str
    directory: Path


def read_skill(skill_md: Path) -> Skill:
    text = skill_md.read_text(encoding="utf-8")
    fields, body = parse_frontmatter(text, skill_md)
    _required_fields(fields, skill_md, "name", "description")
    return Skill(fields["name"], fields["description"], body.strip(), text, skill_md.parent)


def iter_skills(repo_dir: Path) -> list[Skill]:
    """Every `skills/*/SKILL.md` of the repository, by folder."""
    return [read_skill(path) for path in sorted((repo_dir / "skills").glob("*/SKILL.md"))]


def _single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def copilot_note(with_subagents: bool) -> str:
    sentences = (
        (COPILOT_SUBAGENT_NOTE, COPILOT_TERMINAL_NOTE) if with_subagents else (COPILOT_TERMINAL_NOTE,)
    )
    return f"> **GitHub Copilot:** {' '.join(sentences)}\n\n"


def adapt_body_for_copilot(body: str, *, with_subagents: bool = True) -> str:
    """Prefix the generated marker and the Copilot platform note."""
    return f"{CRAFTSMAN_GENERATED_MARKER}\n\n" + copilot_note(with_subagents) + body + "\n"


def adapt_body_for_claude(body: str) -> str:
    """Prefix the generated marker and the Claude Code platform note."""
    return f"{CRAFTSMAN_GENERATED_MARKER}\n\n> **Claude Code:** {CLAUDE_SUBAGENT_NOTE}\n\n{body}\n"


def render_copilot_skill_file(skill: Skill) -> str:
    adapted = adapt_body_for_copilot(skill.body)
    return f"---\nname: {skill.name}\ndescription: {_single_quoted(skill.description)}\n---\n\n{adapted}"


def render_claude_command_file(skill: Skill) -> str:
    adapted = adapt_body_for_claude(_with_inlined_siblings(skill))
    return f"---\ndescription: {_single_quoted(skill.description)}\n---\n\n{adapted}"


def _with_inlined_siblings(skill: Skill) -> str:
    """The skill body followed by the sibling Markdown files it links to, for one-file formats."""
    appendices = [
        f"## Appendix: {name}\n\n{_strip_frontmatter((skill.directory / name).read_text(encoding='utf-8'))}"
        for name in dict.fromkeys(SIBLING_LINK_RE.findall(skill.body))
        if name != "SKILL.md" and (skill.directory / name).is_file()
    ]
    if not appendices:
        return skill.body
    return skill.body.rstrip() + "\n\n---\n\n" + "\n\n".join(appendices)


def _strip_frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return (match.group(2) if match else text).strip()


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    description: str
    role: TemplateRole
    subagents: tuple[str, ...]
    body: str  # with includes expanded


def _parse_list(raw: str) -> tuple[str, ...]:
    inner = raw.strip().removeprefix("[").removesuffix("]")
    return tuple(item.strip().strip("\"'") for item in inner.split(",") if item.strip())


def expand_includes(body: str, repo_dir: Path, source: Path) -> str:
    """Replace `{{include <repo-relative path>}}` lines with that file's body."""

    def include(match: re.Match[str]) -> str:
        relative = PurePosixPath(match.group(1))
        # Checked lexically: an installed copy may symlink skills/ to a checkout elsewhere.
        if relative.is_absolute() or ".." in relative.parts:
            raise ExportError(f"{source}: include {match.group(1)} must be a path inside {repo_dir}")
        target = repo_dir / relative
        if not target.is_file():
            raise ExportError(f"{source}: included file not found: {target}")
        return _strip_frontmatter(target.read_text(encoding="utf-8"))

    expanded = INCLUDE_RE.sub(include, body)
    if leftover := MALFORMED_INCLUDE_RE.search(expanded):
        raise ExportError(f"{source}: malformed include directive {leftover.group(0).strip()!r}")
    return expanded


def load_agent_templates(repo_dir: Path) -> list[AgentTemplate]:
    paths = sorted((repo_dir / "agents").glob("*.md"))
    if not paths:
        raise ExportError(f"no agent templates (agents/*.md) found under {repo_dir}")
    return [_load_agent_template(path, repo_dir) for path in paths]


def _load_agent_template(path: Path, repo_dir: Path) -> AgentTemplate:
    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"), path)
    _required_fields(fields, path, "name", "description")
    role = fields.get("role", "")
    if role not in TEMPLATE_ROLES:
        raise ExportError(f"{path}: role must be one of {', '.join(TEMPLATE_ROLES)} (got {role!r})")
    return AgentTemplate(
        name=fields["name"],
        description=fields["description"],
        role=role,
        subagents=_parse_list(fields.get("subagents", "")),
        body=expand_includes(body, repo_dir, path).strip(),
    )


def claude_agent_templates(repo_dir: Path) -> list[AgentTemplate]:
    """The reviewer and verifier subagents; Claude users run the orchestrator as `/craftsman`."""
    return [t for t in load_agent_templates(repo_dir) if t.role != "orchestrator"]


def craftsman_agent_names(repo_dir: Path) -> tuple[str, ...]:
    return tuple(template.name for template in load_agent_templates(repo_dir))


def craftsman_skill_names(repo_dir: Path) -> tuple[str, ...]:
    return tuple(skill.name for skill in iter_skills(repo_dir))


def craftsman_entity_names(repo_dir: Path) -> frozenset[str]:
    return frozenset(
        (*craftsman_agent_names(repo_dir), *craftsman_skill_names(repo_dir), "craftsman-rules")
    )


def _yaml_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(_single_quoted(item) for item in items) + "]"


def render_copilot_agent(template: AgentTemplate) -> str:
    lines = [
        "---",
        f"name: {template.name}",
        f"description: {_single_quoted(template.description)}",
        f"tools: {_yaml_list(COPILOT_AGENT_TOOLS[template.role])}",
    ]
    if template.role == "orchestrator":
        lines.append(f"agents: {_yaml_list(template.subagents)}")
    else:
        lines.append("user-invocable: false")
    lines += ["---", ""]
    body = adapt_body_for_copilot(template.body, with_subagents=template.role == "orchestrator")
    return "\n".join(lines) + "\n" + body


def render_claude_agent(template: AgentTemplate) -> str:
    lines = ["---", f"name: {template.name}", f"description: {_single_quoted(template.description)}"]
    if template.role != "orchestrator":
        lines.append(f"tools: {CLAUDE_AGENT_TOOLS}")
    lines += ["---", "", CRAFTSMAN_GENERATED_MARKER, ""]
    return "\n".join(lines) + "\n" + template.body + "\n"


def render_combined_rules(rules_dir: Path, with_frontmatter: bool) -> str:
    bodies = [
        _strip_frontmatter(rule_file.read_text(encoding="utf-8"))
        for rule_file in sorted(rules_dir.glob("*.md"))
    ]
    combined = "\n\n---\n\n".join(bodies) + "\n"
    if not with_frontmatter:
        return combined
    return (
        "---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering Rules'\n---\n"
        f"{CRAFTSMAN_GENERATED_MARKER}\n\n{combined}"
    )


def render_agents_md(repo_dir: Path) -> str:
    """The universal AGENTS.md block for OpenAI Codex, Cursor, Windsurf and GitHub Copilot."""
    template_path = repo_dir / AGENTS_MD_TEMPLATE
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"cannot read the AGENTS.md template {template_path}: {exc}") from exc
    if REVIEWER_GROUPS_PLACEHOLDER not in template:
        raise ExportError(f"{template_path} has no {REVIEWER_GROUPS_PLACEHOLDER} placeholder")
    groups = "\n".join(f"  {line}" for line in render_reviewer_groups_markdown().splitlines())
    skills = "\n".join(f"- **`/{skill.name}`**: {skill.description}" for skill in iter_skills(repo_dir))
    rules = render_combined_rules(repo_dir / "rules", with_frontmatter=False)
    loop = template.replace(REVIEWER_GROUPS_PLACEHOLDER, groups).strip()
    return f"{loop}\n{skills}\n\n---\n\n{rules}"


# --- generated files ------------------------------------------------------------------------


def is_craftsman_generated_file(path: Path) -> bool:
    """Whether `path` carries the marker Craftsman writes into every file it generates."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return CRAFTSMAN_GENERATED_MARKER in text


@dataclass(frozen=True)
class FileChange:
    """What writing one file did: whether it changed, and where a user's copy was kept."""

    path: Path
    changed: bool
    backup: Path | None = None


def write_generated(path: Path, text: str, *, owned: Callable[[Path], bool]) -> FileChange:
    """Write a generated file; a file in the way that `owned` does not claim is moved aside."""
    backup = None if not exists(path) or owned(path) else move_aside(path, dry_run=False)
    if backup is None and path.is_file() and not path.is_symlink():
        if path.read_bytes() == text.encode("utf-8"):
            return FileChange(path, changed=False)
    if path.is_symlink():  # never write through a link into the file it points to
        path.unlink()
    write_text_atomic(path, text)
    return FileChange(path, changed=True, backup=backup)


def _write_marked(path: Path, text: str) -> FileChange:
    return write_generated(path, text, owned=is_craftsman_generated_file)


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


def export_agents(
    templates: Sequence[AgentTemplate],
    out_dir: Path,
    suffix: str,
    render: Callable[[AgentTemplate], str],
) -> list[FileChange]:
    return [_write_marked(out_dir / f"{t.name}{suffix}", render(t)) for t in templates]


def export_copilot_agents(repo_dir: Path, out_dir: Path) -> list[FileChange]:
    """Every agent as `<name>.agent.md` in `out_dir`."""
    return export_agents(load_agent_templates(repo_dir), out_dir, ".agent.md", render_copilot_agent)


def export_claude_agents(repo_dir: Path, agents_dir: Path) -> list[FileChange]:
    return export_agents(claude_agent_templates(repo_dir), agents_dir, ".md", render_claude_agent)


def export_vscode_instructions(repo_dir: Path, prompts_dir: Path) -> list[FileChange]:
    """The global instructions in the VS Code prompts folder and in each profile's."""
    rendered = render_combined_rules(repo_dir / "rules", with_frontmatter=True)
    return [
        _write_marked(directory / "craftsman-rules.instructions.md", rendered)
        for directory in vscode_prompt_dirs(prompts_dir)
    ]


# --- hooks ----------------------------------------------------------------------------------


def _bash_quote(value: str) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", value) + '"'


def _powershell_quote(value: str) -> str:
    return '"' + re.sub(r'([`"$])', r"`\1", value) + '"'


def cli_shim_for(python: str) -> Path | None:
    """The shim script `python` must run to reach this copy of craftsman, or None for `-m`.

    `-m craftsman.cli` works when `python` imports this very package on its own; a source checkout
    (or a different craftsman installed for that interpreter) needs the checkout's shim script.
    """
    package_dir = Path(__file__).resolve().parent
    if _imports_package(python, package_dir):
        return None
    shim = package_dir.parent / CLI_SHIM
    if not shim.is_file():
        raise ExportError(
            f"{python} cannot import craftsman from {package_dir} and {shim} is missing; "
            "install craftsman for that interpreter (pip install .) and export again"
        )
    return shim


def _imports_package(python: str, package_dir: Path) -> bool:
    """Whether `python`, run outside this checkout, imports the package in `package_dir`."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    try:
        proc = subprocess.run(
            [python, "-c", "import craftsman, sys; sys.stdout.write(craftsman.__file__)"],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=IMPORT_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportError(f"cannot run {python} to check which craftsman it imports: {exc}") from exc
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve().parent == package_dir


def absolute_hook_prefixes(python: str | None = None) -> tuple[str, str]:
    """(bash, PowerShell) command prefixes that run craftsman with an absolute interpreter path."""
    interpreter = python or sys.executable
    if not interpreter:
        raise ExportError("cannot determine the Python interpreter for the hook commands")
    shim = cli_shim_for(interpreter)
    if shim is None:
        return (
            f"{_bash_quote(interpreter)} -m craftsman.cli",
            f"& {_powershell_quote(interpreter)} -m craftsman.cli",
        )
    return (
        f"{_bash_quote(interpreter)} {_bash_quote(str(shim))}",
        f"& {_powershell_quote(interpreter)} {_powershell_quote(str(shim))}",
    )


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


def is_craftsman_hook_command(command: Any) -> bool:
    return isinstance(command, str) and OUR_HOOK_COMMAND_RE.match(command) is not None


def read_json_object(path: Path) -> dict[str, Any]:
    """The JSON object in `path`; anything else is an error, so a user's file is never clobbered."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"refusing to modify {path}: cannot read it as JSON ({exc})") from exc
    if not isinstance(loaded, dict):
        raise ExportError(f"refusing to modify {path}: the top level is not a JSON object")
    return loaded


def write_json_object(path: Path, data: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


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
    return _rewrite_json(settings_path, original, updated, backup=exists(settings_path), dry_run=dry_run)


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


def _rewrite_json(
    path: Path, original: dict[str, Any], updated: dict[str, Any], *, backup: bool, dry_run: bool
) -> FileChange:
    if updated == original and exists(path):
        return FileChange(path, changed=False)
    kept = copy_once_backup(path, dry_run=dry_run) if backup else None
    if not dry_run:
        write_json_object(path, updated)
    return FileChange(path, changed=True, backup=kept)


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


# --- managed Markdown blocks ----------------------------------------------------------------


def find_managed_block(text: str, source: Path) -> tuple[int, int] | None:
    """The span of the Craftsman block in `text`, its trailing newline included, or None when the
    text has no markers. Markers that do not form one block are an error: cutting at them could
    take the user's text with the block."""
    begins, ends = text.count(CRAFTSMAN_BEGIN_MARKER), text.count(CRAFTSMAN_END_MARKER)
    if begins == ends == 0:
        return None
    begin = text.find(CRAFTSMAN_BEGIN_MARKER)
    end = text.find(CRAFTSMAN_END_MARKER)
    if begins != 1 or ends != 1 or end < begin:
        raise ExportError(
            f"refusing to modify {source}: its {CRAFTSMAN_BEGIN_MARKER} and {CRAFTSMAN_END_MARKER} "
            "markers do not enclose one block; fix them by hand"
        )
    end += len(CRAFTSMAN_END_MARKER)
    return begin, end + 1 if text[end : end + 1] == "\n" else end


def read_markdown(target_file: Path) -> str:
    try:
        return target_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"refusing to modify {target_file}: cannot read it as UTF-8 ({exc})") from exc


def require_resolves_within(path: Path, root: Path) -> None:
    """A symlinked workspace file is updated where it lives, which must be inside `root`."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExportError(f"refusing to modify {path}: it resolves outside {root}")


def upsert_managed_markdown(
    target_file: Path, inner_content: str, *, within: Path, dry_run: bool = False
) -> FileChange:
    """Insert or update the Craftsman block, keeping everything else in the file.

    A file without the block gets it appended, after a one-time `.bak` copy of the original.
    """
    require_resolves_within(target_file, within)
    block = f"{CRAFTSMAN_BEGIN_MARKER}\n{inner_content.strip()}\n{CRAFTSMAN_END_MARKER}\n"
    existing = read_markdown(target_file) if exists(target_file) else None
    span = find_managed_block(existing, target_file) if existing is not None else None
    if existing is None:
        updated = block
    elif span is not None:
        updated = existing[: span[0]] + block + existing[span[1] :]
    else:
        updated = (f"{existing.rstrip()}\n\n" if existing.strip() else "") + block
    if updated == existing:
        return FileChange(target_file, changed=False)
    kept = copy_once_backup(target_file, dry_run=dry_run) if existing and span is None else None
    if not dry_run:
        write_text_atomic(target_file, updated)
    return FileChange(target_file, changed=True, backup=kept)


def strip_managed_block(text: str, source: Path) -> str:
    """`text` without the Craftsman block, joining what surrounded it."""
    span = find_managed_block(text, source)
    if span is None:
        return text
    before, after = text[: span[0]].rstrip(), text[span[1] :].strip()
    joined = "\n\n".join(part for part in (before, after) if part)
    return f"{joined}\n" if joined else ""


# --- discovery ------------------------------------------------------------------------------


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


# --- targets --------------------------------------------------------------------------------


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
            _write_marked(
                claude_dir / "commands" / f"{skill.name}.md", render_claude_command_file(skill)
            )
            for skill in iter_skills(repo_dir)
        ],
        agents=export_claude_agents(repo_dir, claude_dir / "agents"),
        rules=_write_marked(claude_dir / "rules" / "craftsman-rules.md", rules),
    )


CURSOR_RULE_HEADER = (
    "---\n"
    "description: Craftsman Agentic Engineering Rules\n"
    'globs: "*"\n'
    "alwaysApply: true\n"
    "---\n\n"
    f"{CRAFTSMAN_GENERATED_MARKER}\n\n"
)


@dataclass(frozen=True)
class AgentsWorkspaceExport:
    agents_md: FileChange
    cursor_rule: FileChange


def export_to_agents_workspace(repo_dir: Path, workspace_dir: Path) -> AgentsWorkspaceExport:
    agents_content = render_agents_md(repo_dir)
    return AgentsWorkspaceExport(
        agents_md=upsert_managed_markdown(
            workspace_dir / "AGENTS.md", agents_content, within=workspace_dir
        ),
        cursor_rule=_write_marked(
            workspace_dir / ".cursor" / "rules" / "craftsman.mdc", CURSOR_RULE_HEADER + agents_content
        ),
    )


# --- command line ---------------------------------------------------------------------------


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
