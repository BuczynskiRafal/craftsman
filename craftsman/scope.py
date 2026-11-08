"""Repository search-scope enforcement (`craftsman/scope.py`).

Models scope checks as candidate paths against forbidden zones:
1. Extract candidate paths from shell commands (any non-flag argument that looks like a path or
   exists on disk after `~` and environment-variable expansion) or structured IDE tool inputs.
2. Allow paths that resolve inside the active git work tree, its own `<git-dir>/craftsman`
   directory, or `craftsman.scopeAllow` (excluding other worktrees' git directories).
3. Outside those roots, deny recursive searches and deny non-recursive reads targeting session
   stores, shell histories, other git repositories/worktrees, or symlinks escaping the work tree.

Antigravity enters through `evaluate_antigravity_payload`: the shared command policy first, then this
scope check for the callers it binds (a hunter or verifier subagent, the owner of an active gate, a
subagent of the review-marker holder), then arming the review guard for the main conversation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, get_args

from craftsman import hook_errors
from craftsman.agy_sync import main_conversation
from craftsman.antigravity import app_dir, session_id, sync_marker_from_user_input
from craftsman.command_policy import PolicyContext, decide, default_transcript_roots, tool_call
from craftsman.diffscan import DiffScanError, git_time_left, note_git_timeout, worktree_root
from craftsman.gate import GateOwner, GateStateError, load_state, owns_active_gate, state_path_for
from craftsman.ledger import ledger_path_for
from craftsman.paths import user_home
from craftsman.records import CRAFTSMAN_AGENTS, AgentRole, LedgerError, craftsman_agent_name
from craftsman.review_guard import arm_for_review_command, review_in_progress
from craftsman.shell import HEREDOC_OPEN_RE, iter_simple_commands, split_shell_events
from craftsman.streams import configure_std_streams
from craftsman.tool_names import READ_PATH_KEYS, READ_TOOL_NAMES, SEARCH_TOOL_NAMES, SHELL_TOOL_NAMES
from craftsman.transcript import TranscriptError, decode_arg, parent_conversation, subagent_role
from craftsman.workspace import read_marker

ScopeMode = Literal["deny", "warn", "off"]
SCOPE_CHECKED_TOOLS = SHELL_TOOL_NAMES | READ_TOOL_NAMES | SEARCH_TOOL_NAMES
ALWAYS_RECURSIVE_CMDS = frozenset({"rg", "ripgrep", "ag", "ack", "fd", "fdfind", "find", "tree", "du"})
READ_CMDS = frozenset({"cat", "head", "tail", "less", "more", "sed", "awk", "gawk", "bat"})
FORBIDDEN_HOME_RELPATHS = (
    ".copilot/session-state",
    ".claude/projects",
    ".claude/sessions",
    ".gemini/antigravity/brain",
    ".gemini/antigravity/conversations",
    ".gemini/antigravity-cli/brain",
    ".gemini/antigravity-cli/conversations",
    ".gemini/antigravity-ide/brain",
    ".gemini/antigravity-ide/conversations",
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
GIT_CONFIG_TIMEOUT_S = 5.0
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
    """Whether the agent is one of Craftsman's own agents (see `craftsman_agent_name`)."""
    return craftsman_agent_name(agent) in CRAFTSMAN_AGENTS


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
    timeout = git_time_left(GIT_CONFIG_TIMEOUT_S)
    if timeout <= 0:
        note_git_timeout(f"`git config` was not run in {cwd}: the hook's git budget is spent")
        return []
    try:
        proc = subprocess.run(
            ["git", "config", *args],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",  # git config files are UTF-8; the locale may not be
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        note_git_timeout(f"`git config` timed out after {timeout:g}s in {cwd}; the search scope policy was not read")
        return []
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


def _is_forbidden_store(candidate: Path, protected: Sequence[Path]) -> bool:
    """Whether `candidate` lies in a session store or shell history: a home path of `FORBIDDEN_HOME_RELPATHS`, or
    a transcript root of `protected` (the roots the command policy protects; they follow the environment)."""
    home = user_home()
    resolved_cand = candidate.resolve()
    stores = [base / rel for base in {home, home.resolve()} for rel in FORBIDDEN_HOME_RELPATHS]
    for store in (*stores, *protected):
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
    candidate: Path, *, is_recursive: bool, repo_root: Path, policy: ScopePolicy, label: str, protected: Sequence[Path]
) -> ScopeDecision:
    if _is_forbidden_store(candidate, protected):
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
    for seg in split_shell_events(command_line):
        if seg in ("(", ")"):
            continue
        first_line = seg.split("\n", 1)[0]
        hd_match = HEREDOC_OPEN_RE.search(first_line)
        try:
            shlex.split(first_line[: hd_match.start()] if hd_match else seg, posix=True)
        except ValueError:
            return True
    return False


