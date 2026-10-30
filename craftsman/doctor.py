"""`craftsman doctor`: check the repository's assets and every installed Craftsman platform."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from craftsman import __version__
from craftsman.assets import craftsman_agent_names, craftsman_skill_names, parse_frontmatter
from craftsman.export import (
    CLAUDE_HOOK_EVENTS,
    COPILOT_DISCOVERY,
    HOOKS_FILENAME,
    CopilotClient,
    CopilotDiscoveryLocation,
    has_user_copilot_install,
    has_workspace_copilot_install,
    is_craftsman_hook_command,
    iter_discovered_files,
    resolve_discovery_dirs,
)
from craftsman.generated import ExportError
from craftsman.legacy_githooks import (
    git_config_get,
    is_craftsman_hooks_path,
    legacy_git_hooks_bases,
)
from craftsman.manifest import InstallError, TargetKind, load_manifest
from craftsman.paths import (
    default_claude_dir,
    default_copilot_home,
    default_copilot_skills_dir,
    default_vscode_prompts_dir,
    resolve_craftsman_pkg_dir,
    user_home,
)
from craftsman.records import AGENT_ROLES

CLAUDE_AGENT_NAMES = tuple(AGENT_ROLES)
AGENT_NAMES = ("craftsman", *CLAUDE_AGENT_NAMES)
REQUIRED_SCRIPTS = (
    "craftsman-scope-guard.py",
    "craftsman-stop-gate.py",
    "copilot-export.py",
    "craftsman-cli.py",
)
MIN_RULE_FILES = 1
LEFTOVER_GIT_HOOKS_PROBLEM = (
    "leftover git hooks from Craftsman 3.x; run `craftsman install` or `craftsman uninstall`"
)
# A SKILL.md body shorter than this is a stub, not a skill.
MIN_SKILL_BODY_CHARS = 100
_CLAUDE_SCOPES = ("claude-user", "claude-workspace")


class HookState(str, Enum):
    NOT_INSTALLED = "not installed"
    OK = "ok"
    BROKEN = "broken"


@dataclass(frozen=True)
class HooksReport:
    """A Copilot hooks file, the programs its commands need that are missing, or why it is unusable."""

    path: str
    state: HookState
    missing: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class SkillsReport:
    path: str
    installed: bool
    code_review_lenses: bool | None  # None when code-review itself is not installed


@dataclass(frozen=True)
class AgentsReport:
    user: bool
    workspace: bool | None  # None without --workspace


@dataclass(frozen=True)
class CopilotDuplicate:
    client: CopilotClient
    kind: str
    name: str
    paths: tuple[str, ...]
    canonical_path: str
    claude_collision: bool
    claude_scope: str | None
    scope_collision: bool


@dataclass(frozen=True)
class CopilotReport:
    user_hooks: HooksReport
    agents: AgentsReport
    skills: tuple[SkillsReport, ...]
    duplicates: tuple[CopilotDuplicate, ...]


class ClaudeHooks(str, Enum):
    MERGED = "merged"
    MISSING = "missing"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ClaudeReport:
    agents: bool
    hooks: ClaudeHooks
    detail: str  # what to do about the hooks, or why the settings cannot be read


@dataclass(frozen=True)
class GitConfig:
    user_name: str | None
    user_email: str | None
    hooks_path: str | None


@dataclass(frozen=True)
class InstalledTargets:
    antigravity: bool
    claude_code: bool
    github_copilot: bool
    vscode_instructions: bool


@dataclass(frozen=True)
class DoctorReport:
    version: str
    healthy: bool
    python_version: str
    repo_dir: str
    skills_count: int
    expected_skills_count: int
    skill_errors: tuple[str, ...]
    rules_count: int
    missing_scripts: tuple[str, ...]
    config_drift: tuple[str, ...]
    git_config: GitConfig
    installed_targets: InstalledTargets
    copilot: CopilotReport
    claude: ClaudeReport
    problems: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# --- repository assets ----------------------------------------------------------------------


def plugin_skill_names(repo_dir: Path) -> tuple[str, ...]:
    """The skills `plugin.json` ships; an unreadable manifest is an error, not zero skills."""
    plugin_file = repo_dir / "plugin.json"
    try:
        data = json.loads(plugin_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read {plugin_file}: {exc}") from exc
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list) or not all(isinstance(name, str) for name in skills):
        raise ExportError(f"{plugin_file}: `skills` must be a list of skill names")
    repeated = sorted({name for name in skills if skills.count(name) > 1})
    if repeated:
        raise ExportError(f"{plugin_file}: `skills` lists {', '.join(repeated)} more than once")
    return tuple(skills)


def validate_skill_manifests(repo_dir: Path) -> tuple[int, list[str]]:
    """Check every skills/*/SKILL.md: frontmatter, folder named after the skill, a real body."""
    skills_dir = repo_dir / "skills"
    if not skills_dir.is_dir():
        return 0, [f"Missing skills directory: {skills_dir}"]
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    errors = [error for path in skill_files for error in _skill_manifest_errors(path)]
    return len(skill_files), errors


def _skill_manifest_errors(skill_path: Path) -> Iterator[str]:
    folder = skill_path.parent.name
    try:
        fields, raw_body = parse_frontmatter(skill_path.read_text(encoding="utf-8"), skill_path)
    except (ExportError, OSError, UnicodeDecodeError) as exc:
        yield f"{folder}: {exc}"
        return
    body = raw_body.strip()
    if not fields.get("description"):
        yield f"{folder}: frontmatter missing 'description:' field"
    name = fields.get("name", "")
    if name != folder:
        yield f"{folder}: the folder must be named after the skill ({name or 'no name:'})"
    if len(body) < MIN_SKILL_BODY_CHARS:
        yield f"{folder}: SKILL.md body is unexpectedly short ({len(body)} chars)"
    if folder == "craftsman":
        yield from _scope_sync_errors(skill_path.parent, body)


def _scope_sync_errors(skill_dir: Path, body: str) -> Iterator[str]:
    scope_path = skill_dir / "scope.md"
    if not scope_path.is_file():
        yield "craftsman: missing companion scope.md"
    elif scope_path.read_text(encoding="utf-8").strip() not in body:
        yield "craftsman: SKILL.md 'Search scope' section is out of sync with scope.md"


# --- Antigravity drift ----------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def detect_antigravity_drift(repo_dir: Path, cfg: Path) -> list[str]:
    """Files of the repository that differ from, or are missing in, the Antigravity install `cfg`."""
    has_skills = (cfg / "skills" / "craftsman" / "SKILL.md").is_file()
    has_plugin = (cfg / "plugins" / "craftsman" / "plugin.json").is_file()
    if not has_skills and not has_plugin:
        return []
    subdirs = [("scripts", "*.py"), ("craftsman", "**/*.py")]
    if has_skills:
        subdirs = [("skills", "*/SKILL.md"), ("rules", "*.md"), *subdirs]
    drifted: list[str] = []
    for subdir, pattern in subdirs:
        src_base = resolve_craftsman_pkg_dir(repo_dir) if subdir == "craftsman" else repo_dir / subdir
        drifted.extend(_copied_file_drift(src_base, cfg / subdir, subdir, pattern, cfg))
    if has_skills:
        drifted.extend(_hooks_json_drift(repo_dir / "hooks.json", cfg))
    return drifted


