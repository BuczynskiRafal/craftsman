#!/usr/bin/env python3
"""Multi-platform exporter for Craftsman skills, rules, agents and lifecycle hooks.

Targets:
1. GitHub Copilot, user level (`--user-dir`, `--copilot-skills-dir`, `--copilot-home`): prompt files
   and instructions in the VS Code prompts folder (and its profiles), Agent Skills in
   `~/.copilot/skills/<skill>/`, custom agents in `~/.copilot/agents/` and the prompts folder, and
   hooks in `~/.copilot/hooks/craftsman.json` that run this interpreter by absolute path.
2. GitHub Copilot, workspace (`--workspace`): `.github/prompts`, `.github/skills`, `.github/agents`,
   `.github/hooks/craftsman.json` (runs `craftsman` from PATH) and `.github/copilot-instructions.md`.
3. Claude Code (`--claude-dir`): slash commands, rules, the reviewer/verifier subagents and the
   Craftsman hooks merged into `settings.json` (skip them with `--no-claude-hooks`).
4. OpenAI Codex / Cursor (`--agents-workspace`): `AGENTS.md` and `.cursor/rules/craftsman.mdc`.

Exporting overwrites files Craftsman generated before and never deletes anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, get_args

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
CRAFTSMAN_BEGIN_MARKER = "<!-- craftsman:begin -->"
CRAFTSMAN_END_MARKER = "<!-- craftsman:end -->"
INCLUDE_RE = re.compile(r"^\{\{include\s+(\S+)\s*\}\}[ \t]*$", re.MULTILINE)
MALFORMED_INCLUDE_RE = re.compile(r"^[ \t]*\{\{\s*include\b.*$", re.MULTILINE)
SIBLING_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([\w.-]+\.md)\)")
OUR_HOOK_COMMAND_RE = re.compile(r"\bcraftsman(?:\.cli|-cli\.py|\.exe)?[\"']?\s+hook\b")
COPY_IGNORED_NAMES = frozenset({"__pycache__", ".DS_Store", "Thumbs.db"})

AgentRole = Literal["orchestrator", "reviewer", "verifier"]
AGENT_ROLES: tuple[AgentRole, ...] = get_args(AgentRole)
COPILOT_AGENT_TOOLS: dict[AgentRole, tuple[str, ...]] = {
    "orchestrator": ("read", "edit", "search", "execute", "agent", "todo", "web"),
    "reviewer": ("read", "search", "execute"),
    "verifier": ("read", "search", "execute"),
}
CLAUDE_AGENT_TOOLS = "Read, Grep, Glob, Bash"
ORCHESTRATOR_SKILL = "craftsman"
COPILOT_TERMINAL_NOTE = (
    "Terminal commands run in the integrated terminal; every `craftsman` command works in bash, "
    "zsh and PowerShell."
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


class ExportError(RuntimeError):
    """An export target cannot be written without losing or corrupting data."""


def user_home(home: Path | None = None) -> Path:
    if home is not None:
        return home
    env_home = os.environ.get("HOME", "").strip()
    if env_home:
        return Path(env_home)
    try:
        return Path.home()
    except RuntimeError:
        return Path(".")


def env_var_for_home(env_key: str, home: Path | None = None) -> str:
    val = os.environ.get(env_key, "").strip()
    if not val:
        return ""
    if home is None:
        return val
    abs_home = Path(os.path.abspath(str(home)))
    if abs_home == Path(os.path.abspath(str(user_home(None)))):
        return val
    if env_key == "APPDATA" and os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", val):
        return val
    abs_val = Path(os.path.abspath(val))
    if abs_val == abs_home or abs_home in abs_val.parents:
        return val
    return ""


def _has_assets(directory: Path) -> bool:
    return (directory / "skills").is_dir() and (directory / "agents").is_dir()


def resolve_craftsman_pkg_dir(repo_dir: Path) -> Path:
    candidate = repo_dir / "craftsman"
    if candidate.is_dir() and (candidate / "__init__.py").is_file():
        return candidate
    if repo_dir.name == "_assets" and (repo_dir.parent / "__init__.py").is_file():
        return repo_dir.parent
    return Path(__file__).resolve().parent


def default_repo_dir() -> Path:
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parent
    if _has_assets(repo_root):
        return repo_root
    bundled = pkg_dir / "_assets"
    if _has_assets(bundled):
        return bundled
    cfg_dir = user_home() / ".gemini" / "config"
    if _has_assets(cfg_dir):
        return cfg_dir
    return repo_root


def _resolve_windows_appdata(home: Path | None = None) -> Path | None:
    resolved_home = user_home(home)
    appdata = env_var_for_home("APPDATA", home)
    win_roaming = resolved_home / "AppData" / "Roaming"
    if appdata:
        if os.name != "nt":
            drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", appdata)
            if drive_match:
                drive = drive_match.group(1).lower()
                rest = drive_match.group(2).replace("\\", "/")
                for candidate in (
                    win_roaming,
                    Path(f"/{drive}/{rest}"),
                    Path(f"/mnt/{drive}/{rest}"),
                ):
                    if candidate.is_dir():
                        return candidate
                return win_roaming
        return Path(appdata)
    if sys.platform == "win32" or os.name == "nt" or win_roaming.is_dir():
        return win_roaming
    return None


def default_vscode_prompts_dir(home: Path | None = None) -> Path:
    resolved_home = user_home(home)
    win_base = _resolve_windows_appdata(home)
    if win_base is not None:
        return win_base / "Code" / "User" / "prompts"
    mac_dir = resolved_home / "Library" / "Application Support" / "Code" / "User" / "prompts"
    if sys.platform == "darwin" or mac_dir.parent.is_dir():
        return mac_dir
    xdg_cfg = env_var_for_home("XDG_CONFIG_HOME", home)
    cfg_root = Path(xdg_cfg) if xdg_cfg else (resolved_home / ".config")
    return cfg_root / "Code" / "User" / "prompts"


def default_copilot_skills_dir(home: Path | None = None) -> Path:
    return user_home(home) / ".copilot" / "skills"


def default_claude_dir(home: Path | None = None) -> Path:
    return user_home(home) / ".claude"


def default_copilot_home(home: Path | None = None) -> Path:
    return user_home(home) / ".copilot"


def extract_skill_name(skill_md: str, fallback_name: str) -> str:
    match = FRONTMATTER_RE.match(skill_md)
    if not match:
        return fallback_name
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            val = _unquote(line.split(":", 1)[1].strip())
            if val:
                return val
    return fallback_name


def extract_description_and_body(skill_md: str, fallback_name: str) -> tuple[str, str]:
    match = FRONTMATTER_RE.match(skill_md)
    if not match:
        return f"Craftsman skill /{fallback_name}", skill_md.strip()

    fm_raw, body = match.group(1), match.group(2)
    desc_lines: list[str] = []
    in_desc = False
    for line in fm_raw.splitlines():
        if line.startswith("description:"):
            val = line.split(":", 1)[1].strip()
            if val not in (">-", "|-", "|", ">", ""):
                desc_lines.append(_unquote(val))
            in_desc = True
            continue
        if in_desc:
            if line.startswith("  ") or line.startswith("\t"):
                desc_lines.append(line.strip())
            else:
                break

    description = " ".join(desc_lines).strip() or f"Craftsman skill /{fallback_name}"
    return description, body.strip()


GATE_SCRIPT_INVOCATION_RE = re.compile(
    r"(?:python3?\s+)?~/.gemini/config/scripts/craftsman-stop-gate\.py"
)


def _rewrite_antigravity_paths(body: str) -> str:
    """Rewrite Antigravity-specific script and artifact paths to portable per-worktree CLI equivalents."""
    state_expr = (
        "$(git rev-parse --git-dir 2>/dev/null || echo .craftsman)/craftsman/craftsman_state.json"
    )
    dir_expr = "$(git rev-parse --git-dir 2>/dev/null || echo .craftsman)/craftsman"
    rewritten = GATE_SCRIPT_INVOCATION_RE.sub("craftsman gate", body)
    rewritten = rewritten.replace(
        '"<artifactDirectoryPath>/craftsman_state.json"',
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace(
        "<artifactDirectoryPath>/craftsman_state.json",
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace(
        '"<appDataDir>/brain/<conversation-id>/craftsman_state.json"',
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace(
        "<appDataDir>/brain/<conversation-id>/craftsman_state.json",
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace("<artifactDirectoryPath>", dir_expr)
    rewritten = rewritten.replace("<appDataDir>/brain/<conversation-id>", dir_expr)
    return rewritten


def copilot_note(with_subagents: bool) -> str:
    sentences = (
        (COPILOT_SUBAGENT_NOTE, COPILOT_TERMINAL_NOTE) if with_subagents else (COPILOT_TERMINAL_NOTE,)
    )
    return f"> **GitHub Copilot:** {' '.join(sentences)}\n\n"


def adapt_body_for_copilot(body: str, *, with_subagents: bool = True) -> str:
    """Prefix the Copilot platform note and normalize legacy paths."""
    return copilot_note(with_subagents) + _rewrite_antigravity_paths(body) + "\n"


def adapt_body_for_claude(body: str) -> str:
    """Prefix the Claude Code platform note and normalize legacy paths."""
    return f"> **Claude Code:** {CLAUDE_SUBAGENT_NOTE}\n\n" + _rewrite_antigravity_paths(body) + "\n"


def _single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def render_copilot_prompt_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    agent = ORCHESTRATOR_SKILL if skill_name == ORCHESTRATOR_SKILL else "agent"
    adapted = adapt_body_for_copilot(body)
    return f"---\nagent: '{agent}'\ndescription: {_single_quoted(description)}\n---\n\n{adapted}"


def render_copilot_skill_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    adapted = adapt_body_for_copilot(body)
    return f"---\nname: {skill_name}\ndescription: {_single_quoted(description)}\n---\n\n{adapted}"


def render_claude_command_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    adapted = adapt_body_for_claude(body)
    return f"---\ndescription: {_single_quoted(description)}\n---\n\n{adapted}"


def inline_sibling_links(skill_md: str, skill_dir: Path) -> str:
    """Append sibling Markdown files that `skill_md` links to, for single-file formats."""
    appendices: list[str] = []
    for name in dict.fromkeys(SIBLING_LINK_RE.findall(skill_md)):
        sibling = skill_dir / name
        if name == "SKILL.md" or not sibling.is_file():
            continue
        appendices.append(
            f"## Appendix: {name}\n\n{_strip_frontmatter(sibling.read_text(encoding='utf-8'))}"
        )
    if not appendices:
        return skill_md
    return skill_md.rstrip() + "\n\n---\n\n" + "\n\n".join(appendices) + "\n"


def _strip_frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return (match.group(2) if match else text).strip()


# --- agents ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    description: str
    role: AgentRole
    subagents: tuple[str, ...]
    body: str  # with includes expanded


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
    templates: list[AgentTemplate] = []
    for path in paths:
        fields, body = parse_frontmatter(path.read_text(encoding="utf-8"), path)
        role = fields.get("role", "")
        name = fields.get("name", "")
        if not name or not fields.get("description"):
            raise ExportError(f"{path}: frontmatter needs `name` and `description`")
        if role not in AGENT_ROLES:
            raise ExportError(f"{path}: role must be one of {', '.join(AGENT_ROLES)} (got {role!r})")
        templates.append(
            AgentTemplate(
                name=name,
                description=fields["description"],
                role=role,
                subagents=_parse_list(fields.get("subagents", "")),
                body=expand_includes(body, repo_dir, path).strip(),
            )
        )
    return templates


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
    lines += ["---", ""]
    return "\n".join(lines) + "\n" + _rewrite_antigravity_paths(template.body) + "\n"


def _write_agents(
    templates: list[AgentTemplate], out_dir: Path, suffix: str, render: Callable[[AgentTemplate], str]
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for template in templates:
        target = out_dir / f"{template.name}{suffix}"
        write_text_atomic(target, render(template))
        written.append(target)
    return written


def export_copilot_agents(repo_dir: Path, out_dir: Path) -> list[Path]:
    """Write every agent as `<name>.agent.md` into `out_dir`."""
    return _write_agents(load_agent_templates(repo_dir), out_dir, ".agent.md", render_copilot_agent)


def export_claude_agents(repo_dir: Path, agents_dir: Path) -> list[Path]:
    """Write the reviewer and verifier subagents; Claude users run the orchestrator as `/craftsman`."""
    templates = [t for t in load_agent_templates(repo_dir) if t.role != "orchestrator"]
    return _write_agents(templates, agents_dir, ".md", render_claude_agent)


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
    except (OSError, subprocess.SubprocessError):
        return False
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


def write_copilot_hooks(hooks_dir: Path, bash_prefix: str, powershell_prefix: str) -> Path:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / HOOKS_FILENAME
    payload = render_copilot_hooks(bash_prefix, powershell_prefix)
    write_text_atomic(target, json.dumps(payload, indent=2) + "\n")
    return target


def is_craftsman_hook_command(command: Any) -> bool:
    return isinstance(command, str) and OUR_HOOK_COMMAND_RE.search(command) is not None


def merge_claude_hooks(settings_path: Path, bash_prefix: str) -> bool:
    """Merge the Craftsman hooks into Claude Code settings; return whether the file changed.

    Only commands that run `craftsman hook` are replaced; other hooks and settings keep their
    order. The original file is copied to `settings.json.bak` once, before its first change.
    """
    original: dict[str, Any] = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportError(
                f"refusing to modify {settings_path}: cannot read it as JSON ({exc})"
            ) from exc
        if not isinstance(loaded, dict):
            raise ExportError(f"refusing to modify {settings_path}: the top level is not a JSON object")
        original = loaded
    hooks = original.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ExportError(f"refusing to modify {settings_path}: `hooks` is not a JSON object")
    merged_hooks = dict(hooks)
    for event, (hook_event, timeout) in CLAUDE_HOOK_EVENTS.items():
        groups = merged_hooks.get(event, [])
        if not isinstance(groups, list):
            raise ExportError(f"refusing to modify {settings_path}: `hooks.{event}` is not a list")
        ours: dict[str, Any] = {
            "hooks": [
                {
                    "type": "command",
                    "command": f"{bash_prefix} hook {hook_event} --platform claude",
                    "timeout": timeout,
                }
            ]
        }
        if event in CLAUDE_HOOK_MATCHERS:
            ours = {"matcher": CLAUDE_HOOK_MATCHERS[event], **ours}
        merged_hooks[event] = _replace_our_group(groups, ours)
    updated = {**original, "hooks": merged_hooks}
    if updated == original:
        return False
    backup = settings_path.with_name(settings_path.name + ".bak")
    if settings_path.exists() and not backup.exists():
        shutil.copy2(settings_path, backup)
    write_text_atomic(settings_path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    return True


def remove_claude_hooks(settings_path: Path, *, create_backup: bool = True) -> bool:
    """Remove Craftsman hook entries from Claude Code settings; return whether the file changed."""
    if not settings_path.exists():
        return False
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(
            f"refusing to modify {settings_path}: cannot read it as JSON ({exc})"
        ) from exc
    if not isinstance(loaded, dict):
        raise ExportError(f"refusing to modify {settings_path}: the top level is not a JSON object")
    hooks = loaded.get("hooks")
    if not isinstance(hooks, dict):
        return False
    cleaned_hooks: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            cleaned_hooks[event] = groups
            continue
        kept = list(_without_our_hooks(groups))
        if kept or not groups:
            cleaned_hooks[event] = kept
    updated = dict(loaded)
    if cleaned_hooks:
        updated["hooks"] = cleaned_hooks
    elif hooks:
        updated.pop("hooks", None)
    if updated == loaded:
        return False
    backup = settings_path.with_name(settings_path.name + ".bak")
    if create_backup and not backup.exists():
        shutil.copy2(settings_path, backup)
    write_text_atomic(settings_path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    return True


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


def write_text_atomic(path: Path, text: str) -> None:
    """Replace `path` atomically; a symlinked file (dotfiles) is updated where it really lives."""
    path = path.resolve() if path.is_symlink() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render_combined_rules(rules_dir: Path, with_frontmatter: bool) -> str:
    parts: list[str] = []
    if with_frontmatter:
        parts.append(
            "---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering & Git Rules'\n---\n"
        )

    for rule_file in sorted(rules_dir.glob("*.md")):
        raw_rule = rule_file.read_text(encoding="utf-8").strip()
        fm_match = FRONTMATTER_RE.match(raw_rule + "\n")
        body = fm_match.group(2).strip() if fm_match else raw_rule
        parts.append(body)
        parts.append("\n\n---\n")

    return "\n".join(parts).strip() + "\n"


def render_agents_md(repo_dir: Path) -> str:
    """Render a universal AGENTS.md block for OpenAI Codex, Cursor, Windsurf, and GitHub Copilot."""
    rules_dir = repo_dir / "rules"
    skills_dir = repo_dir / "skills"

    lines: list[str] = [
        "# AGENTS.md — Craftsman Adaptive Closed-Loop Engineering Standard",
        "",
        "This repository uses **Craftsman**: every change is reviewed and verified against an evidence "
        "ledger before it counts as done.",
        "",
        "## The Craftsman loop",
        "- **Gears:** `quick` (≤ 40 changed lines in ≤ 2 files; lenses applied inline), `standard` "
        "(default; every always-on lens in fresh-context reviewers, a final sweep and a verifier), "
        "`deep` (`/craftsman --deep` or `ultracraft`; every lens plus an end-to-end run).",
        "- **Start (`/craftsman` only):** `craftsman gate --init --tier <auto|quick|standard|deep> --task \"<request>\"` records "
        "the base commit; the review always covers the whole diff since then (standalone `/code-review` without `--fix` does not start a gate session).",
        "- **Review:** `craftsman review prepare` writes a packet of hunks and lenses; record what each "
        "lens examined with `craftsman review cover` (or `--from-json - <<'EOF'`) and every defect with `craftsman review add` (or `--from-json - <<'EOF'`).",
        "- **Verify findings:** group unjudged same-root-cause duplicates first with `craftsman review merge <target> <dup>` "
        "(only when one fix removes every consequence), then record verdicts with `craftsman review verdict` (or `--from-json - <<'EOF'`), "
        'fix and `craftsman review resolve <id> --as fixed --note "..."`; prepare a new '
        "round for the changed hunks.",
        "- **Run checks through the ledger:** `craftsman run --kind test -- <command>` (and `--kind e2e` "
        "on deep); only runs at the final code count.",
        "- **Done means** `craftsman review check` passes; then `craftsman gate --complete` and report with "
        "`craftsman review report` (in standalone review-only `/code-review` without `--fix`, finish with "
        "`craftsman review check --review-only` and `craftsman review report --review-only`).",
        "- **Skills & lenses:** invoke `/name` as a skill if your platform offers it; otherwise run "
        "`craftsman skill show <name>` (and `craftsman skill show code-review --file lenses.md` for "
        "the review lens playbooks). Never search the file system for skill or lens files.",
        "",
        "## Search scope",
        "- Work only inside this git work tree (`git rev-parse --show-toplevel`): prefer `git ls-files`, "
        "`git grep`, and editor search scoped to this folder.",
        "- Outside the work tree you may read only an exact file cited by a stack trace/error, a temp "
        "directory you created, CI logs for this branch/PR, or Craftsman's own data via `craftsman …` CLI commands.",
        "- Never run recursive searches of `~`, `/`, or parent directories, and never open other "
        "repositories, other projects' packets/ledgers/reports, or assistant session stores.",
        "",
        "## Available Skills",
    ]
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        raw_md = skill_path.read_text(encoding="utf-8")
        skill_name = extract_skill_name(raw_md, skill_path.parent.name)
        desc, _ = extract_description_and_body(raw_md, skill_name)
        lines.append(f"- **`/{skill_name}`**: {desc}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        _rewrite_antigravity_paths(render_combined_rules(rules_dir, with_frontmatter=False).strip())
    )
    lines.append("")
    return "\n".join(lines)


def upsert_managed_markdown(target_file: Path, inner_content: str) -> bool:
    """Insert or update a `<!-- craftsman:begin -->...<!-- craftsman:end -->` block without destroying user content."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    managed_block = f"{CRAFTSMAN_BEGIN_MARKER}\n{inner_content.strip()}\n{CRAFTSMAN_END_MARKER}\n"

    if not target_file.is_file():
        write_text_atomic(target_file, managed_block)
        return True

    try:
        existing = target_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        bak = target_file.with_name(target_file.name + ".bak")
        if not bak.exists():
            shutil.copy2(target_file, bak)
        raise ExportError(
            f"refusing to modify {target_file}: cannot read it as UTF-8 ({exc}); backed up to {bak}"
        ) from exc
    begin_idx = existing.find(CRAFTSMAN_BEGIN_MARKER)
    end_idx = existing.find(CRAFTSMAN_END_MARKER)
    if begin_idx != -1 and end_idx != -1 and begin_idx < end_idx:
        end_pos = end_idx + len(CRAFTSMAN_END_MARKER)
        if end_pos < len(existing) and existing[end_pos : end_pos + 1] == "\n":
            end_pos += 1
        updated = existing[:begin_idx] + managed_block + existing[end_pos:]
    elif existing.lstrip().startswith(
        ("# AGENTS.md — Craftsman", "# Craftsman Global Engineering")
    ):
        updated = managed_block
    else:
        # Preserve existing custom project instructions and append the Craftsman block
        base = existing.rstrip()
        prefix = f"{base}\n\n" if base else ""
        updated = prefix + managed_block

    if updated != existing:
        write_text_atomic(target_file, updated)
        return True
    return False


