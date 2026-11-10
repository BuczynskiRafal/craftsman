"""The command policy: every PreToolUse decision about one tool call, whichever platform made it.

The Claude Code and Copilot hooks (`hooks.on_pre_tool`) are thin adapters over `decide`, and the Antigravity scope
guard calls the same entry point: an adapter describes the call platform-neutrally (`ToolCall`: the kind of tool,
the shell command or the paths it names, the directory it runs in, whether the caller is the main session or a
Craftsman subagent, the caller's session id) and gets an allow, deny or ask (`Decision`). Nothing here reads a
hook payload or writes to disk; what needs the work tree (its git directory, the gate it owns, a review in
progress) arrives as lazy callbacks in `PolicyContext`, so a call that touches none of it costs no subprocess.

What the policy decides, in this order, for a shell command:
- the hook entry points are run by the platform, never by an agent (`craftsman hook ...` in every spelling,
  the plugin's launchers among them, `python -m craftsman.hooks`, the guard scripts, and inline programs that import craftsman; what a `python`
  command line runs (`-c` code, `-m` module, a script, stdin) is decided by `shell.python_program`, which
  reads the interpreter's options as Python itself does, and this module does not restate that grammar);
- the ledger (`<git-dir>/craftsman/`) and the platform transcripts are not written by agents, by any write
  verb, redirection or inline program that names them (reading stays allowed);
- the identity variables the platform sets are not set or unset for a craftsman command;
- a gate is not taken over by its own owner, and `craftsman gate --session-id` names only the caller's session;
- the `craftsman review` commands that belong to someone else: re-planning by a hunter or a verifier, briefs
  by the main session, slot-tagged records by the main session;
- the work-tree guard (`worktree_guard`): subagents never change the tree; the main session is asked while a
  review is in progress.
File-writing tools (Write, Edit, MultiEdit, NotebookEdit, Copilot's edit tools, MCP tools that take a path) are
checked against the same protected places.

These guards stop low-effort shortcuts; they are not a sandbox. A script file written first and run later, a
program name or path built by command substitution, and an interpreter that assembles the path at run time pass
a shell parser unseen.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from craftsman.diffscan import DiffScanError
from craftsman.files import decode_json_bytes
from craftsman.paths import user_home
from craftsman.records import (
    LEAD_HUNTER,
    ORCHESTRATOR_AGENT,
    AgentRole,
    agent_role,
    craftsman_agent_name,
)
from craftsman.shell import (
    SHELL_EXECUTABLES,
    SimpleCommand,
    expand_shell_path_token,
    iter_simple_commands,
    python_program,
    redirection_targets,
)
from craftsman.tool_names import READ_PATH_KEYS, READ_TOOL_NAMES, SEARCH_TOOL_NAMES, SHELL_TOOL_NAMES
from craftsman.worktree_guard import find_worktree_mutation

# --- the messages -----------------------------------------------------------------------------------

HOOKS_ARE_THE_PLATFORMS = "Craftsman hook commands are run by the platform, not by agents."
LEDGER_IS_WRITTEN_BY_THE_CLI = "The Craftsman ledger is written only by `craftsman` commands."
TRANSCRIPTS_ARE_THE_PLATFORMS = "Agent transcripts are written only by the platform."
IDENTITY_IS_THE_PLATFORMS = "Craftsman identity variables are set by the platform."
LEAD_ONLY = (
    "Slot-tagged records come from the hunter you launched for that slot; record your own findings with "
    "`--hunter lead`."
)
OWNER_CANNOT_TAKE_OVER = (
    "This session owns the gate; finish it with `craftsman review check` passing or ask the user to close it."
)
NO_RESTART = (
    "Craftsman hunters and verifiers must not re-plan or reset the review; record with `--hunter` and "
    "report back."
)
BRIEFS_ARE_FOR_SUBAGENTS = (
    "Briefs are for the subagents you launch: give each the one-line prompt that `prepare` / `check` printed."
)
RESTARTING_COMMANDS = frozenset({"prepare", "reset"})

# --- reading a craftsman command ----------------------------------------------------------------------

# The CLI by name, and the plugin's launchers in `bin/`, which start it (`craftsman` is one of them).
_CRAFTSMAN_EXECUTABLES = frozenset({"craftsman", "craftsman.exe", "craftsman.cmd", "launcher.sh", "launcher.cmd"})
_CLI_MODULES = frozenset({"craftsman.cli", "craftsman"})
_CLI_SCRIPT = "craftsman-cli.py"
_CLI_FILE = "craftsman/cli.py"
# Entry points that only a platform runs: the hook dispatcher, the Antigravity guards and their scripts.
_ENTRY_MODULES = frozenset({"craftsman.hooks", "craftsman.antigravity", "craftsman.scope"})
_ENTRY_SCRIPTS = frozenset({"craftsman-stop-gate.py", "craftsman-scope-guard.py"})
_ENTRY_FILES = ("craftsman/hooks.py", "craftsman/antigravity.py", "craftsman/scope.py")
# Programs that run another program: the words that follow the launcher's name before the command it runs.
_LAUNCHERS: dict[str, tuple[str, ...]] = {"uv": ("run",), "uvx": (), "pipx": ("run",), "poetry": ("run",), "npx": ()}
# Interpreters whose inline program (`-c` / `-e`) can name a file, with the option that carries it.
_INLINE_OPTIONS: dict[str, str] = {"node": "-e", "nodejs": "-e", "perl": "-e", "ruby": "-e", "php": "-r"}
# The options of a POSIX shell that take the next word as their value, before the script it runs.
_SHELL_OPTIONS_WITH_VALUE = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})

CallKind = Literal["cli", "entry", "inline", "stdin"]


@dataclass(frozen=True)
class Invocation:
    """What a simple command runs: the craftsman CLI (`args` follow its name), a hook or guard entry point, an
    interpreter's inline program (`args` is its code), or Python reading its program from stdin."""

    kind: CallKind
    args: tuple[str, ...]
    command: SimpleCommand