def _copied_file_drift(
    src_base: Path, dst_base: Path, label: str, pattern: str, cfg: Path
) -> Iterator[str]:
    if not src_base.is_dir() or not dst_base.is_dir():
        return
    for src_file in sorted(src_base.glob(pattern)):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src_base)
        if any(part in ("_assets", "__pycache__") for part in rel.parts):
            continue
        dst_file = dst_base / rel
        rel_posix = rel.as_posix()
        if not dst_file.is_file():
            yield f"{label}/{rel_posix} (missing in {cfg})"
        elif _sha256_file(src_file) != _sha256_file(dst_file):
            yield f"{label}/{rel_posix}"


def _hooks_json_drift(src_hooks_json: Path, cfg: Path) -> Iterator[str]:
    """Our hooks.json entries must be installed unchanged; only their `enabled` flag is the user's."""
    dst_hooks_json = cfg / "hooks.json"
    if not dst_hooks_json.is_file():
        yield f"hooks.json (missing in {cfg})"
        return
    try:
        src = json.loads(src_hooks_json.read_text(encoding="utf-8"))
        dst = json.loads(dst_hooks_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        yield f"hooks.json (unreadable: {exc})"
        return
    if not isinstance(dst, dict) or any(
        not _same_hook_entry(entry, dst.get(key)) for key, entry in src.items()
    ):
        yield "hooks.json"


def _same_hook_entry(ours: Any, installed: Any) -> bool:
    if not isinstance(installed, dict) or not isinstance(installed.get("enabled"), bool):
        return False
    return {k: v for k, v in installed.items() if k != "enabled"} == {
        k: v for k, v in ours.items() if k != "enabled"
    }


# --- Copilot and Claude Code ----------------------------------------------------------------


def command_executables(command: str, *, powershell: bool) -> list[str]:
    """The interpreter and script a hook command runs, i.e. the words before `hook`.

    Raises ValueError when the command's quoting does not parse.
    """
    if powershell:  # PowerShell quoting: backslashes are path separators, not escapes
        words = [word.strip("\"'") for word in shlex.split(command.removeprefix("& "), posix=False)]
    else:
        words = shlex.split(command)
    head = words[: words.index("hook")] if "hook" in words else words[:1]
    return [word for word in head if not word.startswith("-") and word != "craftsman.cli"]


def _is_runnable(executable: str) -> bool:
    if os.path.isabs(executable):
        return Path(executable).is_file()
    return shutil.which(executable) is not None


def inspect_copilot_hooks(hooks_file: Path) -> HooksReport:
    """Whether a Copilot hooks file exists and every program its commands run is present."""
    if not hooks_file.is_file():
        return HooksReport(str(hooks_file), HookState.NOT_INSTALLED, ())
    # Copilot runs the `powershell` command on Windows and the `bash` command elsewhere.
    key = "powershell" if os.name == "nt" else "bash"
    try:
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        entries = [entry for group in data["hooks"].values() for entry in group]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return HooksReport(str(hooks_file), HookState.BROKEN, (), f"unreadable hooks file: {exc!r}")
    commands = [entry.get(key) for entry in entries if isinstance(entry, dict)]
    if not commands or len(commands) != len(entries) or not all(isinstance(c, str) for c in commands):
        return HooksReport(
            str(hooks_file), HookState.BROKEN, (), f"a hook has no `{key}` command string"
        )
    try:
        executables = {
            exe
            for command in commands
            for exe in command_executables(str(command), powershell=key == "powershell")
        }
    except ValueError as exc:
        return HooksReport(str(hooks_file), HookState.BROKEN, (), f"unparsable hook command: {exc}")
    missing = sorted(exe for exe in executables if not _is_runnable(exe))
    return HooksReport(str(hooks_file), HookState.BROKEN if missing else HookState.OK, tuple(missing))


def _agents_installed(directory: Path, names: tuple[str, ...], suffix: str) -> bool:
    return all((directory / f"{name}{suffix}").is_file() for name in names)


def _skills_report(skills_dir: Path) -> SkillsReport:
    review = skills_dir / "code-review"
    lenses = (review / "lenses.md").is_file() if (review / "SKILL.md").is_file() else None
    return SkillsReport(str(skills_dir), (skills_dir / "craftsman" / "SKILL.md").is_file(), lenses)


_CLAUDE_HOOKS_HINT = "run craftsman install --claude"


def _claude_report(claude_dir: Path) -> ClaudeReport:
    agents = _agents_installed(claude_dir / "agents", CLAUDE_AGENT_NAMES, ".md")
    settings_file = claude_dir / "settings.json"
    if not settings_file.is_file():
        return ClaudeReport(agents, ClaudeHooks.MISSING, _CLAUDE_HOOKS_HINT)
    try:
        hooks = json.loads(settings_file.read_text(encoding="utf-8")).get("hooks", {})
        merged = all(
            any(
                is_craftsman_hook_command(entry.get("command"))
                for group in hooks.get(event, [])
                for entry in group.get("hooks", [])
            )
            for event in CLAUDE_HOOK_EVENTS
        )
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        return ClaudeReport(agents, ClaudeHooks.UNREADABLE, f"unreadable {settings_file} ({exc!r})")
    if merged:
        return ClaudeReport(agents, ClaudeHooks.MERGED, "")
    return ClaudeReport(agents, ClaudeHooks.MISSING, _CLAUDE_HOOKS_HINT)


# --- Copilot duplicates ---------------------------------------------------------------------


def loads_jsonc_dict(raw: str) -> dict[str, Any] | None:
    """Parse VS Code JSONC: `//` and `/* */` comments and trailing commas outside strings.

    Returns None when the text is valid but not a JSON object; raises json.JSONDecodeError
    when it is not valid JSONC.
    """
    data = json.loads("".join(_jsonc_to_json(raw)))
    return data if isinstance(data, dict) else None


def _jsonc_to_json(raw: str) -> Iterator[str]:
    """One pass over `raw` that knows whether it is inside a string, so `"C:\\\\"` closes."""
    index, length = 0, len(raw)
    pending_comma = ""
    while index < length:
        char = raw[index]
        if char == '"':
            end = _string_end(raw, index)
            yield pending_comma + raw[index:end]
            pending_comma, index = "", end
        elif raw.startswith("//", index):
            newline = raw.find("\n", index)
            index = length if newline == -1 else newline
        elif raw.startswith("/*", index):
            close = raw.find("*/", index + 2)
            index = length if close == -1 else close + 2
        elif char == ",":
            yield pending_comma
            pending_comma, index = ",", index + 1
        elif char in "}]":
            yield char  # a comma right before a closing bracket is a trailing comma: dropped
            pending_comma, index = "", index + 1
        elif char.isspace():
            yield char
            index += 1
        else:
            yield pending_comma + char
            pending_comma, index = "", index + 1
    yield pending_comma


def _string_end(raw: str, start: int) -> int:
    """The index just past the string literal opening at `start` (or the end of the text)."""
    index = start + 1
    while index < len(raw):
        if raw[index] == "\\":
            index += 2
        elif raw[index] == '"':
            return index + 1
        else:
            index += 1
    return len(raw)


def _claude_location_candidates(loc: CopilotDiscoveryLocation, home: Path | None) -> set[str]:
    if loc.scope == "claude-user":
        leaf = "agents" if loc.rel_path.endswith("agents") else "skills"
        return {
            f"~/.claude/{leaf}",
            f".claude/{leaf}",
            str(default_claude_dir(home) / leaf).replace("\\", "/"),
        }
    return {loc.rel_path, f"./{loc.rel_path}"}


def _is_vscode_location_disabled(
    loc: CopilotDiscoveryLocation, home: Path | None, profile: Path | None
) -> bool:
    """Return True if a `.claude` discovery location is explicitly disabled in VS Code settings."""
    if loc.scope not in _CLAUDE_SCOPES:
        return False
    setting_key = (
        "chat.agentFilesLocations" if loc.rel_path.endswith("agents") else "chat.agentSkillsLocations"
    )
    settings_files = [default_vscode_prompts_dir(home).parent / "settings.json"]
    if profile is not None:
        settings_files.append(profile / "settings.json")
    candidates = _claude_location_candidates(loc, home)
    for settings_path in settings_files:
        if not settings_path.is_file():
            continue
        try:
            data = loads_jsonc_dict(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExportError(f"cannot read VS Code settings {settings_path}: {exc}") from exc
        if data is None:
            raise ExportError(f"VS Code settings {settings_path} are not a JSON object")
        loc_map = data.get(setting_key)
        if not isinstance(loc_map, dict):
            continue
        for key, val in loc_map.items():
            norm = key.replace("\\", "/")
            if val is False and (norm in candidates or norm.removeprefix("./") in candidates):
                return True
    return False


def _collect_profile_matches(
    client: CopilotClient,
    locations: tuple[CopilotDiscoveryLocation, ...],
    valid_names: set[str],
    *,
    home: Path | None,
    workspace: Path | None,
    profile: Path | None,
) -> dict[str, list[tuple[CopilotDiscoveryLocation, Path]]]:
    found: dict[str, list[tuple[CopilotDiscoveryLocation, Path]]] = {}
    for loc in locations:
        if client == "vscode" and _is_vscode_location_disabled(loc, home, profile):
            continue
        for directory in resolve_discovery_dirs(loc, home=home, workspace=workspace, profile=profile):
            for entity_name, file_path in iter_discovered_files(directory, loc.pattern):
                if entity_name in valid_names:
                    found.setdefault(entity_name, []).append((loc, file_path))
    return found


def _build_copilot_duplicate(
    client: CopilotClient,
    kind: str,
    entity_name: str,
    entries: list[tuple[CopilotDiscoveryLocation, Path]],
) -> CopilotDuplicate:
    canonical_path = next((path for loc, path in entries if loc.canonical), entries[0][1])
    claude_scopes = [loc.scope for loc, _ in entries if loc.scope in _CLAUDE_SCOPES]
    return CopilotDuplicate(
        client=client,
        kind=kind,
        name=entity_name,
        paths=tuple(str(path) for _, path in entries),
        canonical_path=str(canonical_path),
        claude_collision=bool(claude_scopes),
        claude_scope=claude_scopes[-1] if claude_scopes else None,
        scope_collision=len({loc.scope for loc, _ in entries if loc.canonical}) > 1,
    )


def scan_copilot_duplicates(
    repo_dir: Path, *, home: Path | None = None, workspace: Path | None = None
) -> list[CopilotDuplicate]:
    """Craftsman agents and skills that Copilot discovers more than once."""
    valid = {
        "agent": set(craftsman_agent_names(repo_dir)),
        "skill": set(craftsman_skill_names(repo_dir)),
    }
    profiles: list[Path | None] = [None]
    profiles_root = default_vscode_prompts_dir(home).parent / "profiles"
    if profiles_root.is_dir():
        profiles.extend(p for p in sorted(profiles_root.iterdir()) if p.is_dir())
    by_key: dict[tuple[CopilotClient, str, str], list[tuple[CopilotDiscoveryLocation, Path]]] = {}
    for client in ("vscode", "copilot-cli"):
        locations = {
            "agent": COPILOT_DISCOVERY[(client, "agent")],
            "skill": (
                *COPILOT_DISCOVERY[(client, "skill")],
                *COPILOT_DISCOVERY.get((client, "prompt"), ()),
            ),
        }
        for kind, locs in locations.items():
            for profile in profiles if client == "vscode" else [None]:
                matches_map = _collect_profile_matches(
                    client, locs, valid[kind], home=home, workspace=workspace, profile=profile
                )
                for entity_name, matches in matches_map.items():
                    if len(matches) > 1:
                        _extend_unique(by_key.setdefault((client, kind, entity_name), []), matches)
    return [
        _build_copilot_duplicate(client, kind, name, entries)
        for (client, kind, name), entries in sorted(by_key.items(), key=lambda item: item[0])
    ]


def _extend_unique(
    existing: list[tuple[CopilotDiscoveryLocation, Path]],
    matches: list[tuple[CopilotDiscoveryLocation, Path]],
) -> None:
    seen = {path for _, path in existing}
    for loc, path in matches:
        if path not in seen:
            seen.add(path)
            existing.append((loc, path))


def _format_duplicate_problem(dup: CopilotDuplicate) -> str:
    prefix = f"Duplicate Copilot {dup.kind} '{dup.name}' in {dup.client}: {', '.join(dup.paths)}"
    if dup.claude_collision:
        setting = "chat.agentFilesLocations" if dup.kind == "agent" else "chat.agentSkillsLocations"
        leaf = "agents" if dup.kind == "agent" else "skills"
        loc_key = f"~/.claude/{leaf}" if dup.claude_scope == "claude-user" else f".claude/{leaf}"
        return (
            f"{prefix} "
            f'(VS Code also reads Claude Code directories; set "{setting}": {{"{loc_key}": false}} '
            "in VS Code settings.json to disable)"
        )
    if dup.scope_collision:
        return (
            f"{prefix} "
            "(both user and workspace scopes are installed; uninstall one scope with "
            "`craftsman uninstall --copilot` or `craftsman uninstall --copilot-workspace <dir>`)"
        )
    return f"{prefix} (keep {dup.canonical_path}; rename or remove the other copies)"


# --- legacy git hooks -----------------------------------------------------------------------


def inspect_legacy_git_hooks(hooks_path: str | None, home: Path) -> list[str]:
    """Warn when a manifest still lists Craftsman 3.x git hooks or `core.hooksPath` points to them."""
    problems: list[str] = []
    has_leftover = is_craftsman_hooks_path(hooks_path, home)
    for base in legacy_git_hooks_bases(home):
        try:
            manifest = load_manifest(base)
        except InstallError as exc:
            problems.append(str(exc))
            continue
        if manifest is not None and TargetKind.GIT_HOOKS in manifest.targets:
            has_leftover = True
    if has_leftover:
        problems.append(LEFTOVER_GIT_HOOKS_PROBLEM)
    return problems


# --- report ---------------------------------------------------------------------------------


def _copilot_report(repo_dir: Path, workspace: Path | None, home: Path) -> CopilotReport:
    copilot_home = default_copilot_home(home)
    skills = [_skills_report(default_copilot_skills_dir(home))]
    if workspace is not None:
        skills.append(_skills_report(workspace / ".github" / "skills"))
    workspace_agents = (
        _agents_installed(workspace / ".github" / "agents", AGENT_NAMES, ".agent.md")
        if workspace is not None
        else None
    )
    return CopilotReport(
        user_hooks=inspect_copilot_hooks(copilot_home / "hooks" / HOOKS_FILENAME),
        agents=AgentsReport(
            _agents_installed(copilot_home / "agents", AGENT_NAMES, ".agent.md"), workspace_agents
        ),
        skills=tuple(skills),
        duplicates=tuple(scan_copilot_duplicates(repo_dir, home=home, workspace=workspace)),
    )


def _platform_problems(copilot: CopilotReport, claude: ClaudeReport) -> list[str]:
    problems = [
        f"{entry.path}/code-review has no lenses.md next to SKILL.md; re-run `craftsman install --copilot`"
        for entry in copilot.skills
        if entry.code_review_lenses is False
    ]
    hooks = copilot.user_hooks
    if hooks.error is not None:
        problems.append(f"{hooks.path}: {hooks.error}; re-run `craftsman install --copilot`")
    elif hooks.missing:
        problems.append(
            f"{hooks.path} runs missing programs ({', '.join(hooks.missing)}); "
            "re-run `craftsman install --copilot`"
        )
    if claude.hooks is ClaudeHooks.UNREADABLE:
        problems.append(f"Claude Code settings: {claude.detail}")
    problems.extend(_format_duplicate_problem(dup) for dup in copilot.duplicates)
    return problems


def _installed_targets(repo_dir: Path, workspace: Path | None, home: Path) -> InstalledTargets:
    copilot_home = default_copilot_home(home)
    gemini = home / ".gemini" / "config"
    user_copilot = (
        (default_copilot_skills_dir(home) / "craftsman" / "SKILL.md").is_file()
        or (copilot_home / "agents" / "craftsman.agent.md").is_file()
        or has_user_copilot_install(repo_dir, home)
    )
    workspace_copilot = workspace is not None and has_workspace_copilot_install(workspace, repo_dir)
    instructions = default_vscode_prompts_dir(home) / "craftsman-rules.instructions.md"
    return InstalledTargets(
        antigravity=(gemini / "skills" / "craftsman" / "SKILL.md").is_file()
        or (gemini / "plugins" / "craftsman" / "plugin.json").is_file(),
        claude_code=(home / ".claude" / "commands" / "craftsman.md").is_file()
        or (home / ".claude" / "agents" / "craftsman-reviewer.md").is_file(),
        github_copilot=user_copilot or workspace_copilot,
        vscode_instructions=instructions.is_file(),
    )


def _plugin_mismatch(repo_dir: Path, plugin_skills: tuple[str, ...]) -> list[str]:
    """Skills `plugin.json` lists without a folder, and skill folders it does not list."""
    listed = set(plugin_skills)
    folders = {path.parent.name for path in repo_dir.glob("skills/*/SKILL.md")}
    return [
        *(
            f"plugin.json lists skill {name!r} that skills/ does not have"
            for name in sorted(listed - folders)
        ),
        *(f"skills/{name} is not listed in plugin.json" for name in sorted(folders - listed)),
    ]


def _git_config(home: Path) -> tuple[GitConfig, list[str]]:
    """The global git settings doctor shows; git that cannot read them is a problem to report."""
    try:
        return GitConfig(
            git_config_get("user.name", home),
            git_config_get("user.email", home),
            git_config_get("core.hooksPath", home),
        ), []
    except InstallError as exc:
        return GitConfig(None, None, None), [str(exc)]


def build_report(repo_dir: Path, workspace: Path | None, home: Path) -> DoctorReport:
    plugin_skills = plugin_skill_names(repo_dir)
    skill_count, skill_errors = validate_skill_manifests(repo_dir)
    skill_errors += _plugin_mismatch(repo_dir, plugin_skills)
    expected_skills = len(plugin_skills)
    rule_files = sorted((repo_dir / "rules").glob("*.md"))
    missing_scripts = tuple(
        name for name in REQUIRED_SCRIPTS if not (repo_dir / "scripts" / name).is_file()
    )
    git, git_problems = _git_config(home)
    config_drift = tuple(detect_antigravity_drift(repo_dir, home / ".gemini" / "config"))
    copilot = _copilot_report(repo_dir, workspace, home)
    claude = _claude_report(default_claude_dir(home))
    problems = (
        *git_problems,
        *_platform_problems(copilot, claude),
        *inspect_legacy_git_hooks(git.hooks_path, home),
    )
    healthy = (
        skill_count == expected_skills
        and not skill_errors
        and len(rule_files) >= MIN_RULE_FILES
        and not missing_scripts
        and not config_drift
        and not problems
    )
    return DoctorReport(
        version=__version__,
        healthy=healthy,
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        repo_dir=str(repo_dir),
        skills_count=skill_count,
        expected_skills_count=expected_skills,
        skill_errors=tuple(skill_errors),
        rules_count=len(rule_files),
        missing_scripts=missing_scripts,
        config_drift=config_drift,
        git_config=git,
        installed_targets=_installed_targets(repo_dir, workspace, home),
        copilot=copilot,
        claude=claude,
        problems=tuple(problems),
    )


def print_report(report: DoctorReport) -> None:
    repo_dir = Path(report.repo_dir)
    status = "HEALTHY" if report.healthy else "ISSUES DETECTED"
    git = report.git_config
    print(f"Craftsman Doctor v{report.version} [{status}]")
    print(f"  Python runtime : {report.python_version}")
    print(f"  Repository     : {repo_dir}")
    print(
        f"  Skills audited : {report.skills_count}/{report.expected_skills_count} "
        f"({len(report.skill_errors)} errors)"
    )
    print(f"  Rules audited  : {report.rules_count}")
    present = len(REQUIRED_SCRIPTS) - len(report.missing_scripts)
    print(f"  Core scripts   : {present}/{len(REQUIRED_SCRIPTS)} present")
    print(f"  Git identity   : {git.user_name or 'unset'} <{git.user_email or 'unset'}>")
    print(f"  Git hooksPath  : {git.hooks_path or 'unset'}")
    if report.config_drift:
        sync_hint = "run 'craftsman install --antigravity' to sync"
        print(
            f"  Config drift   : {len(report.config_drift)} file(s) differ between repo and "
            f"~/.gemini/config ({sync_hint})"
        )
    copilot = report.copilot
    print(f"  Copilot hooks  : {copilot.user_hooks.state.value} ({copilot.user_hooks.path})")
    workspace_agents = (
        f", workspace {_yes_no(bool(copilot.agents.workspace))}"
        if copilot.agents.workspace is not None
        else ""
    )
    print(f"  Copilot agents : user {_yes_no(copilot.agents.user)}{workspace_agents}")
    claude = report.claude
    hooks = (
        "installed" if claude.hooks is ClaudeHooks.MERGED else f"{claude.hooks.value} ({claude.detail})"
    )
    print(f"  Claude Code    : agents {_yes_no(claude.agents)}, hooks {hooks}")
    for problem in (*report.problems, *report.skill_errors):
        print(f"  [ERROR] {problem}", file=sys.stderr)
    for script in report.missing_scripts:
        print(f"  [ERROR] Missing script: scripts/{script}", file=sys.stderr)


def _yes_no(value: bool) -> str:
    return "installed" if value else "missing"


def run_doctor(repo_dir: Path, *, as_json: bool, workspace: Path | None) -> int:
    """Inspect repository integrity, Git configuration, drift, and installed agent platforms."""
    report = build_report(repo_dir, workspace, user_home())
    if as_json:
        sys.stdout.write(json.dumps(report.to_json(), indent=2, ensure_ascii=False) + "\n")
    else:
        print_report(report)
    return 0 if report.healthy else 1
