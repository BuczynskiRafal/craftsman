"""Repository search-scope enforcement (`craftsman/scope.py`).

Models scope checks as candidate paths against forbidden zones:
1. Extract candidate paths from shell commands (any non-flag argument that looks like a path or
   exists on disk after `~` and environment-variable expansion) or structured IDE tool inputs.
2. Allow paths that resolve inside the active git work tree, its own `<git-dir>/craftsman`
   directory, or `craftsman.scopeAllow` (excluding other worktrees' git directories).
3. Outside those roots, deny recursive searches and deny non-recursive reads targeting session
   stores, shell histories, other git repositories/worktrees, or symlinks escaping the work tree.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from craftsman.export import user_home
from craftsman.guard import HEREDOC_OPEN_RE, _split_shell_events, iter_simple_commands
from craftsman.records import CRAFTSMAN_AGENTS

ScopeMode = Literal["deny", "warn", "off"]
SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "run_in_terminal",
        "runinterminal",
        "terminal",
        "powershell",
        "pwsh",
        "shell",
        "run_command",
    }
)
READ_TOOL_NAMES = frozenset({"read", "read_file", "view_file", "notebookread", "view"})
SEARCH_TOOL_NAMES = frozenset(
    {
        "grep",
        "grep_search",
        "ripgrep",
        "glob",
        "file_search",
        "find_by_name",
        "ls",
        "list_dir",
        "list_directory",
    }
)
SCOPE_CHECKED_TOOLS = SHELL_TOOL_NAMES | READ_TOOL_NAMES | SEARCH_TOOL_NAMES
ALWAYS_RECURSIVE_CMDS = frozenset({"rg", "ripgrep", "ag", "ack", "fd", "fdfind", "find", "tree", "du"})
READ_CMDS = frozenset({"cat", "head", "tail", "less", "more", "sed", "awk", "gawk", "bat"})
FORBIDDEN_HOME_RELPATHS = (
    ".copilot/session-state",
    ".claude/projects",
    ".claude/sessions",
    ".gemini/antigravity/brain",
    ".gemini/antigravity/conversations",
    ".Cursor/User/workspaceStorage",
    ".config/Code/User/workspaceStorage",
    "Library/Application Support/Code/User/workspaceStorage",
    ".zsh_history",
    ".bash_history",
    ".python_history",
    ".lesshst",
    ".sh_history",
)
ENV_VAR_RE = re.compile(
    r"\$\{([A-Za-z_]\w*)\}|\$env:([A-Za-z_]\w*)|\$([A-Za-z_]\w*)|%([A-Za-z_]\w*)%",
    re.IGNORECASE,
)
GLOB_META_RE = re.compile(r"[*?\[\{]")
WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):([/\\].*|$)")
SCOPE_ALLOW_SPLIT_RE = re.compile(r";|(?<![A-Za-z]):|(?<=[A-Za-z]{2}):")


@dataclass(frozen=True)
class ScopePolicy:
    mode: ScopeMode = "deny"
    allowed_dirs: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ScopeDecision:
    decision: Literal["allow", "deny"]
    reason: str = ""


class ScopeConfigError(ValueError):
    """`git config craftsman.scope` holds a value that is not a scope mode."""


def is_craftsman_agent(agent: str) -> bool:
    """Whether the agent is one of Craftsman's own agents (its exact name, in any case)."""
    return agent.strip().lower() in CRAFTSMAN_AGENTS


def is_scope_checked_tool(tool_name: str) -> bool:
    return tool_name.strip().lower() in SCOPE_CHECKED_TOOLS


def _deny(target_desc: str, detail: str = "is outside the repository work tree") -> ScopeDecision:
    return ScopeDecision(
        decision="deny",
        reason=(
            f"[CRAFTSMAN SCOPE-GUARD] Blocked out-of-scope access: {target_desc} {detail}.\n"
            "Search and read only inside the current repository work tree (`git ls-files`, `git grep`).\n"
            "If this task genuinely requires reading another directory, ask the user or run:\n"
            "  git config craftsman.scopeAllow /abs/path"
        ),
    )