def _invocation(tokens: Sequence[str]) -> tuple[CallKind, tuple[str, ...]] | None:
    if not tokens:
        return None
    executable = os.path.basename(tokens[0].replace("\\", "/")).lower()
    if executable in _CRAFTSMAN_EXECUTABLES or executable == _CLI_SCRIPT:
        return "cli", tuple(tokens[1:])
    if executable in _ENTRY_SCRIPTS:
        return "entry", tuple(tokens[1:])
    if executable in _LAUNCHERS:
        return _invocation(_launched(executable, tokens[1:]))
    if executable in SHELL_EXECUTABLES:
        return _invocation(_shell_script(tokens[1:]))
    if executable in ("cmd", "cmd.exe"):
        return _cmd_invocation(tokens[1:])
    if executable in _INLINE_OPTIONS:
        option = _INLINE_OPTIONS[executable]
        if option in tokens[1:]:
            index = tokens.index(option, 1)
            return ("inline", (tokens[index + 1],)) if index + 1 < len(tokens) else None
        return None
    if not (executable.startswith("python") or executable in ("py", "py.exe")):
        return None
    return _python_invocation(tokens)


def _python_invocation(tokens: Sequence[str]) -> tuple[CallKind, tuple[str, ...]] | None:
    kind, value, rest = python_program(tokens[1:])
    if kind == "code":
        return "inline", (value,)
    if kind == "module":
        if value in _LAUNCHERS:
            return _invocation(_launched(value, rest))
        if value in _CLI_MODULES:
            return "cli", tuple(rest)
        return ("entry", tuple(rest)) if value in _ENTRY_MODULES else None
    if kind == "script":
        script = value.replace("\\", "/").lower()
        if os.path.basename(script) == _CLI_SCRIPT or script.endswith(_CLI_FILE):
            return "cli", tuple(rest)
        if os.path.basename(script) in _ENTRY_SCRIPTS or script.endswith(_ENTRY_FILES):
            return "entry", tuple(rest)
        return None
    return "stdin", ()  # no script and no module: the program comes from stdin


def _launched(launcher: str, tokens: Sequence[str]) -> Sequence[str]:
    """The command `launcher` runs, given the words after its name: its own options and subcommand are skipped;
    empty when the launcher is not asked to run anything (`uv sync`)."""
    words = _after_options(tokens)
    for word in _LAUNCHERS[launcher]:
        if not words or words[0] != word:
            return ()
        words = _after_options(words[1:])
    return words


def _shell_script(tokens: Sequence[str]) -> Sequence[str]:
    """The script a POSIX shell runs and its arguments (`sh bin/launcher.sh hook stop`), given the words after the
    shell's name; empty when the shell runs a `-c` string (`shell.iter_simple_commands` reads that one) or stdin."""
    index = 0
    while index < len(tokens) and tokens[index][:1] in ("-", "+") and tokens[index] not in ("-", "--"):
        word = tokens[index]
        if not word.startswith("--") and ("c" in word[1:] or "s" in word[1:]):
            return ()
        index += 2 if word in _SHELL_OPTIONS_WITH_VALUE else 1
    index += index < len(tokens) and tokens[index] == "--"
    return tokens[index:]


def _cmd_invocation(tokens: Sequence[str]) -> tuple[CallKind, tuple[str, ...]] | None:
    """What `cmd /c <command>` (or `/k`) runs: the command follows as words, or as one quoted string."""
    switches = [index for index, word in enumerate(tokens) if word.lower() in ("/c", "/k")]
    if not switches:
        return None
    rest = tokens[switches[0] + 1 :]
    if len(rest) != 1:
        return _invocation(rest)
    for item in iter_simple_commands(rest[0]):
        parsed = _invocation(item.tokens)
        if parsed is not None:
            return parsed
    return None


def _after_options(tokens: Sequence[str]) -> Sequence[str]:
    """`tokens` without the options (words that start with `-`) in front of the first plain word."""
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 1
    return tokens[index:]


def invocations(commands: Iterable[SimpleCommand]) -> tuple[Invocation, ...]:
    found: list[Invocation] = []
    for item in commands:
        parsed = _invocation(item.tokens)
        if parsed is not None:
            found.append(Invocation(parsed[0], parsed[1], item))
    return tuple(found)