def _evaluate_shell(
    command_line: str, cwd: Path, repo_root: Path, policy: ScopePolicy, protected: Sequence[Path]
) -> ScopeDecision:
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
                target, is_recursive=is_rec, repo_root=repo_root, policy=policy, label=label, protected=protected
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
    tool_name: str,
    tool_input: Mapping[str, Any],
    cwd: Path,
    repo_root: Path,
    policy: ScopePolicy,
    protected: Sequence[Path] | None = None,
) -> ScopeDecision:
    """The search-scope verdict on one tool call. `protected` are the transcript roots no read may touch (default:
    the roots of the process environment, `command_policy.default_transcript_roots`)."""
    if policy.mode == "off":
        return ScopeDecision("allow")
    if protected is None:
        protected = default_transcript_roots()
    lowered = tool_name.strip().lower()
    if lowered in SHELL_TOOL_NAMES:
        cmd = tool_input.get("command") or tool_input.get("CommandLine") or ""
        raw_cwd = tool_input.get("cwd") or tool_input.get("Cwd")
        eff_cwd = (
            _to_path(_expand_vars(raw_cwd), cwd) if isinstance(raw_cwd, str) and raw_cwd.strip() else cwd
        )
        return _evaluate_shell(cmd if isinstance(cmd, str) else "", eff_cwd, repo_root, policy, protected)
    if lowered in READ_TOOL_NAMES:
        for key in READ_PATH_KEYS:
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                target = _to_path(_expand_vars(val.strip()), cwd)
                return _check_target(
                    target,
                    is_recursive=False,
                    repo_root=repo_root,
                    policy=policy,
                    label=f"{tool_name}({val!r})",
                    protected=protected,
                )
        return ScopeDecision("allow")
    if lowered in SEARCH_TOOL_NAMES:
        keys = (
            ("path", "pattern", "folder", "DirectoryPath", "SearchDirectory", "SearchPath")
            if lowered == "glob"
            else ("path", "glob", "includePattern", "folder", "DirectoryPath", "SearchDirectory", "SearchPath")
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
                    protected=protected,
                )
                if dec.decision == "deny":
                    return dec
    return ScopeDecision("allow")


# --- the Antigravity PreToolUse hook ---------------------------------------------------------------------

HOOK_EVENT = "antigravity-pre-tool"


@dataclass(frozen=True)
class AntigravityCaller:
    """Who makes an Antigravity tool call: its conversation, and the main conversation it belongs to.

    A subagent conversation has a conversation id of its own; the gate, the review marker and the ledger belong
    to the MAIN conversation, so everything that asks "whose is this" is asked of `main_id`.
    """

    conversation: str | None
    main_id: str | None
    role: AgentRole | None  # the Craftsman role of a subagent; None for the main conversation and other subagents
    is_main: bool

    @property
    def owner(self) -> GateOwner:
        return GateOwner("antigravity", self.main_id)


def antigravity_caller(payload: Mapping[str, Any], env: Mapping[str, str]) -> AntigravityCaller:
    """The caller of a PreToolUse payload, resolved through the transcripts (see `AntigravityCaller`)."""
    conversation = session_id(payload)
    if conversation is None:
        return AntigravityCaller(None, None, None, True)
    app = app_dir(payload, env)
    given = payload.get("transcriptPath")
    transcript = Path(given).expanduser() if isinstance(given, str) and given.strip() else None
    parent = parent_conversation(app, conversation, path=transcript)
    if parent is None:
        return AntigravityCaller(conversation, conversation, None, True)
    return AntigravityCaller(
        conversation, main_conversation(app, conversation, transcript), subagent_role(app, conversation, parent), False
    )


