"""Repository search-scope enforcement for Craftsman PreToolUse lifecycle hooks."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from craftsman.guard import iter_simple_commands

ScopeMode = Literal["deny", "warn", "off"]
DecisionKind = Literal["allow", "deny"]

CRAFTSMAN_AGENT_NAMES = frozenset(
    {
        "craftsman",
        "craftsman-reviewer",
        "craftsman-verifier",
    }
)

SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "powershell",
        "pwsh",
        "shell",
        "terminal",
        "run_in_terminal",
        "runinterminal",
        "run_command",
    }
)

READ_LIST_TOOL_NAMES = frozenset(
    {
        "read",
        "read_file",
        "readfile",
        "view",
        "view_file",
        "ls",
        "list_dir",
        "listdir",
        "get_file_contents",
        "open_file",
    }
)

SEARCH_TOOL_NAMES = frozenset(
    {
        "grep",
        "glob",
        "grep_search",
        "grepsearch",
        "file_search",
        "filesearch",
        "find_by_name",
        "ripgrep",
        "rg",
        "semantic_search",
        "codebase_search",
        "search_files",
    }
)

ALWAYS_RECURSIVE_SEARCH_BINS = frozenset({"rg", "ripgrep", "ag", "ack", "fd", "fdfind", "tree", "du"})
GREP_BINS = frozenset({"grep", "egrep", "fgrep", "ugrep"})
LS_BINS = frozenset({"ls", "dir", "gci", "get-childitem"})
LOCATE_BINS = frozenset({"locate", "mlocate", "plocate"})
SHELL_READ_BINS = frozenset(
    {"cat", "head", "tail", "less", "more", "sed", "awk", "gawk", "get-content", "gc", "type"}
)

RG_OPTS_WITH_VALUE = frozenset(
    {
        "-e",
        "--regexp",
        "-f",
        "--file",
        "-g",
        "--glob",
        "--iglob",
        "-t",
        "--type",
        "-T",
        "--type-not",
        "--type-add",
        "--type-clear",
        "-m",
        "--max-count",
        "-A",
        "--after-context",
        "-B",
        "--before-context",
        "-C",
        "--context",
        "--context-separator",
        "--field-context-separator",
        "--field-match-separator",
        "--color",
        "--colors",
        "--encoding",
        "-E",
        "-M",
        "--max-columns",
        "--max-filesize",
        "--maxdepth",
        "--max-depth",
        "-j",
        "--threads",
        "--path-separator",
        "--pre",
        "--pre-glob",
        "-r",
        "--replace",
        "--sort",
        "--sortr",
        "--dfa-size-limit",
        "--regex-size-limit",
        "--ignore-file",
    }
)

FD_OPTS_WITH_VALUE = frozenset(
    {
        "-d",
        "--max-depth",
        "--min-depth",
        "--exact-depth",
        "-t",
        "--type",
        "-e",
        "--extension",
        "-E",
        "--exclude",
        "-c",
        "--color",
        "-j",
        "--threads",
        "-S",
        "--size",
        "--changed-within",
        "--changed-before",
        "-o",
        "--owner",
        "--base-directory",
        "--path-separator",
        "--search-path",
    }
)

GREP_OPTS_WITH_VALUE = frozenset(
    {
        "-e",
        "--regexp",
        "-f",
        "--file",
        "-m",
        "--max-count",
        "-A",
        "--after-context",
        "-B",
        "--before-context",
        "-C",
        "--context",
        "--color",
        "--colour",
        "--devices",
        "-D",
        "--directories",
        "-d",
        "--exclude",
        "--exclude-from",
        "--exclude-dir",
        "--include",
        "--label",
    }
)

WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:[/\\]|$)")
GLOB_META_RE = re.compile(r"[*?\[\]{}]")


@dataclass(frozen=True)
class ScopePolicy:
    """Repository search-scope configuration loaded from git config."""

    mode: ScopeMode = "deny"
    allowed_dirs: tuple[Path, ...] = ()
    git_common_dir: Path | None = None


@dataclass(frozen=True)
class ScopeDecision:
    """Result of evaluating a tool call against the repository search scope."""

    decision: DecisionKind
    reason: str = ""


def is_craftsman_agent(agent_name: str | None) -> bool:
    if not agent_name:
        return False
    normalized = agent_name.strip().lower()
    if normalized in CRAFTSMAN_AGENT_NAMES:
        return True
    return any(normalized.endswith(f"/{name}") or normalized.endswith(f":{name}") for name in CRAFTSMAN_AGENT_NAMES)


def _git_config_values(cwd: Path, *args: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "config", *args],
            cwd=str(cwd) if cwd.is_dir() else None,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def _resolve_git_common_dir(repo_root: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo_root) if repo_root.is_dir() else None,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = Path(proc.stdout.strip())
            return (raw if raw.is_absolute() else (repo_root / raw)).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _current_home() -> Path:
    raw = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if raw:
        try:
            return Path(raw).resolve()
        except OSError:
            return Path(raw)
    return Path.home().resolve()


def expand_env_and_tilde(raw: str, home: Path | None = None) -> str:
    eff_home = str(home or _current_home())
    expanded = re.sub(
        r"\$(?:HOME|\{HOME\}|env:USERPROFILE|env:HOME)(?=[/\\]|$)|%USERPROFILE%",
        lambda _: eff_home,
        raw,
        flags=re.IGNORECASE,
    )
    if expanded == "~" or expanded.startswith(("~/", "~\\")):
        return eff_home + expanded[1:]
    return os.path.expanduser(expanded)


def load_scope_policy(cwd: Path, repo_root: Path) -> ScopePolicy:
    """Load `craftsman.scope` and `craftsman.scopeAllow` from repository and global git config."""
    resolved_root = repo_root.resolve()
    mode_vals = _git_config_values(resolved_root, "--get", "craftsman.scope")
    raw_mode = mode_vals[-1].lower() if mode_vals else "deny"
    mode: ScopeMode = raw_mode if raw_mode in ("deny", "warn", "off") else "deny"  # type: ignore[assignment]

    home = _current_home()
    allow_vals = _git_config_values(resolved_root, "--get-all", "craftsman.scopeAllow")
    allowed: list[Path] = []
    for val in allow_vals:
        expanded = expand_env_and_tilde(val, home)
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        try:
            allowed.append(candidate.resolve())
        except OSError:
            allowed.append(Path(os.path.normpath(str(candidate))))

    return ScopePolicy(
        mode=mode,
        allowed_dirs=tuple(allowed),
        git_common_dir=_resolve_git_common_dir(resolved_root),
    )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_windows_drive_outside_posix_repo(raw: str, repo_root: Path) -> bool:
    return bool(WINDOWS_DRIVE_RE.match(raw) and str(repo_root).startswith("/"))


def _resolve_lexical_and_real(raw: str, base_cwd: Path, home: Path) -> tuple[Path, Path]:
    expanded = expand_env_and_tilde(raw, home)
    p = Path(expanded)
    lexical = p if p.is_absolute() else (base_cwd / p)
    lexical_norm = Path(os.path.normpath(str(lexical)))
    try:
        real = lexical.resolve()
    except OSError:
        real = lexical_norm
    return lexical_norm, real


def _is_in_policy_allowlist(target: Path, policy: ScopePolicy) -> bool:
    return any(_is_within(target, allowed) for allowed in policy.allowed_dirs)


def _is_in_own_git_craftsman(target: Path, repo_root: Path, policy: ScopePolicy) -> bool:
    if _is_within(target, repo_root / ".git" / "craftsman") or _is_within(
        target, repo_root / ".craftsman"
    ):
        return True
    if policy.git_common_dir is not None and _is_within(
        target, policy.git_common_dir / "craftsman"
    ):
        return True
    return False


def _is_assistant_session_store(target: Path, home: Path) -> bool:
    stores = (
        home / ".copilot" / "session-state",
        home / ".claude" / "projects",
        home / ".gemini" / "antigravity" / "brain",
        home / ".gemini" / "antigravity" / "conversations",
    )
    if any(_is_within(target, store) for store in stores):
        return True
    parts = target.parts
    for i in range(len(parts) - 1):
        if parts[i] == ".copilot" and parts[i + 1] == "session-state":
            return True
        if parts[i] == ".claude" and parts[i + 1] == "projects":
            return True
    for i in range(len(parts) - 2):
        if parts[i] == ".gemini" and parts[i + 1] == "antigravity" and parts[i + 2] in (
            "brain",
            "conversations",
        ):
            return True
    return False


def _is_other_craftsman_data(target: Path, repo_root: Path, policy: ScopePolicy) -> bool:
    if _is_in_own_git_craftsman(target, repo_root, policy):
        return False
    parts = target.parts
    for i in range(len(parts) - 1):
        if parts[i] in (".git", ".craftsman") and parts[i + 1] == "craftsman":
            return True
    if target.name in ("ledger.json", "craftsman_state.json") and target.parent.name == "craftsman":
        return True
    if re.match(r"^packet-\d+\.md$", target.name) and target.parent.name == "craftsman":
        return True
    return False


def _nearest_git_worktree(target: Path) -> Path | None:
    cur = target if target.is_dir() else target.parent
    for candidate in (cur, *cur.parents):
        git_marker = candidate / ".git"
        try:
            if git_marker.exists():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _deny_reason(action_desc: str, target_desc: str, repo_root: Path) -> str:
    return (
        f"[CRAFTSMAN SCOPE-GUARD] Blocked {action_desc} targeting {target_desc} outside the "
        f"repository work tree ({repo_root}). Search inside {repo_root} (git grep, git ls-files) "
        "or ask the user for the exact path. Allow a directory with "
        "`git config craftsman.scopeAllow <dir>`."
    )


def _check_read_or_list_path(
    raw_path: str,
    base_cwd: Path,
    repo_root: Path,
    policy: ScopePolicy,
    home: Path,
    action_desc: str,
) -> ScopeDecision:
    if _is_windows_drive_outside_posix_repo(raw_path, repo_root):
        return ScopeDecision("deny", _deny_reason(action_desc, repr(raw_path), repo_root))

    lexical, real = _resolve_lexical_and_real(raw_path, base_cwd, home)
    if _is_in_policy_allowlist(real, policy):
        return ScopeDecision("allow")

    # Symlink inside repo_root pointing outside repo_root
    if (
        _is_within(lexical, repo_root)
        and not _is_within(real, repo_root)
        and not _is_in_own_git_craftsman(real, repo_root, policy)
    ):
        return ScopeDecision(
            "deny",
            _deny_reason(f"{action_desc} (symlink escaping repository)", str(real), repo_root),
        )

    if _is_within(real, repo_root) or _is_in_own_git_craftsman(real, repo_root, policy):
        return ScopeDecision("allow")

    if _is_assistant_session_store(real, home) or _is_assistant_session_store(lexical, home):
        return ScopeDecision(
            "deny",
            _deny_reason(f"{action_desc} (assistant session store)", str(real), repo_root),
        )

    if _is_other_craftsman_data(real, repo_root, policy) or _is_other_craftsman_data(
        lexical, repo_root, policy
    ):
        return ScopeDecision(
            "deny",
            _deny_reason(f"{action_desc} (another repository's Craftsman data)", str(real), repo_root),
        )

    git_wt = _nearest_git_worktree(real)
    if git_wt is not None and git_wt != repo_root:
        return ScopeDecision(
            "deny",
            _deny_reason(f"{action_desc} (another git repository {git_wt})", str(real), repo_root),
        )

    return ScopeDecision("allow")


def _is_allowed_recursive_root(
    raw_path: str,
    base_cwd: Path,
    repo_root: Path,
    policy: ScopePolicy,
    home: Path,
) -> tuple[bool, str]:
    if _is_windows_drive_outside_posix_repo(raw_path, repo_root):
        return False, raw_path

    lexical, real = _resolve_lexical_and_real(raw_path, base_cwd, home)
    if _is_in_policy_allowlist(real, policy):
        return True, str(real)

    # Symlink inside repo_root pointing outside repo_root
    if _is_within(lexical, repo_root) and not _is_within(real, repo_root):
        return False, f"{lexical} -> {real}"

    # Root, home, or any ancestor of repo_root is never allowed
    if real == Path(real.anchor) or real == home or real in repo_root.parents:
        return False, str(real)

    if _is_assistant_session_store(real, home) or _is_other_craftsman_data(real, repo_root, policy):
        return False, str(real)

    git_wt = _nearest_git_worktree(real)
    if git_wt is not None and git_wt != repo_root:
        return False, str(real)

    if _is_within(real, repo_root) or _is_in_own_git_craftsman(real, repo_root, policy):
        return True, str(real)

    # Dedicated subdirectory in system temp is allowed only if it is not a sibling of repo_root
    # when repo_root itself lives inside a test TemporaryDirectory.
    temp_roots: list[Path] = []
    for raw_tmp in (tempfile.gettempdir(), "/tmp", "/private/tmp"):
        try:
            temp_roots.append(Path(raw_tmp).resolve())
        except OSError:
            continue
    for tmp_root in temp_roots:
        if real != tmp_root and _is_within(real, tmp_root):
            if _is_within(repo_root, tmp_root) and _is_within(real, repo_root.parent):
                # Inside the same test parent directory as repo_root (e.g. ../other-repo)
                return False, str(real)
            return True, str(real)

    return False, str(real)


def _extract_find_roots(args: list[str]) -> list[str]:
    roots: list[str] = []
    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok in ("-H", "-L", "-P", "-E", "-X", "-s", "-x"):
            idx += 1
            continue
        if tok in ("-D", "-O") and idx + 1 < n:
            idx += 2
            continue
        if tok == "-f" and idx + 1 < n:
            roots.append(args[idx + 1])
            idx += 2
            continue
        break
    while idx < n:
        tok = args[idx]
        if tok.startswith("-") or tok in ("(", ")", "!", ","):
            break
        roots.append(tok)
        idx += 1
    return roots or ["."]


def _extract_rg_roots(args: list[str]) -> list[str]:
    has_explicit_pattern = False
    files_mode = False
    positionals: list[str] = []
    idx = 0
    n = len(args)
    after_double_dash = False
    while idx < n:
        tok = args[idx]
        if after_double_dash:
            positionals.append(tok)
            idx += 1
            continue
        if tok == "--":
            after_double_dash = True
            idx += 1
            continue
        if tok == "--files":
            files_mode = True
            idx += 1
            continue
        if tok in ("-e", "--regexp", "-f", "--file") or tok.startswith(
            ("--regexp=", "--file=")
        ):
            has_explicit_pattern = True
            if "=" not in tok and idx + 1 < n:
                idx += 2
            else:
                idx += 1
            continue
        if tok in RG_OPTS_WITH_VALUE:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        positionals.append(tok)
        idx += 1

    if files_mode or has_explicit_pattern:
        return positionals or ["."]
    return positionals[1:] if len(positionals) > 1 else ["."]


def _extract_fd_roots(args: list[str]) -> list[str]:
    explicit_roots: list[str] = []
    positionals: list[str] = []
    idx = 0
    n = len(args)
    after_double_dash = False
    while idx < n:
        tok = args[idx]
        if tok in ("-x", "--exec", "-X", "--exec-batch"):
            break
        if after_double_dash:
            positionals.append(tok)
            idx += 1
            continue
        if tok == "--":
            after_double_dash = True
            idx += 1
            continue
        if tok in ("--search-path", "--base-directory") and idx + 1 < n:
            explicit_roots.append(args[idx + 1])
            idx += 2
            continue
        if tok.startswith(("--search-path=", "--base-directory=")):
            explicit_roots.append(tok.split("=", 1)[1])
            idx += 1
            continue
        if tok in FD_OPTS_WITH_VALUE:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        positionals.append(tok)
        idx += 1

    if explicit_roots:
        return explicit_roots
    return positionals[1:] if len(positionals) > 1 else ["."]


def _parse_grep_invocation(args: list[str]) -> tuple[bool, list[str]]:
    recursive = False
    has_explicit_pattern = False
    positionals: list[str] = []
    idx = 0
    n = len(args)
    after_double_dash = False
    while idx < n:
        tok = args[idx]
        if after_double_dash:
            positionals.append(tok)
            idx += 1
            continue
        if tok == "--":
            after_double_dash = True
            idx += 1
            continue
        if tok in ("-r", "-R", "--recursive", "--directories=recurse"):
            recursive = True
            idx += 1
            continue
        if tok in ("-d", "--directories") and idx + 1 < n:
            if args[idx + 1] == "recurse":
                recursive = True
            idx += 2
            continue
        if tok in ("-e", "--regexp", "-f", "--file") or tok.startswith(
            ("--regexp=", "--file=")
        ):
            has_explicit_pattern = True
            if "=" not in tok and idx + 1 < n:
                idx += 2
            else:
                idx += 1
            continue
        if tok in GREP_OPTS_WITH_VALUE:
            idx += 2
            continue
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            flags = tok[1:]
            if "r" in flags or "R" in flags:
                recursive = True
            if "e" in flags or "f" in flags:
                has_explicit_pattern = True
                if flags.endswith(("e", "f")) and idx + 1 < n:
                    idx += 2
                    continue
            idx += 1
            continue
        if tok.startswith("--"):
            idx += 1
            continue
        positionals.append(tok)
        idx += 1

    roots = positionals if has_explicit_pattern else (positionals[1:] if len(positionals) > 1 else ["."])
    return recursive, (roots or ["."])


def _parse_ls_or_gci(exe: str, args: list[str]) -> tuple[bool, list[str]]:
    recursive = False
    roots: list[str] = []
    idx = 0
    n = len(args)
    is_ps = exe in ("get-childitem", "gci")
    is_dir = exe == "dir"
    while idx < n:
        tok = args[idx]
        low = tok.lower()
        if low in ("-recurse", "--recursive") or (is_ps and low == "-r") or (is_dir and low in ("/s", "-r")):
            recursive = True
            idx += 1
            continue
        if tok.startswith("-") and not tok.startswith("--") and not is_ps:
            if "R" in tok[1:]:
                recursive = True
            idx += 1
            continue
        if low in ("-path", "-literalpath") and idx + 1 < n:
            roots.append(args[idx + 1])
            idx += 2
            continue
        if low in ("-filter", "-include", "-exclude", "-depth") and idx + 1 < n:
            idx += 2
            continue
        if tok.startswith("-") or (is_dir and tok.startswith("/") and len(tok) <= 3):
            idx += 1
            continue
        roots.append(tok)
        idx += 1
    return recursive, (roots or ["."])


def _extract_tree_or_du_roots(args: list[str]) -> list[str]:
    roots: list[str] = []
    idx = 0
    n = len(args)
    while idx < n:
        tok = args[idx]
        if tok in ("-L", "-P", "-I", "-o", "-d", "-t", "--max-depth") and idx + 1 < n:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        roots.append(tok)
        idx += 1
    return roots or ["."]


def _extract_shell_read_paths(exe: str, args: list[str]) -> list[str]:
    paths: list[str] = []
    idx = 0
    n = len(args)
    if exe == "sed":
        has_script_flag = False
        non_opts: list[str] = []
        while idx < n:
            tok = args[idx]
            if tok in ("-e", "-f") and idx + 1 < n:
                has_script_flag = True
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            non_opts.append(tok)
            idx += 1
        return non_opts if has_script_flag else non_opts[1:]

    if exe in ("awk", "gawk"):
        has_prog_file = False
        non_opts_awk: list[str] = []
        while idx < n:
            tok = args[idx]
            if tok in ("-f", "-F", "-v") and idx + 1 < n:
                if tok == "-f":
                    has_prog_file = True
                idx += 2
                continue
            if tok.startswith("-"):
                idx += 1
                continue
            non_opts_awk.append(tok)
            idx += 1
        return non_opts_awk if has_prog_file else non_opts_awk[1:]

    while idx < n:
        tok = args[idx]
        low = tok.lower()
        if low in ("-n", "-c", "-lines", "-bytes", "-totalcount", "-tail") and idx + 1 < n:
            idx += 2
            continue
        if low in ("-path", "-literalpath") and idx + 1 < n:
            paths.append(args[idx + 1])
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        paths.append(tok)
        idx += 1
    return paths


def _has_unmatched_quotes(command_line: str) -> bool:
    # Ignore trailing backslash on Windows drive roots like C:\
    sanitized = re.sub(r"([A-Za-z]:)\\(?=\s|$)", r"\1/", command_line)
    try:
        shlex.split(sanitized, posix=True)
        return False
    except ValueError:
        return True


def _evaluate_shell_command_scope(
    command_line: str,
    cwd: Path,
    repo_root: Path,
    policy: ScopePolicy,
    home: Path,
) -> ScopeDecision:
    if not command_line.strip():
        return ScopeDecision("allow")

    if _has_unmatched_quotes(command_line):
        sys.stderr.write("craftsman scope warning: ignoring unparseable shell command\n")
        return ScopeDecision("allow")

    commands = iter_simple_commands(command_line, str(cwd))
    for cmd in commands:
        tokens = cmd.tokens
        if not tokens:
            continue
        exe = os.path.basename(tokens[0]).lower()
        args = tokens[1:]
        eff_cwd = Path(cmd.cwd) if cmd.cwd else cwd

        # Skip python -c / node -e inline scripts per specification
        if exe in ("python", "python3", "node", "ruby", "perl") and any(
            t in ("-c", "-e") for t in args
        ):
            continue

        # 1. System-wide index searches (locate / mlocate / mdfind)
        if exe in LOCATE_BINS:
            return ScopeDecision("deny", _deny_reason(f"`{exe}` search", "system index", repo_root))
        if exe == "mdfind":
            onlyin_roots: list[str] = []
            for i, tok in enumerate(args):
                if tok == "-onlyin" and i + 1 < len(args):
                    onlyin_roots.append(args[i + 1])
            if not onlyin_roots:
                return ScopeDecision("deny", _deny_reason("`mdfind` search", "system Spotlight index", repo_root))
            for r in onlyin_roots:
                ok, desc = _is_allowed_recursive_root(r, eff_cwd, repo_root, policy, home)
                if not ok:
                    return ScopeDecision("deny", _deny_reason("`mdfind` search", desc, repo_root))
            continue

        # 2. PowerShell Select-String
        if exe == "select-string":
            for i, tok in enumerate(args):
                if tok.lower() in ("-path", "-literalpath") and i + 1 < len(args):
                    raw_p = args[i + 1]
                    prefix = GLOB_META_RE.split(raw_p, maxsplit=1)[0].rstrip("/\\") or raw_p
                    ok, desc = _is_allowed_recursive_root(prefix, eff_cwd, repo_root, policy, home)
                    if not ok:
                        return ScopeDecision("deny", _deny_reason("`Select-String`", desc, repo_root))
            continue

        # 3. Recursive search / listing commands (D1)
        recursive_roots: list[str] | None = None
        if exe == "find":
            recursive_roots = _extract_find_roots(args)
        elif exe in ("rg", "ripgrep", "ag", "ack"):
            recursive_roots = _extract_rg_roots(args)
        elif exe in ("fd", "fdfind"):
            recursive_roots = _extract_fd_roots(args)
        elif exe in GREP_BINS:
            is_rec, g_roots = _parse_grep_invocation(args)
            if is_rec:
                recursive_roots = g_roots
        elif exe in LS_BINS:
            is_rec, l_roots = _parse_ls_or_gci(exe, args)
            if is_rec:
                recursive_roots = l_roots
        elif exe in ("tree", "du"):
            recursive_roots = _extract_tree_or_du_roots(args)

        if recursive_roots is not None:
            for r in recursive_roots:
                ok, desc = _is_allowed_recursive_root(r, eff_cwd, repo_root, policy, home)
                if not ok:
                    return ScopeDecision(
                        "deny",
                        _deny_reason(f"recursive `{exe}` search", desc, repo_root),
                    )
            continue

        # 4. File read commands (D3)
        if exe in SHELL_READ_BINS:
            for raw_p in _extract_shell_read_paths(exe, args):
                decision = _check_read_or_list_path(
                    raw_p, eff_cwd, repo_root, policy, home, f"`{exe}` read"
                )
                if decision.decision == "deny":
                    return decision

    return ScopeDecision("allow")


def _extract_glob_root_prefix(pattern: str) -> str | None:
    expanded = pattern.strip()
    if not expanded:
        return None
    if not (
        expanded.startswith(("/", "~", "$HOME", "${HOME}", "%USERPROFILE%", "$env:", ".."))
        or WINDOWS_DRIVE_RE.match(expanded)
    ):
        return None
    prefix = GLOB_META_RE.split(expanded, maxsplit=1)[0]
    cleaned = prefix.rstrip("/\\")
    if not cleaned:
        return "/" if expanded.startswith("/") else expanded[:3]
    return cleaned


def evaluate_scope(
    tool_name: str,
    tool_input: Mapping[str, Any],
    cwd: Path,
    repo_root: Path,
    policy: ScopePolicy,
) -> ScopeDecision:
    """Evaluate a PreToolUse invocation against the repository search scope."""
    if policy.mode == "off":
        return ScopeDecision("allow")

    resolved_repo = repo_root.resolve()
    try:
        resolved_cwd = cwd.resolve()
    except OSError:
        resolved_cwd = cwd
    home = _current_home()
    tool_low = tool_name.strip().lower()

    # Tool-level cwd override if provided in tool_input
    raw_tool_cwd = tool_input.get("cwd") or tool_input.get("Cwd")
    if isinstance(raw_tool_cwd, str) and raw_tool_cwd.strip():
        _, resolved_cwd = _resolve_lexical_and_real(raw_tool_cwd.strip(), resolved_cwd, home)

    # D1 & D3: Shell command tools
    if tool_low in SHELL_TOOL_NAMES:
        raw_cmd = tool_input.get("command") or tool_input.get("CommandLine")
        if not isinstance(raw_cmd, str):
            return ScopeDecision("allow")
        return _evaluate_shell_command_scope(raw_cmd, resolved_cwd, resolved_repo, policy, home)

    # D2a: Read / list directory tools
    if tool_low in READ_LIST_TOOL_NAMES:
        for key in (
            "file_path",
            "filePath",
            "path",
            "AbsolutePath",
            "TargetFile",
            "dirPath",
            "directory",
            "DirectoryPath",
        ):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                decision = _check_read_or_list_path(
                    val.strip(),
                    resolved_cwd,
                    resolved_repo,
                    policy,
                    home,
                    f"`{tool_name}`",
                )
                if decision.decision == "deny":
                    return decision
        return ScopeDecision("allow")

    # D2b: Search / Grep / Glob tools
    if tool_low in SEARCH_TOOL_NAMES:
        checked_any = False
        for key in ("path", "folder", "SearchPath", "SearchDirectory", "dirPath", "directory"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                checked_any = True
                ok, desc = _is_allowed_recursive_root(
                    val.strip(), resolved_cwd, resolved_repo, policy, home
                )
                if not ok:
                    return ScopeDecision("deny", _deny_reason(f"`{tool_name}` search", desc, resolved_repo))

        for pat_key in ("pattern", "glob", "includePattern", "query"):
            pat_val = tool_input.get(pat_key)
            if isinstance(pat_val, str) and pat_val.strip():
                prefix = _extract_glob_root_prefix(pat_val.strip())
                if prefix is not None:
                    checked_any = True
                    ok, desc = _is_allowed_recursive_root(
                        prefix, resolved_cwd, resolved_repo, policy, home
                    )
                    if not ok:
                        return ScopeDecision(
                            "deny",
                            _deny_reason(f"`{tool_name}` pattern", desc, resolved_repo),
                        )

        if not checked_any:
            ok, desc = _is_allowed_recursive_root(
                ".", resolved_cwd, resolved_repo, policy, home
            )
            if not ok:
                return ScopeDecision("deny", _deny_reason(f"`{tool_name}` search", desc, resolved_repo))

        return ScopeDecision("allow")

    return ScopeDecision("allow")