def craftsman_arguments(tokens: Sequence[str]) -> tuple[str, ...] | None:
    """What follows the Craftsman CLI in a simple command, None when the command is another program.

    The executable has to be `craftsman`, `craftsman.exe`, `python -m craftsman.cli`, a `craftsman-cli.py`
    script or one of the plugin's launchers (`launcher.sh`, `launcher.cmd`, `craftsman.cmd`, also through `sh`,
    `bash` or `cmd /c`), alone or run by a launcher (`uv run`, `uvx`, `pipx run`, `poetry run`, `npx`), so text
    that merely mentions the command (`echo craftsman review prepare`) names none.
    """
    parsed = _invocation(tokens)
    return parsed[1] if parsed is not None and parsed[0] == "cli" else None


def review_commands(command: str, cwd: str | None = None) -> tuple[str, ...]:
    """The subcommand of every `craftsman review <sub>` the shell command line runs, in order.

    Found with the same parser every PreToolUse rule uses (`shell.iter_simple_commands` and `craftsman_arguments`).
    Deliberately not read: a program named by command substitution (`$(which craftsman) review ...`), a command
    line inside `powershell -Command "..."`, and a launcher option that takes its value as a separate word
    (`uv run --project dir craftsman ...`); a main session that goes to such lengths gets an advisory review,
    which `prepare` says so.
    """
    return _review_subcommands(invocations(iter_simple_commands(command, cwd)))


def _review_subcommands(calls: Iterable[Invocation]) -> tuple[str, ...]:
    found: list[str] = []
    for call in calls:
        if call.kind == "cli" and len(call.args) >= 2 and call.args[0] == "review" and not call.args[1].startswith("-"):
            found.append(call.args[1])
    return tuple(found)


def arming_action(review_subcommands: Iterable[str]) -> Literal["arm", "touch"] | None:
    """What a `craftsman review` command does to the review guard: any command but `report` arms it, a `report`
    only touches it, and a command that is no review command leaves it alone."""
    subs = tuple(review_subcommands)
    if any(sub != "report" for sub in subs):
        return "arm"
    return "touch" if subs else None


def is_main_session(agent: str, agent_id: str) -> bool:
    """Whether a tool call is the main session's: no subagent role, no agent id, and no agent name other than
    Craftsman's own orchestrator (a session started with `--agent craftsman` is still the main session)."""
    if agent_role(agent) is not None or agent_id:
        return False
    return not agent.strip() or craftsman_agent_name(agent) == ORCHESTRATOR_AGENT


# --- option parsing -----------------------------------------------------------------------------------

# Every long option of the commands whose options the policy reads, one table per command: argparse resolves an
# abbreviation against the options of the command it parses, so `--s` is `--session-id` for `gate` but ambiguous
# for `review add`. `tests/test_command_policy.py` compares each table with the live parser.
GATE_OPTIONS = (
    "--await-user", "--base-sha", "--complete", "--cwd", "--deep", "--help", "--init", "--question", "--resume",
    "--session-id", "--takeover", "--task",
)  # fmt: skip
COVER_OPTIONS = ("--check", "--cwd", "--files", "--from-json", "--help", "--hunks", "--hunter", "--note")
ADD_OPTIONS = (
    "--check", "--cwd", "--file", "--from-json", "--help", "--hunter", "--lens", "--line", "--scenario", "--severity",
    "--suggestion", "--title",
)  # fmt: skip
BRIEF_OPTIONS = ("--cwd", "--help", "--verifier")


def _is_option(word: str, option: str, options: Sequence[str]) -> bool:
    """Whether `word` (without a `=value`) is `option` or, as argparse reads it, a unique abbreviation of it among
    the `options` of the command."""
    if not word.startswith("--") or len(word) < 3:
        return False
    if word == option:
        return True
    return option.startswith(word) and [name for name in options if name.startswith(word)] == [option]


def option_values(args: Sequence[str], option: str, options: Sequence[str]) -> list[str]:
    """The values given to a long `option` (`--opt value` and `--opt=value`) of a command with these `options`, in order."""
    values: list[str] = []
    index = 0
    while index < len(args):
        word, equals, value = args[index].partition("=")
        if _is_option(word, option, options):
            if equals:
                values.append(value)
            elif index + 1 < len(args):
                values.append(args[index + 1])
                index += 1
        index += 1
    return values


def has_option(args: Sequence[str], option: str, options: Sequence[str]) -> bool:
    return any(_is_option(word.partition("=")[0], option, options) for word in args)


# --- the call -----------------------------------------------------------------------------------------

ToolKind = Literal["shell", "write", "read"]
Action = Literal["allow", "deny", "ask"]

# Tools that only read or search; a path they carry is not a write. Anything else with a path is a candidate.
READ_ONLY_TOOL_NAMES = READ_TOOL_NAMES | SEARCH_TOOL_NAMES | frozenset(
    {
        "readfile", "readmcpresource", "listmcpresources", "semantic_search", "semanticsearch", "text_search",
        "textsearch", "codebase", "search", "get_errors", "list_code_usages", "web_search", "websearch", "webfetch",
        "fetch_webpage", "todowrite", "todoread",
    }
)  # fmt: skip
_READ_ONLY_PREFIXES = ("read", "view", "get_", "list", "search", "find", "grep", "glob", "fetch")
_PATH_KEYS = frozenset(key.lower() for key in READ_PATH_KEYS) | frozenset(
    {"notebookpath", "target_file", "filename", "file", "files", "paths", "destination", "dest", "newpath", "oldpath"}
)  # fmt: skip
_PATCH_KEYS = frozenset({"input", "patch", "diff"})
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)