@dataclass
class _Checkout:
    """The work tree of a tool call and what the hook asks of it, each read once and only when a rule needs it."""

    directory: Path
    _root: Path | None = None

    @property
    def root(self) -> Path:
        """Raises `DiffScanError` outside a git work tree."""
        if self._root is None:
            self._root = worktree_root(self.directory)
        return self._root

    def in_work_tree(self) -> bool:
        try:
            return self.root is not None
        except DiffScanError:
            return False

    @property
    def ledger_path(self) -> Path:
        return ledger_path_for(self.root)

    @property
    def state_path(self) -> Path:
        return state_path_for(self.root)


def _antigravity_directory(args: Mapping[str, Any], payload: Mapping[str, Any]) -> Path:
    """Where the call runs: its `Cwd`, else the first workspace of the payload (file tools carry no `Cwd`), else the process cwd."""
    cwd = args.get("Cwd")
    if isinstance(cwd, str) and cwd.strip() and os.path.isdir(cwd):
        return Path(cwd)
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], str) and os.path.isdir(workspaces[0]):
        return Path(workspaces[0])
    return Path.cwd()


def _protected_roots(env: Mapping[str, str], app: Path) -> tuple[Path, ...]:
    """The transcript roots of a call: the platforms' (`default_transcript_roots` of `env`) and `<app data dir>/brain`.
    The command policy and the search scope both protect exactly these."""
    return tuple(dict.fromkeys((*default_transcript_roots(env), app / "brain")))


def _policy_context(caller: AntigravityCaller, checkout: _Checkout, app: Path, env: Mapping[str, str]) -> PolicyContext:
    def ledger_dir() -> Path | None:
        try:
            return checkout.ledger_path.parent
        except (LedgerError, DiffScanError):
            return None

    def owns_gate() -> bool:
        try:
            return owns_active_gate(load_state(checkout.state_path), caller.owner)
        except (DiffScanError, GateStateError):
            return False

    def in_progress() -> int | None:
        try:
            return review_in_progress(checkout.ledger_path, checkout.state_path)
        except (LedgerError, DiffScanError):
            return None

    return PolicyContext(ledger_dir, owns_gate, in_progress, _protected_roots(env, app))


def _scope_applies(caller: AntigravityCaller, checkout: _Checkout) -> bool:
    """Whether the search scope binds this call: the conversation owns the active gate (a subagent counts as its
    parent), is a Craftsman subagent, or is a subagent of the conversation whose review the marker guards."""
    if caller.role is not None:
        return True
    if caller.main_id is None:
        return False
    if owns_active_gate(load_state(checkout.state_path), caller.owner):
        return True
    marker = read_marker(checkout.ledger_path)
    return not caller.is_main and marker is not None and marker.guarding and marker.is_of(caller.owner)


def _scope_decision(
    name: str, args: Mapping[str, Any], base: Path, checkout: _Checkout, protected: Sequence[Path]
) -> tuple[str, str | None]:
    """The search-scope verdict on one call whose caller the scope binds."""
    try:
        policy = load_scope_policy(base, checkout.root)
    except ScopeConfigError as exc:
        return "force_ask", f"[CRAFTSMAN SCOPE-GUARD] {exc}"
    if policy.mode == "off":
        return "allow", None
    decision = evaluate_scope(name, args, base, checkout.root, policy, protected)
    if decision.decision == "deny" and policy.mode == "deny":
        return "deny", decision.reason
    if decision.decision == "deny":
        sys.stderr.write(decision.reason + "\n")  # warn mode: the call goes on
    return "allow", None


def _decoded_args(raw: object) -> Mapping[str, Any]:
    """The arguments of a tool call with every text value read the way the transcript spells it (`decode_arg`):
    the platform may hand over JSON text (`"\\"ls\\""`) as `transcript.jsonl` does, and a command that stays quoted
    would parse as one word and slip past every shell rule."""
    if not isinstance(raw, Mapping):
        return {}
    return {key: decode_arg(value) if isinstance(value, str) else value for key, value in raw.items()}


