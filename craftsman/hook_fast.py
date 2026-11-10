"""`craftsman hook`: answer a read-only PreToolUse call before the hooks load; hand the rest to them.

The platforms start a hook process for every tool call, and `craftsman.hooks` imports most of Craftsman
before it reads the payload. For a call that only reads, made outside an active gate by an agent that is
not Craftsman's, the full handler answers `{}` and changes nothing, so this module gives that answer with
the standard library alone. It answers only `hook pre-tool --platform claude|copilot`, and only when:

- stdin is a JSON object with a text `cwd`, and its tool input (`hooks._tool_input`) is absent or a JSON
  object;
- the tool is in the command policy's read-only set (`command_policy.READ_ONLY_TOOL_NAMES`), which the
  policy allows whatever the call names;
- no agent name field contains `craftsman` in any letter case: the search scope binds Craftsman's agents
  even without a gate;
- the home directory is known from `HOME` or `USERPROFILE`, and neither `CLAUDE_CONFIG_DIR` nor
  `ANTIGRAVITY_APP_DATA_DIR` starts with `~` (the policy builds the transcript roots from them);
- no environment variable changes how git finds the repository or passes configuration that may set a
  work tree;
- walking up from the real path of `cwd`, no directory is itself a git directory, every `.git` entry is a
  directory or a plain `gitdir: <path>` line that git reads as this module does, and every git directory
  they name has a `config` (and common `config`) that may set no work tree and includes nothing, and
  either no gate state or a valid inactive one (`gate.load_state` would read it without error).
  Whichever of them git settles on, its gate is inactive, so neither the scope guard nor the claim of a
  gate runs. The user's and the system's git configuration are not read: git takes `core.worktree` only
  from the repository's own (`tests/test_hook_fast.py` checks that with the git it runs).

Anything else, and any error, goes to `craftsman.hooks` with the input already read: the fast path never
denies, asks or writes. Nothing is imported here but `json` (and the `re` it loads), `os`, `sys` and the
tool names; `tests/test_hook_fast.py` holds the answer to the full handler's.
"""

from __future__ import annotations

import json
import os
import re  # `json` has imported it already
import sys

from craftsman.tool_names import READ_TOOL_NAMES, SEARCH_TOOL_NAMES

# `typing` is not imported: this module runs on every tool call, before anything else is loaded.
TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Mapping

# `command_policy.READ_ONLY_TOOL_NAMES`; a test keeps the two equal.
READ_ONLY_TOOL_NAMES = READ_TOOL_NAMES | SEARCH_TOOL_NAMES | frozenset(
    {
        "readfile", "readmcpresource", "listmcpresources", "semantic_search", "semanticsearch",
        "text_search", "textsearch", "codebase", "search", "get_errors", "list_code_usages",
        "web_search", "websearch", "webfetch", "fetch_webpage", "todowrite", "todoread",
    }
)  # fmt: skip
PLATFORMS = ("claude", "copilot")
ALLOW = "{}\n"  # `hooks.Reply.allow()` as `hooks.main` prints it
AGENT_KEYS = ("agent_type", "agentType", "agentName", "agentDisplayName")
TOOL_INPUT_KEYS = ("tool_input", "toolArgs", "toolInput")
# The variables `command_policy.default_transcript_roots` expands besides the home directory.
TRANSCRIPT_ROOT_VARIABLES = ("CLAUDE_CONFIG_DIR", "ANTIGRAVITY_APP_DATA_DIR")
# Variables that make git find another repository.
GIT_DISCOVERY_VARIABLES = frozenset(
    {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_OBJECT_DIRECTORY",
    }
)  # fmt: skip
# Configuration in the environment (`git -c`, `GIT_CONFIG_KEY_<n>`), read like a `config` file's keys.
GIT_CONFIG_PARAMETERS = "GIT_CONFIG_PARAMETERS"
GIT_CONFIG_KEY_PREFIX = "GIT_CONFIG_KEY_"
# Configuration that may move the work tree, and with it the gate state git would read: `core.worktree`,
# `extensions.worktreeConfig` (a per-worktree file may set it) and `[include]` / `[includeIf]` sections.
# Git reads a key at the start of a line or after a section header on the same line (`[core] worktree`,
# `[core][core] worktree`) and skips a byte-order mark; a value that matches only makes the fast path defer.
WORK_TREE_CONFIG_RE = re.compile(
    rb"(?:^|\])[ \t\r\f\v\xef\xbb\xbf]*worktree(?:config)?[ \t\r\f\v]*(?:=|$)|\[[ \t]*include",
    re.MULTILINE,
)
# A `.git` file of a linked work tree: `gitdir: <path>`; `named_git_dir` reads at most this much of it.
GIT_FILE_PREFIX = b"gitdir: "
GIT_FILE_LIMIT = 4096
STATE_PATH = ("craftsman", "craftsman_state.json")  # below the git directory (`gate.state_path_for`)
# The gate state as `gate.GateState.from_json` reads it (a test keeps these equal to the gate's own).
GATE_PLATFORMS = ("copilot", "claude", "antigravity", "cli")
STAGES = ("implement", "needs_human", "completed", "complete-unisolated", "aborted_max_continuations")
ISOLATION_EVIDENCES = ("hooks", "transcript", "none")
STATE_KEYS = frozenset(
    {
        "owner", "base_sha", "deep", "task", "started_at", "active", "stage", "awaiting_user",
        "isolation_evidence", "forced_continuations", "best_issue_count", "takeover_from",
        "request_step",
    }
)  # fmt: skip
OPTIONAL_STATE_KEYS = frozenset({"evidence_seen", "awaiting_since", "question"})
OWNER_KEYS = frozenset({"platform", "session_id"})