def tool_kind(tool_name: str) -> ToolKind:
    """`shell` for a tool that runs a command line, `read` for one that only reads or searches, else `write`: any
    other tool that carries a path argument is a write candidate."""
    name = tool_name.strip().lower()
    if name in SHELL_TOOL_NAMES:
        return "shell"
    action = name.rsplit("__", 1)[-1]  # an MCP tool is `mcp__<server>__<tool>`: the tool decides
    if name in READ_ONLY_TOOL_NAMES or action in READ_ONLY_TOOL_NAMES or action.startswith(_READ_ONLY_PREFIXES):
        return "read"
    return "write"


def candidate_paths(tool_input: object) -> tuple[str, ...]:
    """The paths a (non-shell) tool call names: `file_path`, `notebook_path`, `filePath`, `path`, `TargetFile`,
    `files[]`, `replacements[].filePath` and the like, found at any depth, and the files an `apply_patch` text edits."""
    found: list[str] = []
    _collect_paths(tool_input, False, found)
    return tuple(dict.fromkeys(found))


def _collect_paths(value: object, is_path: bool, found: list[str]) -> None:
    if isinstance(value, str):
        if is_path and value.strip():
            found.append(value.strip().removeprefix("file://"))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).lower()
            if name in _PATCH_KEYS and isinstance(item, str):
                found.extend(
                    (match[1] or match[2]).strip() for match in _PATCH_FILE_RE.finditer(item) if (match[1] or match[2])
                )
            _collect_paths(item, name in _PATH_KEYS, found)
    elif isinstance(value, list):
        for item in value:
            _collect_paths(item, is_path, found)


@dataclass(frozen=True)
class ToolCall:
    """One tool call, described the same way for every platform.

    `role` is the Craftsman role of a subagent caller; `main_session` is true for the session's own agent (the
    two are never both set). `cwd` is the directory the call runs in (a shell call's own `cwd` argument applied).
    """

    kind: ToolKind
    cwd: str
    command: str = ""
    paths: tuple[str, ...] = ()
    role: AgentRole | None = None
    main_session: bool = False
    session_id: str | None = None


def tool_call(
    tool_name: str,
    tool_input: Mapping[str, object],
    cwd: str,
    *,
    role: AgentRole | None,
    main_session: bool,
    session_id: str | None,
) -> ToolCall:
    """The `ToolCall` of a tool call as a platform sent it (`command` / `CommandLine` for a shell tool)."""
    kind = tool_kind(tool_name)
    if kind == "shell":
        command = tool_input.get("command") or tool_input.get("CommandLine")
        tool_cwd = tool_input.get("cwd") or tool_input.get("Cwd")
        where = os.path.join(cwd, tool_cwd) if isinstance(tool_cwd, str) and tool_cwd else cwd
        return ToolCall(kind, where, command=command if isinstance(command, str) else "", role=role,
                        main_session=main_session, session_id=session_id)  # fmt: skip
    paths = candidate_paths(tool_input) if kind == "write" else ()
    return ToolCall(kind, cwd, paths=paths, role=role, main_session=main_session, session_id=session_id)


@dataclass(frozen=True)
class Decision:
    """The policy's answer. `review` lists the `craftsman review` subcommands the call runs (for arming the review guard)."""

    action: Action = "allow"
    reason: str = ""
    review: tuple[str, ...] = ()

    @property
    def refuses(self) -> bool:
        return self.action != "allow"


def _noop_ledger_dir() -> Path | None:
    return None


def _never() -> bool:
    return False


def _no_review() -> int | None:
    return None


@dataclass(frozen=True)
class PolicyContext:
    """What the policy asks of the work tree, lazily; the defaults say there is none.

    `ledger_dir` is `<git-dir>/craftsman` (None when it cannot be found), `owns_gate` whether the calling session
    owns the active gate, `review_in_progress` the latest round number while a review-only round is open (the
    work-tree guard asks the main session before it changes the tree), `transcript_roots` the places where
    platforms keep session transcripts (`default_transcript_roots`).
    """

    ledger_dir: Callable[[], Path | None] = _noop_ledger_dir
    owns_gate: Callable[[], bool] = _never
    review_in_progress: Callable[[], int | None] = _no_review
    transcript_roots: tuple[Path, ...] = field(default_factory=lambda: default_transcript_roots())


# --- protected places ---------------------------------------------------------------------------------