def remove_managed_markdown(target_file: Path) -> bool:
    """Remove the `<!-- craftsman:begin -->...<!-- craftsman:end -->` block from `target_file`."""
    if not target_file.is_file():
        return False
    try:
        existing = target_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    begin_idx = existing.find(CRAFTSMAN_BEGIN_MARKER)
    end_idx = existing.find(CRAFTSMAN_END_MARKER)
    if begin_idx == -1 or end_idx == -1 or begin_idx >= end_idx:
        return False
    end_pos = end_idx + len(CRAFTSMAN_END_MARKER)
    if end_pos < len(existing) and existing[end_pos : end_pos + 1] == "\n":
        end_pos += 1
    before = existing[:begin_idx].rstrip()
    after = existing[end_pos:].lstrip()
    if before and after:
        updated = f"{before}\n\n{after}\n"
    elif before:
        updated = f"{before}\n"
    elif after:
        updated = f"{after}\n"
    else:
        updated = ""
    write_text_atomic(target_file, updated)
    return True


def _render_skill_items(
    skills_dir: Path,
    renderer: Callable[[str, str], str],
    *,
    inline_siblings: bool,
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        raw_md = skill_path.read_text(encoding="utf-8")
        skill_name = extract_skill_name(raw_md, skill_path.parent.name)
        if inline_siblings:
            raw_md = inline_sibling_links(raw_md, skill_path.parent)
        items.append((skill_name, renderer(skill_name, raw_md)))
    return items


def _export_skill_files(
    skills_dir: Path,
    out_dir: Path,
    suffix: str,
    renderer: Callable[[str, str], str],
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered_items = _render_skill_items(skills_dir, renderer, inline_siblings=True)
    for skill_name, rendered in rendered_items:
        write_text_atomic(out_dir / f"{skill_name}{suffix}", rendered)
    return len(rendered_items)


def export_to_copilot_skills(repo_dir: Path, skills_out_dir: Path) -> int:
    """Export native Copilot Agent Skills: `<skills_out_dir>/<skill>/` with every file of the skill."""
    skills_out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill_path in sorted((repo_dir / "skills").glob("*/SKILL.md")):
        raw_md = skill_path.read_text(encoding="utf-8")
        skill_name = extract_skill_name(raw_md, skill_path.parent.name)
        dest = skills_out_dir / skill_name
        _copy_tree_files(skill_path.parent, dest)
        write_text_atomic(dest / "SKILL.md", render_copilot_skill_file(skill_name, raw_md))
        count += 1
    return count


def _copy_tree_files(source: Path, dest: Path) -> None:
    """Copy every file below `source` into `dest`, overwriting same-named files only."""
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or any(part in COPY_IGNORED_NAMES for part in relative.parts):
            continue
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():  # never write through a link into the file it points to
            target.unlink()
        shutil.copyfile(path, target)


def _prompt_dirs(target_dir: Path) -> list[Path]:
    """The prompts folder plus, for a VS Code `User/prompts` folder, those of its profiles."""
    dirs = [target_dir]
    profiles_dir = target_dir.parent / "profiles"
    if target_dir.name == "prompts" and profiles_dir.is_dir():
        dirs.extend(
            profile / "prompts" for profile in sorted(profiles_dir.iterdir()) if profile.is_dir()
        )
    return dirs


vscode_prompt_dirs = _prompt_dirs


def export_to_vscode_user(repo_dir: Path, target_dir: Path) -> tuple[int, Path]:
    rendered_prompts = _render_skill_items(
        repo_dir / "skills", render_copilot_prompt_file, inline_siblings=True
    )
    rendered_rules = _rewrite_antigravity_paths(
        render_combined_rules(repo_dir / "rules", with_frontmatter=True)
    )
    templates = load_agent_templates(repo_dir)
    for prompts_dir in _prompt_dirs(target_dir):
        prompts_dir.mkdir(parents=True, exist_ok=True)
        for skill_name, rendered in rendered_prompts:
            write_text_atomic(prompts_dir / f"{skill_name}.prompt.md", rendered)
        write_text_atomic(prompts_dir / "craftsman-rules.instructions.md", rendered_rules)
        _write_agents(templates, prompts_dir, ".agent.md", render_copilot_agent)
    return len(rendered_prompts), target_dir / "craftsman-rules.instructions.md"


def export_copilot_home(
    repo_dir: Path, copilot_home: Path, python: str | None = None
) -> tuple[list[Path], Path]:
    """User-level Copilot agents and hooks; the hooks run this interpreter by absolute path."""
    agents = export_copilot_agents(repo_dir, copilot_home / "agents")
    bash, powershell = absolute_hook_prefixes(python)
    return agents, write_copilot_hooks(copilot_home / "hooks", bash, powershell)


def export_to_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[int, Path]:
    github_dir = workspace_dir / ".github"
    count = _export_skill_files(
        repo_dir / "skills", github_dir / "prompts", ".prompt.md", render_copilot_prompt_file
    )
    export_to_copilot_skills(repo_dir, github_dir / "skills")
    export_copilot_agents(repo_dir, github_dir / "agents")
    write_copilot_hooks(github_dir / "hooks", "craftsman", "craftsman")
    copilot_instructions = github_dir / "copilot-instructions.md"
    rules_content = _rewrite_antigravity_paths(
        render_combined_rules(repo_dir / "rules", with_frontmatter=False)
    )
    upsert_managed_markdown(copilot_instructions, rules_content)
    return count, copilot_instructions


def export_to_claude_user(repo_dir: Path, claude_dir: Path) -> tuple[int, Path]:
    rules_out_dir = claude_dir / "rules"
    rules_out_dir.mkdir(parents=True, exist_ok=True)
    count = _export_skill_files(
        repo_dir / "skills", claude_dir / "commands", ".md", render_claude_command_file
    )
    rules_file = rules_out_dir / "craftsman-rules.md"
    write_text_atomic(
        rules_file,
        _rewrite_antigravity_paths(render_combined_rules(repo_dir / "rules", with_frontmatter=False)),
    )
    export_claude_agents(repo_dir, claude_dir / "agents")
    return count, rules_file


def export_to_agents_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[Path, Path]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agents_content = render_agents_md(repo_dir)
    agents_md_path = workspace_dir / "AGENTS.md"
    upsert_managed_markdown(agents_md_path, agents_content)

    cursor_rules_dir = workspace_dir / ".cursor" / "rules"
    cursor_rules_dir.mkdir(parents=True, exist_ok=True)
    cursor_mdc = cursor_rules_dir / "craftsman.mdc"
    cursor_header = (
        "---\n"
        "description: Craftsman Agentic Engineering & Git Workflow Rules\n"
        'globs: "*"\n'
        "alwaysApply: true\n"
        "---\n\n"
    )
    write_text_atomic(cursor_mdc, cursor_header + agents_content)
    return agents_md_path, cursor_mdc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="craftsman export",
        description="Export Craftsman to GitHub Copilot, Claude Code, OpenAI Codex, and Cursor.",
    )
    parser.add_argument(
        "--repo-dir",
        default=str(default_repo_dir()),
        help="Path to the craftsman repository root",
    )
    parser.add_argument(
        "--user-dir",
        nargs="?",
        const="",
        help="Export prompts, instructions and agents to a VS Code User prompts directory "
        "(defaults to the OS-specific Code/User/prompts); also writes the user-level Copilot "
        "agents and hooks (see --copilot-home)",
    )
    parser.add_argument(
        "--copilot-skills-dir",
        nargs="?",
        const="",
        help="Export native Copilot Agent Skills (defaults to ~/.copilot/skills); also writes the "
        "user-level Copilot agents and hooks (see --copilot-home)",
    )
    parser.add_argument(
        "--copilot-home",
        help="Copilot user directory for agents/ and hooks/craftsman.json (default: ~/.copilot); "
        "the hooks run this Python interpreter by absolute path",
    )
    parser.add_argument(
        "--workspace",
        help="Export to a project workspace (.github/prompts, skills, agents, hooks and "
        "copilot-instructions.md)",
    )
    parser.add_argument(
        "--claude-dir",
        nargs="?",
        const="",
        help="Export commands, rules and subagents to a Claude Code directory (defaults to ~/.claude)",
    )
    parser.add_argument(
        "--claude-hooks",
        action="store_true",
        help="With --claude-dir: merge the Craftsman hooks into <claude-dir>/settings.json "
        "(this is the default; other hooks are kept and a settings.json.bak is written before "
        "the first change)",
    )
    parser.add_argument(
        "--no-claude-hooks",
        action="store_true",
        help="With --claude-dir: do not touch <claude-dir>/settings.json",
    )
    parser.add_argument(
        "--agents-workspace",
        help="Export universal AGENTS.md and .cursor/rules/craftsman.mdc into a target project workspace",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_dir = Path(args.repo_dir).resolve()

    if (args.claude_hooks or args.no_claude_hooks) and args.claude_dir is None:
        parser.error("--claude-hooks and --no-claude-hooks require --claude-dir")
    if args.claude_hooks and args.no_claude_hooks:
        parser.error("--claude-hooks and --no-claude-hooks are mutually exclusive")
    if not (
        args.user_dir is not None
        or args.copilot_skills_dir is not None
        or args.copilot_home
        or args.workspace
        or args.claude_dir is not None
        or args.agents_workspace
    ):
        parser.error(
            "Specify at least one target: --user-dir, --copilot-skills-dir, --copilot-home, "
            "--workspace <path>, --claude-dir, or --agents-workspace <path>"
        )

    skill_manifests = sorted((repo_dir / "skills").glob("*/SKILL.md"))
    if not skill_manifests:
        sys.stderr.write(f"[ERROR] No skill manifests (skills/*/SKILL.md) found under {repo_dir}\n")
        return 1
    try:
        _run_exports(args, repo_dir)
    except (ExportError, OSError) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    return 0


def _run_exports(args: argparse.Namespace, repo_dir: Path) -> None:
    if args.user_dir is not None:
        target = Path(args.user_dir) if args.user_dir else default_vscode_prompts_dir()
        count, instr = export_to_vscode_user(repo_dir, target)
        print(f"[OK] Exported {count} Copilot prompt files and the Craftsman agents -> {target}")
        print(f"[OK] Exported global Copilot instructions -> {instr}")
        if not args.user_dir and args.copilot_skills_dir is None:
            skills_target = default_copilot_skills_dir()
            skills_count = export_to_copilot_skills(repo_dir, skills_target)
            print(f"[OK] Exported {skills_count} Copilot Agent Skills -> {skills_target}")

    if args.copilot_skills_dir is not None:
        skills_target = (
            Path(args.copilot_skills_dir) if args.copilot_skills_dir else default_copilot_skills_dir()
        )
        skills_count = export_to_copilot_skills(repo_dir, skills_target)
        print(f"[OK] Exported {skills_count} Copilot Agent Skills -> {skills_target}")

    # Explicit target directories (tests, sandboxes) leave the real ~/.copilot alone.
    if args.copilot_home or "" in (args.user_dir, args.copilot_skills_dir):
        copilot_home = Path(args.copilot_home) if args.copilot_home else default_copilot_home()
        agents, hooks_file = export_copilot_home(repo_dir, copilot_home)
        print(f"[OK] Exported {len(agents)} Copilot custom agents -> {copilot_home / 'agents'}")
        print(f"[OK] Exported Copilot hooks -> {hooks_file}")

    if args.workspace:
        ws = Path(args.workspace).resolve()
        count, instr = export_to_workspace(repo_dir, ws)
        github = ws / ".github"
        print(f"[OK] Exported {count} workspace prompt files -> {github / 'prompts'}")
        print(f"[OK] Exported {count} workspace Agent Skills -> {github / 'skills'}")
        print(f"[OK] Exported workspace custom agents -> {github / 'agents'}")
        print(
            f"[OK] Exported workspace hooks (runs `craftsman` from PATH) -> {github / 'hooks' / HOOKS_FILENAME}"
        )
        print(f"[OK] Exported workspace Copilot instructions -> {instr}")

    if args.claude_dir is not None:
        target_claude = Path(args.claude_dir) if args.claude_dir else default_claude_dir()
        count, rules_file = export_to_claude_user(repo_dir, target_claude)
        print(f"[OK] Exported {count} Claude Code slash commands -> {target_claude / 'commands'}")
        print(f"[OK] Exported Claude Code reviewer and verifier subagents -> {target_claude / 'agents'}")
        print(f"[OK] Exported Claude Code global rules -> {rules_file}")
        if not args.no_claude_hooks:
            settings = target_claude / "settings.json"
            bash_prefix, _ = absolute_hook_prefixes()
            changed = merge_claude_hooks(settings, bash_prefix)
            print(
                f"[OK] Claude Code hooks {'merged into' if changed else 'already up to date in'} {settings}"
            )

    if args.agents_workspace:
        ws = Path(args.agents_workspace).resolve()
        agents_md, cursor_mdc = export_to_agents_workspace(repo_dir, ws)
        print(f"[OK] Exported universal AGENTS.md (Codex/Cursor/Windsurf) -> {agents_md}")
        print(f"[OK] Exported Cursor rule -> {cursor_mdc}")


if __name__ == "__main__":
    raise SystemExit(main())