def evaluate_antigravity_payload(
    payload: Mapping[str, Any], env: Mapping[str, str] | None = None, now: datetime | None = None
) -> tuple[str, str | None]:
    """The answer to an Antigravity `PreToolUse` payload: `(decision, reason)`, decision `allow`, `deny` or `force_ask`.

    The shared command policy (`command_policy.decide`) judges every tool the hook is wired to, exactly as the
    Claude and Copilot hooks do; then the search scope applies to the calls it binds (`_scope_applies`); then a
    `craftsman review` command of the MAIN conversation arms or touches the review guard, after the
    conversation's own `/craft-review` request was read from its transcript (`sync_marker_from_user_input`).
    """
    values = os.environ if env is None else env
    raw = payload.get("toolCall")
    if not isinstance(raw, Mapping):
        return "allow", None
    name, raw_args = raw.get("name"), raw.get("args")
    if not isinstance(name, str) or not name.strip():
        return "allow", None
    args = _decoded_args(raw_args)
    caller = antigravity_caller(payload, values)
    base = _antigravity_directory({}, payload)
    checkout = _Checkout(_antigravity_directory(args, payload))
    app = app_dir(payload, values)
    call = tool_call(name, args, str(base), role=caller.role, main_session=caller.is_main, session_id=caller.conversation)
    decision = decide(call, _policy_context(caller, checkout, app, values))
    if decision.action != "allow":
        return ("deny" if decision.action == "deny" else "force_ask"), decision.reason
    if not checkout.in_work_tree():  # outside a git work tree there is no gate, no scope and no review
        return "allow", None
    try:
        if is_scope_checked_tool(name) and _scope_applies(caller, checkout):
            verdict = _scope_decision(
                name, args, Path(call.cwd) if os.path.isdir(call.cwd) else base, checkout, _protected_roots(values, app)
            )
            if verdict[0] != "allow":
                return verdict
        if caller.is_main and caller.main_id is not None and decision.review:
            _arm_for_review(caller, checkout, app, payload, decision.review, now or datetime.now(timezone.utc))
    except GateStateError as exc:
        return "force_ask", f"The Craftsman gate state of this work tree is invalid: {exc}"
    return "allow", None


def _arm_for_review(
    caller: AntigravityCaller,
    checkout: _Checkout,
    app: Path,
    payload: Mapping[str, Any],
    review: tuple[str, ...],
    now: datetime,
) -> None:
    """The main conversation runs a `craftsman review` command: read its `/craft-review` request first (so `prepare` in
    the same turn sees the request's depth and task), then arm or touch the guard. Never blocks the call."""
    assert caller.conversation is not None
    given = payload.get("transcriptPath")
    transcript = Path(given).expanduser() if isinstance(given, str) and given.strip() else None
    try:
        sync_marker_from_user_input(
            checkout.root, app, caller.conversation, caller.owner, now, transcript=transcript
        )
    except (TranscriptError, OSError, ValueError) as exc:
        sys.stderr.write(f"craftsman scope-guard: could not read the review request: {exc}\n")
    arm_for_review_command(checkout.ledger_path, checkout.state_path, caller.owner, review, now)


def _emit(decision: str, reason: str | None = None) -> None:
    payload: dict[str, Any] = {"decision": decision}
    if reason:
        payload["reason"] = reason
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    configure_std_streams()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ("-h", "--help"):
        print(
            "Usage: craftsman scope-guard\n\n"
            "Reads an Antigravity PreToolUse JSON payload from stdin and enforces the search scope."
        )
        return 0
    if args_list:
        sys.stderr.write(f"craftsman scope-guard: unknown arguments {' '.join(args_list)}\n")
        return 2

    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, ValueError) as exc:
        _emit(
            "deny",
            f"[CRAFTSMAN SCOPE-GUARD] Unreadable hook input, so the call was not checked: {exc}",
        )
        return 0
    if not isinstance(payload, dict):
        _emit(
            "deny",
            "[CRAFTSMAN SCOPE-GUARD] The hook input is not a JSON object, so the call was not checked.",
        )
        return 0

    args = payload.get("toolCall", {}).get("args") if isinstance(payload.get("toolCall"), dict) else None
    directory = _antigravity_directory(args if isinstance(args, dict) else {}, payload)
    with hook_errors.hook_run(HOOK_EVENT, directory) as log:
        try:
            decision, reason = evaluate_antigravity_payload(payload)
        except Exception as exc:  # noqa: BLE001 - a crashed hook counts as "allow": ask the user instead
            problem = f"{type(exc).__name__}: {exc}"
            kind = hook_errors.kind_of(exc)
            if kind == "git-timeout":
                log.git_timeout(problem)
            else:
                log.record(kind, problem)
            decision, reason = "force_ask", f"[CRAFTSMAN SCOPE-GUARD] The call was not checked ({problem})."
    _emit(decision, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