# Files of the Craftsman directory that only `craftsman` commands write: `ledger.json` and its backups
# (`ledger.v3.<stamp>.bak.json`), the hook state and the hook error log (`hook-errors.log`, `hook-errors.log.1`).
_LEDGER_FILE_RE = re.compile(r"(?:ledger|review_hooks|craftsman_state)(?:\.[\w-]+)*\.json|hook-errors\.log(?:\.\d+)?", re.IGNORECASE)
# What marks a transcript below a platform's transcript root: a `.jsonl` log, or a folder the platform fills with them.
# The rest (Claude's `memory/*.md`, Copilot's `plan.md`, Antigravity's `task.md`) is written by agents on purpose.
_TRANSCRIPT_SUFFIX = ".jsonl"
_TRANSCRIPT_FOLDERS = frozenset({"subagents", ".system_generated"})
_TRANSCRIPT_TEXT_RE = re.compile(r"\.jsonl\b|\bsubagents\b|\.system_generated", re.IGNORECASE)
# Subfolders agents write on purpose: `craftsman review inbox` and `scratch` hand them out.
_AGENT_FOLDERS = frozenset({"inbox", "scratch"})
_LEDGER_TEXT_RE = re.compile(
    r"\.git[/\\]+(?:worktrees[/\\]+[^/\\\s'\"]+[/\\]+)?craftsman\b(?![/\\]+(?:inbox|scratch)\b)"
    r"|\b(?:ledger\.json|review_hooks\.json|craftsman_state\.json|hook-errors\.log)\b"
    r"|rev-parse\s+--(?:absolute-)?git-dir",
    re.IGNORECASE,
)
_WRITING_RE = re.compile(
    r"""\.write\w*\s*\(|\bwrite\w*\s*\(|\b(?:json|pickle|yaml|marshal|toml|tomli_w)\.dump\s*\(|\bos\.(?:remove|unlink|rename|replace|truncate|symlink|link|
    makedirs|mkdir|system|popen)|\bshutil\b|\.(?:unlink|rename|replace|touch|truncate|symlink_to|hardlink_to)\s*\(
    |\bopen\w*\s*\([^\n]*?[,=]\s*['"][rb]*[wax+]|\bfs\.\w*(?:write|append|unlink|rename|copy|rm)|\bFile\.(?:write|open)
    |\bfile_put_contents|\b(?:sed|perl)\s+-\w*i|\b(?:cp|mv|rm|tee|ln|dd|install|truncate|touch)\s""",
    re.VERBOSE,
)
_PWSH_WRITERS = frozenset(
    {
        "set-content", "add-content", "out-file", "remove-item", "copy-item", "move-item", "new-item", "clear-content",
        "rename-item", "ri", "rm", "del", "erase", "mi", "move", "ni", "sc", "ac", "cpi",
    }
)  # fmt: skip
_ALL_ARGS_WRITERS = frozenset({"tee", "mv", "rm", "unlink", "rmdir", "truncate", "touch", "shred", "sponge", "ed"})
_DESTINATION_WRITERS = frozenset({"cp", "install", "ln", "rsync", "scp"})
_IN_PLACE_RE = re.compile(r"^-[A-Za-z]*i|^--in-place\b")


def default_transcript_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Where the platforms keep session transcripts: Claude Code (`~/.claude/projects`, `$CLAUDE_CONFIG_DIR/projects`),
    Copilot (`~/.copilot/session-state`) and Antigravity (`<app data dir>/brain`)."""
    values = os.environ if env is None else env
    home = user_home()
    roots = [home / ".claude" / "projects", home / ".copilot" / "session-state", home / ".gemini" / "antigravity" / "brain"]
    if values.get("CLAUDE_CONFIG_DIR", "").strip():
        roots.append(Path(values["CLAUDE_CONFIG_DIR"]).expanduser() / "projects")
    if values.get("ANTIGRAVITY_APP_DATA_DIR", "").strip():
        roots.append(Path(values["ANTIGRAVITY_APP_DATA_DIR"]).expanduser() / "brain")
    return tuple(dict.fromkeys(roots))


def _below(path: Path, root: Path) -> tuple[str, ...] | None:
    """The parts of `path` below `root` (none for `root` itself), None when it lies elsewhere. Names are compared
    without regard to case: macOS and Windows file systems do not tell `.GIT/CRAFTSMAN` from `.git/craftsman`."""
    count = len(root.parts)
    if len(path.parts) < count or [part.casefold() for part in path.parts[:count]] != [
        part.casefold() for part in root.parts
    ]:
        return None
    return path.parts[count:]


def _is_transcript(path: Path, root: Path) -> bool:
    """Whether `path` is a transcript below a platform's transcript `root` (or the root itself)."""
    below = _below(path, root)
    if below is None:
        return False
    return not below or below[-1].casefold().endswith(_TRANSCRIPT_SUFFIX) or any(
        part.casefold() in _TRANSCRIPT_FOLDERS for part in below
    )


def _real(raw: str, cwd: str) -> Path:
    """`raw` made absolute against `cwd` (after `~` and `$HOME`), with `..` and symlinks resolved."""
    expanded = expand_shell_path_token(raw)
    return Path(os.path.realpath(expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)))


def _unresolved(text: str) -> bool:
    """Whether a path or directory still holds a variable or a command substitution: where it points is unknown."""
    return any(mark in text for mark in ("$", "`", "%"))