def main(argv: list[str]) -> int:
    """`craftsman hook <argv>`: the fast answer when it is certain, else `hooks.main` with the input."""
    if not is_fast_command(argv):
        return _full_hook(argv)
    try:
        text = read_stdin()
    except Exception:  # noqa: BLE001 - `hooks.main` reads stdin again and fails as it always did
        return _full_hook(argv)
    if not allows(text, os.environ):
        return _full_hook(argv, text)
    sys.stdout.write(ALLOW)
    sys.stdout.flush()
    return 0


def _full_hook(argv: list[str], text: str | None = None) -> int:
    from craftsman.hooks import main as hook_main

    return hook_main(argv, stdin=text)


def is_fast_command(argv: list[str]) -> bool:
    """Whether `argv` is `pre-tool --platform claude|copilot`, as hook configurations write it."""
    return len(argv) == 3 and argv[:2] == ["pre-tool", "--platform"] and argv[2] in PLATFORMS


def read_stdin() -> str:
    """The hook payload: stdin's bytes as UTF-8 (a character it cannot decode is replaced)."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        data: bytes = buffer.read()
        return data.decode("utf-8", errors="replace")
    text: str = sys.stdin.read()
    return text


def allows(text: str, environ: Mapping[str, str]) -> bool:
    """Whether the full handler certainly answers the PreToolUse payload `text` with a bare `{}`."""
    try:
        payload = json.loads(text.strip() or "{}")
        return (
            isinstance(payload, dict)
            and _reads_only(payload)
            and _transcript_roots_are_known(environ)
            and _keeps_the_repository(environ)
            and _gates_are_inactive(payload["cwd"])
        )
    except Exception:  # noqa: BLE001 - in doubt the full handler decides
        return False


def _reads_only(payload: dict[str, object]) -> bool:
    """A read-only tool, `cwd` and input that `hooks.HookInput.parse` accepts, no Craftsman agent."""
    cwd = payload.get("cwd")
    if not (isinstance(cwd, str) and cwd.strip()):
        return False
    if _first_text(payload, "tool_name", "toolName").strip().lower() not in READ_ONLY_TOOL_NAMES:
        return False
    names = [payload.get(key) for key in AGENT_KEYS]
    if any(isinstance(name, str) and "craftsman" in name.lower() for name in names):
        return False
    tool_input = next((payload[key] for key in TOOL_INPUT_KEYS if payload.get(key)), {})
    if isinstance(tool_input, str):
        tool_input = json.loads(tool_input)
    return isinstance(tool_input, dict)


def _first_text(payload: dict[str, object], *keys: str) -> str:
    """`hooks._text`: the first of `keys` that holds non-blank text."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _transcript_roots_are_known(environ: Mapping[str, str]) -> bool:
    """`paths.user_home` answers from the environment, without a lookup that can fail, and no other
    transcript root starts with `~`, which `Path.expanduser` cannot always expand (`~nobody-here`)."""
    if any(environ.get(name, "").strip().startswith("~") for name in TRANSCRIPT_ROOT_VARIABLES):
        return False
    return bool(environ.get("HOME", "").strip() or environ.get("USERPROFILE", "").strip())


def _keeps_the_repository(environ: Mapping[str, str]) -> bool:
    """No variable moves the repository, and no configuration in the environment may set a work tree."""
    if any(name in GIT_DISCOVERY_VARIABLES for name in environ):
        return False
    keys = [value for name, value in environ.items() if name.startswith(GIT_CONFIG_KEY_PREFIX)]
    keys.append(environ.get(GIT_CONFIG_PARAMETERS, ""))  # `'key'='value' ...`: any mention counts
    return not any(word in key.lower() for key in keys for word in ("worktree", "include"))


def _gates_are_inactive(cwd: str) -> bool:
    """Whether each repository git could find from `cwd` has no gate or an inactive one, by its files."""
    directory = os.path.realpath(cwd)
    while True:
        if os.path.lexists(os.path.join(directory, "HEAD")):
            return False  # a git directory (or a bare repository) itself: its work tree may be anywhere
        git_dir = named_git_dir(directory)
        if git_dir is not None and not (
            _names_it_as_git_does(directory)
            and _sets_no_work_tree(git_dir)
            and _gate_is_inactive(git_dir)
        ):
            return False
        parent = os.path.dirname(directory)
        if parent == directory:
            return True
        directory = parent