def _git_config_lines(cwd: Path, *args: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "config", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return (
        [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if proc.returncode == 0
        else []
    )


def load_scope_policy(cwd: Path, repo_root: Path | None = None) -> ScopePolicy:
    target = repo_root or cwd
    mode_lines = _git_config_lines(target, "--get", "craftsman.scope")
    mode = _scope_mode(mode_lines[-1] if mode_lines else "deny")
    allowed: list[Path] = []
    for line in _git_config_lines(target, "--get-all", "craftsman.scopeAllow"):
        for part in SCOPE_ALLOW_SPLIT_RE.split(line):
            if not part.strip():
                continue
            expanded = Path(_expand_vars(part.strip())).expanduser()
            resolved = (
                (target / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()
            )
            if resolved not in allowed:
                allowed.append(resolved)
    return ScopePolicy(mode=mode, allowed_dirs=tuple(allowed))


def _scope_mode(raw: str) -> ScopeMode:
    for mode in get_args(ScopeMode):
        if raw.strip().lower() == mode:
            return mode  # type: ignore[no-any-return]
    raise ScopeConfigError(
        f"git config craftsman.scope is {raw!r}; use one of {', '.join(get_args(ScopeMode))}"
    )


def _expand_vars(value: str) -> str:
    home = str(user_home())
    env_map = {k.upper(): v for k, v in os.environ.items()}
    env_map.setdefault("HOME", home)
    env_map.setdefault("USERPROFILE", home)
    expanded = ENV_VAR_RE.sub(
        lambda m: env_map.get(
            (m.group(1) or m.group(2) or m.group(3) or m.group(4) or "").upper(), m.group(0)
        ),
        value,
    )
    return home + expanded[1:] if expanded == "~" or expanded.startswith(("~/", "~\\")) else expanded


def _to_path(expanded: str, base_cwd: Path) -> Path:
    drive_match = WIN_DRIVE_RE.match(expanded)
    if drive_match and os.name != "nt":
        rest = drive_match.group(2).replace("\\", "/").lstrip("/")
        return Path(f"/{drive_match.group(1).lower()}/{rest}")
    path = Path(expanded)
    return path if path.is_absolute() else (base_cwd / path)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_forbidden_store(candidate: Path) -> bool:
    home = user_home()
    resolved_cand = candidate.resolve()
    for base in {home, home.resolve()}:
        for rel in FORBIDDEN_HOME_RELPATHS:
            store = base / rel
            if _is_within(candidate, store) or _is_within(resolved_cand, store.resolve()):
                return True
    return False


def _is_in_other_git_repo(resolved: Path, allowed_roots: Sequence[Path]) -> bool:
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            return not any(_is_within(parent, root) for root in allowed_roots)
    return False


@dataclass(frozen=True)
class _WorktreeGitScope:
    own_git_dir: Path | None = None
    own_craftsman_dir: Path | None = None
    forbidden_git_dirs: tuple[Path, ...] = ()


def _read_git_pointer(path: Path, prefix: str = "") -> str | None:
    with path.open("rb") as fh:
        line = fh.readline(4096).decode("utf-8", errors="replace").rstrip("\r\n")
    if not line.startswith(prefix):
        return None
    return line[len(prefix) :].strip() or None


def _worktree_git_scope(resolved_repo: Path) -> _WorktreeGitScope:
    dot_git = resolved_repo / ".git"
    try:
        if dot_git.is_dir():
            own_git = dot_git.resolve()
        elif dot_git.is_file() and (raw_git := _read_git_pointer(dot_git, "gitdir:")) is not None:
            own_git = (resolved_repo / raw_git).resolve()
        else:
            return _WorktreeGitScope()
        commondir = own_git / "commondir"
        if commondir.is_file() and (raw_common := _read_git_pointer(commondir)) is not None:
            common_git = (own_git / raw_common).resolve()
        elif dot_git.is_file() and own_git.parent.name == "worktrees":
            common_git = own_git.parent.parent
        else:
            common_git = own_git
        craftsman_dir = own_git / "craftsman"
        own_craftsman = None if craftsman_dir.is_symlink() else craftsman_dir.resolve()
        worktrees_dir = (common_git / "worktrees").resolve()
        extra_forbidden = (
            (common_git, own_git)
            if common_git != own_git
            else (own_git,)
            if not _is_within(own_git, resolved_repo)
            else ()
        )
        return _WorktreeGitScope(own_git, own_craftsman, (worktrees_dir, *extra_forbidden))
    except (OSError, RuntimeError):
        return _WorktreeGitScope()


def _escapes_via_symlink(candidate: Path, roots: Sequence[Path]) -> bool:
    abs_cand = Path(os.path.abspath(str(candidate)))
    if any(_is_within(abs_cand, root) for root in roots):
        return True
    for parent in abs_cand.parents:
        try:
            resolved_parent = parent.resolve()
        except (OSError, RuntimeError):
            continue
        if any(_is_within(resolved_parent, root) for root in roots):
            return True
    return False


def _check_target(
    candidate: Path, *, is_recursive: bool, repo_root: Path, policy: ScopePolicy, label: str
) -> ScopeDecision:
    if _is_forbidden_store(candidate):
        return _deny(label, "targets a protected session store or shell history")
    resolved_repo = repo_root.resolve()
    resolved_extras = tuple(d.resolve() for d in policy.allowed_dirs)
    git_scope = _worktree_git_scope(resolved_repo)
    resolved = candidate.resolve()
    if git_scope.own_craftsman_dir is not None and _is_within(resolved, git_scope.own_craftsman_dir):
        return ScopeDecision("allow")
    if any(_is_within(resolved, d) for d in git_scope.forbidden_git_dirs):
        return _deny(label, "belongs to another git worktree")
    if any(_is_within(resolved, extra) for extra in resolved_extras):
        return ScopeDecision("allow")
    if _is_within(resolved, resolved_repo):
        return ScopeDecision("allow")
    if is_recursive:
        return _deny(label)
    escape_roots = (resolved_repo, *((git_scope.own_git_dir,) if git_scope.own_git_dir else ()))
    if _escapes_via_symlink(candidate, escape_roots):
        return _deny(label, "escapes the repository work tree via symlink")
    allowed_roots = (
        resolved_repo,
        *resolved_extras,
        *((git_scope.own_craftsman_dir,) if git_scope.own_craftsman_dir else ()),
    )
    if _is_in_other_git_repo(resolved, allowed_roots):
        return _deny(label, "belongs to another git repository")
    return ScopeDecision("allow")


def _classify_shell_tool(
    tokens: Sequence[str],
) -> Literal["recursive", "read", "locate", "mdfind_global", "ignore"]:
    cmd = Path(tokens[0].replace("\\", "/")).name.lower().removesuffix(".exe")
    flags = [tok for tok in tokens[1:] if tok.startswith("-") or re.match(r"^/[A-Za-z0-9]$", tok)]
    if cmd == "locate":
        return "locate"
    if cmd == "mdfind":
        return "recursive" if "-onlyin" in tokens[1:] else "mdfind_global"
    if cmd == "git":
        return "recursive" if "grep" in tokens[1:] else "ignore"
    if cmd in ALWAYS_RECURSIVE_CMDS:
        return "recursive"
    if cmd in ("grep", "egrep", "fgrep"):
        is_rec = any(
            f in ("-r", "-R", "--recursive", "--dereference-recursive")
            or re.match(r"^-[A-Za-z]*[rR]", f)
            for f in flags
        )
        return "recursive" if is_rec else "read"
    if cmd == "ls":
        is_rec = any(f in ("-R", "--recursive") or re.match(r"^-[A-Za-z]*R", f) for f in flags)
        return "recursive" if is_rec else "read"
    if cmd in ("get-childitem", "gci", "dir"):
        is_rec = any(
            f.lower() in ("-r", "-recurse", "/s") or f.lower().startswith("-rec") for f in flags
        )
        return "recursive" if is_rec else "read"
    return "read" if cmd in READ_CMDS else "ignore"


def _extract_path_candidates(args: Sequence[str], base_cwd: Path) -> list[Path]:
    candidates: list[Path] = []
    for raw in args:
        if raw.startswith("-"):
            if "=" not in raw:
                continue
            raw = raw.split("=", 1)[1]
        elif re.match(r"^/[A-Za-z0-9]$", raw):
            continue
        expanded = _expand_vars(raw)
        looks_like_path = (
            expanded != raw
            or expanded in (".", "..", "/")
            or expanded.startswith(("./", "../", ".\\", "..\\", "/", "~"))
            or bool(WIN_DRIVE_RE.match(expanded))
            or "/" in expanded
            or "\\" in expanded
        )
        candidate = _to_path(expanded, base_cwd)
        if looks_like_path or candidate.exists() or candidate.is_symlink():
            candidates.append(candidate)
    return candidates


def _has_unmatched_quotes(command_line: str) -> bool:
    for seg in _split_shell_events(command_line):
        if seg in ("(", ")"):
            continue
        first_line = seg.split("\n", 1)[0]
        hd_match = HEREDOC_OPEN_RE.search(first_line)
        try:
            shlex.split(first_line[: hd_match.start()] if hd_match else seg, posix=True)
        except ValueError:
            return True
    return False


def _evaluate_shell(command_line: str, cwd: Path, repo_root: Path, policy: ScopePolicy) -> ScopeDecision:
    if not command_line.strip():
        return ScopeDecision("allow")
    if _has_unmatched_quotes(command_line):
        sys.stderr.write("craftsman scope: blocking unparseable shell command (unmatched quotes)\n")
        return _deny(f"`{command_line.strip()[:80]}`", "has unmatched quotes")
    for simple in iter_simple_commands(command_line, str(cwd)):
        kind = _classify_shell_tool(simple.tokens)
        if kind == "ignore":
            continue
        label = f"`{' '.join(simple.tokens)}`"
        if kind in ("locate", "mdfind_global"):
            return _deny(label)
        eff_cwd = Path(simple.cwd) if simple.cwd else cwd
        is_rec = kind == "recursive"
        targets = _extract_path_candidates(simple.tokens[1:], eff_cwd) or ([eff_cwd] if is_rec else [])
        for target in targets:
            decision = _check_target(
                target, is_recursive=is_rec, repo_root=repo_root, policy=policy, label=label
            )
            if decision.decision == "deny":
                return decision
    return ScopeDecision("allow")


def _strip_glob_prefix(raw: str) -> str:
    match = GLOB_META_RE.search(raw)
    if not match:
        return raw
    prefix = raw[: match.start()]
    sep = max(prefix.rfind("/"), prefix.rfind("\\"))
    return prefix[: sep + 1] if sep != -1 else "."


def evaluate_scope(
    tool_name: str, tool_input: Mapping[str, Any], cwd: Path, repo_root: Path, policy: ScopePolicy
) -> ScopeDecision:
    if policy.mode == "off":
        return ScopeDecision("allow")
    lowered = tool_name.strip().lower()
    if lowered in SHELL_TOOL_NAMES:
        cmd = tool_input.get("command") or tool_input.get("CommandLine") or ""
        raw_cwd = tool_input.get("cwd") or tool_input.get("Cwd")
        eff_cwd = (
            _to_path(_expand_vars(raw_cwd), cwd) if isinstance(raw_cwd, str) and raw_cwd.strip() else cwd
        )
        return _evaluate_shell(cmd if isinstance(cmd, str) else "", eff_cwd, repo_root, policy)
    if lowered in READ_TOOL_NAMES:
        for key in ("file_path", "filePath", "path", "TargetFile", "AbsolutePath", "notebook_path"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                target = _to_path(_expand_vars(val.strip()), cwd)
                return _check_target(
                    target,
                    is_recursive=False,
                    repo_root=repo_root,
                    policy=policy,
                    label=f"{tool_name}({val!r})",
                )
        return ScopeDecision("allow")
    if lowered in SEARCH_TOOL_NAMES:
        keys = (
            ("path", "pattern", "folder", "DirectoryPath", "SearchDirectory")
            if lowered == "glob"
            else ("path", "glob", "includePattern", "folder", "DirectoryPath", "SearchDirectory")
        )
        for key in keys:
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                target = _to_path(_strip_glob_prefix(_expand_vars(val.strip())), cwd)
                dec = _check_target(
                    target,
                    is_recursive=True,
                    repo_root=repo_root,
                    policy=policy,
                    label=f"{tool_name}({val!r})",
                )
                if dec.decision == "deny":
                    return dec
    return ScopeDecision("allow")