class ProtectedPlaces:
    """The ledger directory and the transcript roots, found lazily: git is asked only for a path that mentions them."""

    def __init__(self, context: PolicyContext) -> None:
        self._context = context
        self._roots = tuple(Path(os.path.realpath(root)) for root in context.transcript_roots)
        self._ledger: Path | None = None
        self._ledger_known = False

    def ledger_dir(self) -> Path | None:
        if not self._ledger_known:
            try:
                found = self._context.ledger_dir()
            except DiffScanError:
                found = None  # a git timeout was logged by the budget; the lexical test still applies
            self._ledger = Path(os.path.realpath(found)) if found is not None else None
            self._ledger_known = True
        return self._ledger

    def violation(self, raw: str, cwd: str | None, *, unknown_cwd: bool = False) -> str | None:
        """The message to deny a write to `raw` with, None when it is not a protected place."""
        base = cwd or os.getcwd()
        path = _real(raw, base)
        if any(_is_transcript(path, root) for root in self._roots):
            return TRANSCRIPTS_ARE_THE_PLATFORMS
        if self._in_ledger(path):
            return LEDGER_IS_WRITTEN_BY_THE_CLI
        if (unknown_cwd or _unresolved(raw)) and _LEDGER_FILE_RE.fullmatch(path.name):
            return LEDGER_IS_WRITTEN_BY_THE_CLI  # where it points is unknown: the file name decides
        return None

    def _in_ledger(self, path: Path) -> bool:
        parts = path.parts
        folded = [part.casefold() for part in parts]
        for index, part in enumerate(folded):
            git_dir_before = index >= 1 and folded[index - 1] == ".git"
            worktree_before = index >= 3 and folded[index - 2] == "worktrees" and folded[index - 3] == ".git"
            if part == "craftsman" and (git_dir_before or worktree_before):
                return not _is_agent_folder(parts[index + 1 :])
        if "craftsman" not in folded:
            return False  # nothing mentions the ledger: no git call
        ledger = self.ledger_dir()
        below = _below(path, ledger) if ledger is not None else None
        return below is not None and not _is_agent_folder(below)

    def inline_violation(self, text: str) -> str | None:
        """The message to deny an inline program with when `text` names a protected place and can write."""
        lowered = text.lower()
        names = None
        if _LEDGER_TEXT_RE.search(text):
            names = LEDGER_IS_WRITTEN_BY_THE_CLI
        elif "craftsman" in lowered and (ledger := self.ledger_dir()) is not None and str(ledger).lower() in lowered:
            names = LEDGER_IS_WRITTEN_BY_THE_CLI
        elif _TRANSCRIPT_TEXT_RE.search(text) and (
            any(str(root) in text or root.as_posix() in text for root in self._roots)
            or re.search(r"\.claude[/\\]+projects|\.copilot[/\\]+session-state|antigravity[/\\]+brain|CLAUDE_CONFIG_DIR", text)
        ):
            names = TRANSCRIPTS_ARE_THE_PLATFORMS
        if names is None or not _WRITING_RE.search(text):
            return None
        return names


def _is_agent_folder(below: Sequence[str]) -> bool:
    return bool(below) and below[0].casefold() in _AGENT_FOLDERS


# --- the checks ---------------------------------------------------------------------------------------


def _non_options(tokens: Sequence[str]) -> list[str]:
    return [token for token in tokens if not token.startswith("-") or token == "-"]


def _written_paths(item: SimpleCommand) -> list[str]:
    """The paths a simple command writes to: its redirections, and the arguments its write verb changes."""
    paths = list(redirection_targets(item.segment))
    tokens = item.tokens
    if not tokens:
        return paths
    verb = os.path.basename(tokens[0]).lower()
    rest = tokens[1:]
    if verb in _ALL_ARGS_WRITERS or verb in _PWSH_WRITERS:
        paths.extend(_non_options(rest))
    elif verb in _DESTINATION_WRITERS:
        targets = option_values_short(rest, ("-t", "--target-directory"))
        positional = _non_options(rest)
        if targets:
            paths.extend(targets)
        elif len(positional) >= 2:
            paths.append(positional[-1])
        if _makes_hard_link(verb, rest):
            paths.extend(positional)  # the link and the file are one inode: writing one writes the other
    elif verb in ("sed", "perl", "ruby") and any(_IN_PLACE_RE.match(token) for token in rest):
        paths.extend(_non_options(rest))
    elif verb in ("awk", "gawk") and "inplace" in rest:
        paths.extend(_non_options(rest))
    elif verb == "dd":
        paths.extend(token[3:] for token in rest if token.startswith("of="))
    elif verb == "find" and ("-delete" in rest or _find_runs_a_writer(rest)):
        paths.extend(_non_options(rest))
    elif verb == "curl":
        paths.extend(option_values_short(rest, ("-o", "--output")))
    elif verb == "wget":
        paths.extend(option_values_short(rest, ("-O", "--output-document")))
    return [path for path in paths if path]


_FIND_EXEC_OPTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _find_runs_a_writer(args: Sequence[str]) -> bool:
    """Whether a `find ... -exec <command>` runs a command that writes to its arguments."""
    writers = _ALL_ARGS_WRITERS | _DESTINATION_WRITERS | _PWSH_WRITERS
    return any(
        word in _FIND_EXEC_OPTIONS and index + 1 < len(args) and os.path.basename(args[index + 1]).lower() in writers
        for index, word in enumerate(args)
    )


def _makes_hard_link(verb: str, args: Sequence[str]) -> bool:
    """`ln` without `-s` and `cp -l` / `--link`."""
    short = "".join(word[1:] for word in args if word.startswith("-") and not word.startswith("--"))
    if verb == "ln":
        return "s" not in short and "--symbolic" not in args
    return verb == "cp" and ("l" in short or "--link" in args)