def named_git_dir(directory: str) -> str | None:
    """The git directory the `.git` entry of `directory` names, with symlinks resolved, without git.

    A `.git` directory names itself; a `.git` file names the target of its `gitdir:` line (relative to
    `directory`), as in a linked work tree. None when there is neither; ValueError for a `.git` file
    without a `gitdir:` line, OSError when it cannot be read.
    """
    dot_git = os.path.join(directory, ".git")
    if os.path.isdir(dot_git):
        return os.path.realpath(dot_git)
    if not os.path.isfile(dot_git):
        return None
    with open(dot_git, "rb") as handle:
        line = handle.readline(GIT_FILE_LIMIT).decode("utf-8", errors="replace").strip()
    if not line.startswith("gitdir:"):
        raise ValueError(f"{dot_git} has no gitdir: line")
    return os.path.realpath(os.path.join(directory, line.removeprefix("gitdir:").strip()))


def _names_it_as_git_does(directory: str) -> bool:
    """Whether git reads the `.git` entry of `directory` as naming the directory `named_git_dir` names.

    Git takes every byte after `gitdir: ` up to the trailing line ends, blanks included; `named_git_dir`
    reads the first line, strips it and replaces what is not UTF-8. They agree on a directory and on a
    short UTF-8 pointer whose path is one line with no blank at either end.
    """
    dot_git = os.path.join(directory, ".git")
    if os.path.isdir(dot_git):
        return True
    with open(dot_git, "rb") as handle:
        data = handle.read(GIT_FILE_LIMIT)
    path = data.removeprefix(GIT_FILE_PREFIX).rstrip(b"\r\n").decode("utf-8")
    return (
        data.startswith(GIT_FILE_PREFIX)
        and len(data) < GIT_FILE_LIMIT
        and path != ""
        and path == path.strip()
        and not any(end in path for end in "\r\n")
    )


def _sets_no_work_tree(git_dir: str) -> bool:
    """No `config` of `git_dir` (or of its common directory) may set a work tree or include a file."""
    configs = [os.path.join(git_dir, "config")]
    common = _read(os.path.join(git_dir, "commondir"))
    if common is not None:
        relative = common.decode("utf-8").rstrip("\r\n")
        if not relative or "\n" in relative:
            return False
        configs.append(os.path.join(git_dir, relative, "config"))
    return not any(_may_move_work_tree(data) for data in map(_read, configs) if data is not None)


def _may_move_work_tree(config: bytes) -> bool:
    """Whether a git `config` may set a work tree or include a file (`WORK_TREE_CONFIG_RE`, any case)."""
    return WORK_TREE_CONFIG_RE.search(config.lower()) is not None


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


def _gate_is_inactive(git_dir: str) -> bool:
    """No gate state in `git_dir`, or one `gate.load_state` reads without error and finds inactive."""
    try:
        with open(os.path.join(git_dir, *STATE_PATH), encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return True
    return is_inactive_state(json.loads(text))


def is_inactive_state(raw: object) -> bool:
    """Whether `raw` is a valid gate state (every check of `GateState.from_json`) that is not active."""
    if not isinstance(raw, dict) or raw.get("active") is not False:
        return False
    if not STATE_KEYS <= set(raw) <= STATE_KEYS | OPTIONAL_STATE_KEYS:
        return False
    counts = [raw[key] for key in ("best_issue_count", "evidence_seen") if raw.get(key) is not None]
    return (
        _is_owner(raw["owner"])
        and (raw["takeover_from"] is None or _is_owner(raw["takeover_from"]))
        and isinstance(raw["base_sha"], str)
        and raw["base_sha"] != ""
        and all(isinstance(raw[key], str) for key in ("task", "started_at"))
        and all(isinstance(raw[key], bool) for key in ("deep", "awaiting_user"))
        and _is_one_of(raw["stage"], STAGES)
        and _is_one_of(raw["isolation_evidence"], ISOLATION_EVIDENCES)
        and all(_is_count(value) for value in (raw["forced_continuations"], *counts))
        and (raw["request_step"] is None or _is_integer(raw["request_step"]))
        and all(isinstance(raw.get(key), (str, type(None))) for key in ("awaiting_since", "question"))
    )


def _is_owner(raw: object) -> bool:
    if not isinstance(raw, dict) or set(raw) != OWNER_KEYS:
        return False
    session_id = raw["session_id"]
    return _is_one_of(raw["platform"], GATE_PLATFORMS) and (
        session_id is None or (isinstance(session_id, str) and bool(session_id.strip()))
    )


def _is_one_of(value: object, choices: tuple[str, ...]) -> bool:
    return isinstance(value, str) and value in choices


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