def option_values_short(args: Sequence[str], names: Sequence[str]) -> list[str]:
    """Values of the options in `names` (`-t dir`, `--target-directory=dir`, `-tdir`)."""
    values: list[str] = []
    for index, word in enumerate(args):
        for name in names:
            if word == name and index + 1 < len(args):
                values.append(args[index + 1])
            elif word.startswith(name + "="):
                values.append(word[len(name) + 1 :])
            elif not name.startswith("--") and word.startswith(name) and len(word) > len(name):
                values.append(word[len(name) :])
    return values


def _forges_a_hook(calls: Sequence[Invocation], command: str) -> bool:
    """A hook entry point run by an agent, or an inline program that imports craftsman (it bypasses every rule here)."""
    for call in calls:
        if call.kind == "entry":
            return True
        if call.kind == "cli" and (_first_positional(call.args) == "hook"):
            return True
        if call.kind == "inline" and _IMPORT_RE.search(call.args[0]):
            return True
        if call.kind == "stdin" and _IMPORT_IN_TEXT_RE.search(command):
            return True
    return False


_IMPORT_PATTERN = (
    r"(?:{start})from\s+craftsman\b|(?:{start})import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*craftsman\b"
    r"""|(?:__import__|import_module|run_module|run_path)\s*\(\s*['"]craftsman"""
)
# The code of `python -c`: an import is a statement, so it follows a line start, a space, `;` or a bracket.
_IMPORT_RE = re.compile(_IMPORT_PATTERN.format(start=r"^|[\s;(,]"))
# The whole command line of a program fed through stdin (`echo 'import craftsman' | python`): the code may sit in quotes.
_IMPORT_IN_TEXT_RE = re.compile(_IMPORT_PATTERN.format(start=r"""^|[\s;(,'"]"""))


def _first_positional(args: Sequence[str]) -> str | None:
    return next((word for word in args if not word.startswith("-")), None)


def _check_ledger_writes(
    commands: Sequence[SimpleCommand], calls: Sequence[Invocation], places: ProtectedPlaces, call: ToolCall
) -> str | None:
    cwd = call.cwd
    for item in commands:
        where = item.cwd or cwd
        unknown = item.cwd is not None and _unresolved(item.cwd)
        for raw in _written_paths(item):
            message = places.violation(raw, where, unknown_cwd=unknown)
            if message is not None:
                return message
    call_text = call.command
    for inline in calls:
        if inline.kind == "inline":
            message = places.inline_violation(inline.args[0])
        elif inline.kind == "stdin":
            message = places.inline_violation(call_text)
        else:
            continue
        if message is not None:
            return message
    return None


def _is_identity_variable(name: str) -> bool:
    return name in _IDENTITY_VARIABLES or name.startswith("CRAFTSMAN_")


_IDENTITY_VARIABLES = frozenset(
    {
        "ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_APP_DATA_DIR", "ANTIGRAVITY_SESSION_ID", "ANTIGRAVITY_AGENT",
        "AGY_MANAGED_ENV", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
        "COPILOT_SESSION_ID", "COPILOT_CLI", "GITHUB_COPILOT_CLI",
    }
)  # fmt: skip
_ENV_STATEMENTS = frozenset({"export", "declare", "typeset", "readonly", "local", "unset", "set", "setx", "setenv"})
_POWERSHELL_ENV_RE = re.compile(r"\$env:(\w+)\s*=|\b(?:set-item|remove-item|si|ri)\b[^;|&\n]*\benv:(\w+)", re.IGNORECASE)


def _names_identity(word: str) -> bool:
    """Whether a word of a command prefix or a statement sets or unsets an identity variable (`VAR=x`, `-u VAR`, `--unset=VAR`)."""
    name = word.partition("=")[0]
    if name.startswith("--unset"):
        name = word.partition("=")[2]
    elif name.startswith("-u") and len(name) > 2:
        name = name[2:]
    return _is_identity_variable(name)


def _sets_identity(commands: Sequence[SimpleCommand], calls: Sequence[Invocation], command: str) -> bool:
    """An identity variable set or unset for a craftsman command: inline (`VAR=x craftsman`, `env VAR=x`,
    `env -u VAR`), or by a statement of the same line (`export`, `declare -x`, `unset`, `$env:VAR = ...`)."""
    for call in calls:
        if call.kind not in ("cli", "entry"):
            continue
        if any(_names_identity(word) for word in call.command.prefix):
            return True
    if not any(call.kind in ("cli", "entry") for call in calls):
        return False
    for item in commands:
        if item.tokens and os.path.basename(item.tokens[0]).lower() in _ENV_STATEMENTS:
            if any(_names_identity(word) for word in item.tokens[1:]):
                return True
    return any(
        _is_identity_variable(name) for match in _POWERSHELL_ENV_RE.finditer(command) for name in match.groups() if name
    )


def _check_gate_commands(calls: Sequence[Invocation], call: ToolCall, context: PolicyContext) -> str | None:
    """`craftsman gate` as a hooked session may run it: no takeover of its own gate, no other session's id."""
    for item in calls:
        if item.kind != "cli" or _first_positional(item.args) != "gate":
            continue
        args = item.args
        if has_option(args, "--takeover", GATE_OPTIONS) and context.owns_gate():
            return OWNER_CANNOT_TAKE_OVER
        for value in option_values(args, "--session-id", GATE_OPTIONS):
            if call.session_id is None or value.strip() != call.session_id:
                return IDENTITY_IS_THE_PLATFORMS
    return None


def _check_lead_only(calls: Sequence[Invocation], call: ToolCall) -> str | None:
    """The main session records only as `lead`: `--hunter` of a `review cover|add`, or a `--from-json` entry's `hunter`."""
    for item in calls:
        args = item.args
        if item.kind != "cli" or len(args) < 2 or args[0] != "review" or args[1] not in ("cover", "add"):
            continue
        rest = args[2:]
        options = COVER_OPTIONS if args[1] == "cover" else ADD_OPTIONS
        if any(value != LEAD_HUNTER for value in option_values(rest, "--hunter", options)):
            return LEAD_ONLY
        sources = option_values(rest, "--from-json", options)
        if sources and any(name != LEAD_HUNTER for name in _written_hunters(call.command)):
            return LEAD_ONLY  # a batch the command line itself holds or writes: on stdin, or `printf ... > f && ... f`
        for source in sources:
            if any(name != LEAD_HUNTER for name in _batch_hunters(source, item.command.cwd or call.cwd)):
                return LEAD_ONLY
    return None


MAX_BATCH_BYTES = 4 * 1024 * 1024
# `"hunter": "W1-1"` as it stands in a command line: bare JSON, or inside shell quotes with the quotes escaped.
_HUNTER_ENTRY_RE = re.compile(r"""\\?["']hunter\\?["']\s*:\s*\\?["']([^"'\\]*)""")


def _written_hunters(command: str) -> list[str]:
    """The `hunter` of every JSON entry that the command line itself spells out (a heredoc, an `echo` into the pipe)."""
    return _HUNTER_ENTRY_RE.findall(command)


def _batch_hunters(source: str, cwd: str) -> list[str]:
    """The `hunter` of every entry of a `--from-json` file; empty when it cannot be read (the CLI reports that).

    The file is decoded the way the CLI decodes it (UTF-8 with or without a BOM, UTF-16).
    """
    if source == "-":
        return []
    try:
        path = Path(expand_shell_path_token(source))
        path = path if path.is_absolute() else Path(cwd) / path
        if path.stat().st_size > MAX_BATCH_BYTES:
            return []
        raw = json.loads(decode_json_bytes(path.read_bytes()))
    except (OSError, ValueError):  # a UnicodeDecodeError is a ValueError
        return []
    entries = raw if isinstance(raw, list) else [raw]
    return [
        entry["hunter"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("hunter"), str)
    ]


def _check_review_commands(subs: Sequence[str], call: ToolCall) -> str | None:
    """The `craftsman review` commands the caller must not run: a hunter or verifier re-planning or resetting
    the review, and the main session fetching a brief that belongs to a subagent."""
    if not subs:
        return None
    if call.role is not None:
        return NO_RESTART if RESTARTING_COMMANDS.intersection(subs) else None
    if call.main_session and "brief" in subs:
        return BRIEFS_ARE_FOR_SUBAGENTS
    return None


def _worktree_decision(call: ToolCall, context: PolicyContext) -> Decision:
    """The work-tree guard: subagents never change the work tree or the index; the main session is asked while a
    review-only round is open."""
    subagent = call.role is not None
    mutation = find_worktree_mutation(call.command, call.cwd, subagent=subagent)
    if mutation is None:
        return Decision()
    if subagent:
        return Decision(
            "deny",
            "Craftsman reviewers and verifiers must not change the work tree or the index "
            f"({mutation}). Read an old version with `git show <base>:<path>`; put reproductions "
            "in a temp directory.",
        )
    round_number = context.review_in_progress()
    if round_number is None:
        return Decision()
    return Decision(
        "ask",
        f"A Craftsman review is in progress (round {round_number}); {mutation} would change the "
        "files under review. Approve only if you mean it.",
    )


def _decide_shell(call: ToolCall, context: PolicyContext, places: ProtectedPlaces) -> Decision:
    if not call.command.strip():
        return Decision()
    commands = iter_simple_commands(call.command, call.cwd)
    calls = invocations(commands)
    subs = _review_subcommands(calls)
    denial = (
        HOOKS_ARE_THE_PLATFORMS if _forges_a_hook(calls, call.command) else None
    ) or _check_ledger_writes(commands, calls, places, call)
    if denial is None and _sets_identity(commands, calls, call.command):
        denial = IDENTITY_IS_THE_PLATFORMS
    denial = denial or _check_gate_commands(calls, call, context) or _check_review_commands(subs, call)
    if denial is None and call.main_session:
        denial = _check_lead_only(calls, call)
    if denial is not None:
        return Decision("deny", denial, subs)
    worktree = _worktree_decision(call, context)
    return Decision(worktree.action, worktree.reason, subs)


def _decide_write(call: ToolCall, places: ProtectedPlaces) -> Decision:
    for raw in call.paths:
        message = places.violation(raw, call.cwd)
        if message is not None:
            return Decision("deny", message)
    return Decision()


def decide(call: ToolCall, context: PolicyContext | None = None) -> Decision:
    """Allow, deny or ask for one tool call (see the module text for what is checked, and in which order)."""
    context = context or PolicyContext()
    if call.kind == "shell":
        return _decide_shell(call, context, ProtectedPlaces(context))
    if call.kind == "write" and call.paths:
        return _decide_write(call, ProtectedPlaces(context))
    return Decision()
