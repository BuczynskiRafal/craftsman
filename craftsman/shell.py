"""Shell command-line lexer and simple-command expander (`craftsman/shell.py`).

Splits POSIX and PowerShell command lines into tokenized segments, tracking working-directory
changes (`cd`, `pushd`/`popd`, `env -C`, `sudo -D`), subshell `( ... )` boundaries, nested shell
invocations (`sh -c`, `eval`, `find -exec`, `fd -x`), heredocs, PowerShell here-strings, and `git`
global options/aliases for read-only work-tree and search-scope guards.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from craftsman.paths import user_home

HEREDOC_OPEN_RE = re.compile(
    r"""
    (?<!<) << (?!<)                     # `<<` heredoc operator (not `<` or `<<<` here-string)
    -? \s*                              # optional `-` tab-stripping modifier and whitespace
    (?:
        '([^']+)'                       # single-quoted delimiter (group 1)
        | "([^"]+)"                     # double-quoted delimiter (group 2)
        | ([A-Za-z_][A-Za-z0-9_]*)      # bare identifier delimiter (group 3)
    )
    """,
    re.VERBOSE,
)
_REDIR_TARGET_PAT = r"""
    [ \t]*                              # optional horizontal whitespace before target
    (?:
        '[^']*'                         # single-quoted path
        | "[^"]*"                       # double-quoted path
        | [^\s;&|()<>]*                 # unquoted non-operator token
    )
"""
UNQUOTED_REDIR_RE = re.compile(
    r"""
    (?:
        (?:\d+)? [<>]& [ \t]* (?:\d+|-)             # fd duplication or close, e.g. `2>&1`, `<&-`
        | (?:\d+)?                                  # optional source file descriptor
          (?:
              &>>?                                  # bash combined stdout/stderr redirect (`&>`, `&>>`)
              | >\|                                 # noclobber override (`>|`); before `>` so the `|` is not left behind
              | >>?                                 # stdout write or append (`>`, `>>`)
              | < (?! << | \( )                     # stdin redirect `<` (excluding `<<<` and `<(...)`)
          )
    """
    + _REDIR_TARGET_PAT
    + r"""
    )
    """,
    re.VERBOSE,
)

GIT_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)

SHELL_EXECUTABLES = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
SIMPLE_WRAPPERS = frozenset(
    {"env", "command", "sudo", "time", "nohup", "nice", "stdbuf", "exec", "builtin"}
)
SHELL_CONTROL_KEYWORDS = frozenset({"if", "then", "elif", "else", "fi", "do", "done", "while", "until"})
WRAPPER_OPTS_WITH_VALUE: dict[str, frozenset[str]] = {
    "nice": frozenset({"-n", "--adjustment"}),
    "sudo": frozenset(
        {
            "-u",
            "-g",
            "-C",
            "-D",
            "-R",
            "-T",
            "-h",
            "-p",
            "-r",
            "-t",
            "--user",
            "--group",
            "--chdir",
            "--prompt",
        }
    ),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "command": frozenset(),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "builtin": frozenset(),
}
TIMEOUT_OPTS_WITH_VALUE = frozenset({"-k", "-s", "--kill-after", "--signal"})
XARGS_OPTS_WITH_VALUE = frozenset(
    {"-n", "-L", "-I", "-P", "-d", "-E", "-a", "--max-args", "--max-procs"}
)


def _consume_heredoc_body(command_line: str, start: int, delim: str) -> tuple[str, int]:
    i, n = start, len(command_line)
    chunks: list[str] = []
    while i < n:
        line_end = command_line.find("\n", i)
        if line_end == -1:
            chunks.append(command_line[i:])
            return "".join(chunks), n
        line_str = command_line[i : line_end + 1]
        chunks.append(line_str)
        i = line_end + 1
        if line_str.strip() == delim:
            break
    return "".join(chunks), i


_PS_HERESTRING_OPEN_BOUNDARIES = (" ", "\t", "\n", "\r", "(", "=", ";", "|", "&", "+", ",", "[", "{")


def _consume_ps_herestring_token(command_line: str, idx: int) -> tuple[str, int] | None:
    """Consume a PowerShell `@'\\n...\\n'@` or `@\"\\n...\\n\"@` here-string starting at `idx`."""
    if (
        command_line[idx] != "@"
        or (idx > 0 and command_line[idx - 1] not in _PS_HERESTRING_OPEN_BOUNDARIES)
        or idx + 2 >= len(command_line)
        or command_line[idx + 1] not in ("'", '"')
    ):
        return None
    quote_char = command_line[idx + 1]
    nl_len = 2 if command_line[idx + 2 : idx + 4] == "\r\n" else int(command_line[idx + 2] == "\n")
    if nl_len == 0:
        return None
    close_token = f"{quote_char}@"
    i, n = idx + 2 + nl_len, len(command_line)
    chunks: list[str] = []
    while i < n:
        line_end = command_line.find("\n", i)
        raw_line = command_line[i:] if line_end == -1 else command_line[i:line_end]
        if raw_line.startswith(close_token):
            body = "".join(chunks).removesuffix("\n").removesuffix("\r")
            escaped_body = body.replace("'", "'\\''")
            return f"'{escaped_body}'", i + len(close_token)
        if line_end == -1:
            break
        chunks.append(command_line[i : line_end + 1])
        i = line_end + 1
    unclosed = command_line[idx + 2 + nl_len :].replace("'", "'\\''")
    return f"'{unclosed}", n


def _is_standalone_brace(command_line: str, idx: int) -> bool:
    if command_line[idx] not in ("{", "}"):
        return False
    n = len(command_line)
    sep = (" ", "\t", "\n", ";", "(", ")", "|", "&", "`")
    prev_ok = idx == 0 or command_line[idx - 1] in sep
    next_ok = idx + 1 == n or command_line[idx + 1] in sep
    return prev_ok and next_ok


def _is_redirection_ampersand(command_line: str, idx: int) -> bool:
    n = len(command_line)
    return command_line[idx] == "&" and (
        (idx > 0 and command_line[idx - 1] in (">", "<"))
        or (idx + 1 < n and command_line[idx + 1] == ">")
    )


class ShellEvent(NamedTuple):
    """A command segment, or a subshell boundary (`(` or `)`), of a command line."""

    text: str
    piped: bool  # a segment after an unquoted `|` (or `|&`, not `||` and not `>|`): its stdin is the previous command's output


class _Events:
    """The events of a command line being split; a pipe makes the next SEGMENT `piped` (boundaries and empty
    flushes, such as the `&` of `|&` or a newline after the pipe, keep it pending)."""

    def __init__(self) -> None:
        self.items: list[ShellEvent] = []
        self._pipe_pending = False

    def boundary(self, text: str) -> None:
        self.items.append(ShellEvent(text, False))

    def pipe(self) -> None:
        self._pipe_pending = True

    def flush(self, current: list[str]) -> list[str]:
        seg = "".join(current).strip()
        if seg:
            self.items.append(ShellEvent(seg, self._pipe_pending))
            self._pipe_pending = False
        return []


def split_shell_events(command_line: str) -> list[str]:
    """Split a shell command line into command segments and subshell boundary tokens `(` and `)`."""
    return [event.text for event in scan_shell_events(command_line)]


def scan_shell_events(command_line: str) -> list[ShellEvent]:
    """`split_shell_events`, with the fact whether a segment reads the output of the one before it through a pipe."""
    events = _Events()
    current: list[str] = []
    quote: str | None = None
    escaped = False
    after_redirect = -1  # the index right after the last unquoted `>`: a `|` there is `>|`, not a pipe
    i, n = 0, len(command_line)
    while i < n:
        ch = command_line[i]
        if escaped or ch == "\\":
            escaped = (not escaped) and ch == "\\" and quote != "'"
            current.append(ch)
            i += 1
        elif quote is None and (hs := _consume_ps_herestring_token(command_line, i)) is not None:
            current.append(hs[0])
            i = hs[1]
        elif quote or ch in ("'", '"'):
            quote = None if quote == ch else (ch if quote is None else quote)
            current.append(ch)
            i += 1
        elif ch == "\n" and (hd := HEREDOC_OPEN_RE.search("".join(current))):
            delim = hd.group(1) or hd.group(2) or hd.group(3)
            body, i = _consume_heredoc_body(command_line, i + 1, delim)
            current.extend(("\n", body))
            current = events.flush(current)
        elif ch in ("(", ")"):
            current = events.flush(current)
            events.boundary(ch)
            i += 1
        elif ch in (";", "\n", "`") or _is_standalone_brace(command_line, i):
            current = events.flush(current)
            i += 1
        elif ch == "|" and i == after_redirect:
            current.append(ch)
            i += 1
        elif ch in ("|", "&") and not _is_redirection_ampersand(command_line, i):
            doubled = i + 1 < n and command_line[i + 1] == ch
            i += 2 if doubled else 1
            current = events.flush(current)
            if ch == "|" and not doubled:
                events.pipe()
        else:
            if ch == ">":
                after_redirect = i + 1
            current.append(ch)
            i += 1
    events.flush(current)
    return events.items


def is_env_assignment(token: str) -> bool:
    """A leading `NAME=value` that sets a variable for the command, e.g. `CI=1 pytest`."""
    return "=" in token and not token.startswith("-") and token.split("=", 1)[0].isidentifier()


# `env` options that consume a value: -u NAME, -C DIR, -S STRING, -a ARGV0 (BSD: -P PATH) and their long
# forms. GNU `env` accepts any unique prefix of a long option, and the first letter of these four is unique
# among its long options (`--debug`, `--default-signal`, `--ignore-*`, `--block-signal`, `--list-signal-handling`,
# `--null`, `--help`, `--version` do not start with a, c, s or u), so `--s`, `--sp` and `--split` are
# `--split-string` too. The shortest accepted spelling is the two dashes and one letter.
ENV_SHORT_VALUE_OPTIONS = frozenset("uCSaP")
_ENV_LONG_VALUE_OPTIONS = (("--unset", "u"), ("--chdir", "C"), ("--split-string", "S"), ("--argv0", "a"))
_ENV_LONG_MIN_LENGTH = 3


def split_env_string(value: str) -> list[str]:
    """The words of an `env -S` string: shell-split, or the raw value when its quotes do not balance. `\\_`
    separates words in `env -S` (shlex would glue it into one), so it is read as a space: splitting more
    than `env` does only shows more of the command."""
    try:
        return shlex.split(_preserve_unquoted_windows_backslashes(value.replace("\\_", " ")))
    except ValueError:  # env rejects it too, so keep it as one opaque program
        return [value]


def _env_long_option(name: str) -> str | None:
    if len(name) < _ENV_LONG_MIN_LENGTH:
        return None
    for full, letter in _ENV_LONG_VALUE_OPTIONS:
        if full.startswith(name):
            return letter
    return None


class EnvOption(NamedTuple):
    """One option word of `env` that takes a value."""

    letter: str  # "u", "C", "S", "a" or "P"; "" when the word is another option
    taken: int  # how many of the following words the value is (0: attached to the option)
    words: list[str]  # for `-S`: the words its value stands for, else []


def read_env_option(token: str, following: Sequence[str]) -> EnvOption:
    """Read the `env` option word `token` (it starts with `-`); `following` is the word after it."""
    if token.startswith("--"):
        name, equals, value = token.partition("=")
        has_value = bool(equals)
        letter = _env_long_option(name)
        if letter is None:
            return EnvOption("", 0, [])
    else:
        flags = token[1:]
        index = next((i for i, flag in enumerate(flags) if flag in ENV_SHORT_VALUE_OPTIONS), None)
        if index is None:
            return EnvOption("", 0, [])
        letter, value = flags[index], flags[index + 1 :]
        has_value = bool(value)
    taken = 0
    if not has_value:
        taken = 1 if following else 0
        value = following[0] if following else ""
    return EnvOption(letter, taken, split_env_string(value) if letter == "S" else [])


def _skip_env_options(tokens: list[str], idx: int) -> int:
    """The index after the options and assignments of an `env` whose name sits before `idx`. The words of a
    `-S` string replace the option in `tokens`, so the caller reads them as the rest of the command line."""
    while idx < len(tokens):
        word = tokens[idx]
        if word == "--":
            return idx + 1
        if is_env_assignment(word):
            idx += 1
        elif word.startswith("-"):
            option = read_env_option(word, tokens[idx + 1 : idx + 2])
            if option.letter == "S":
                tokens[idx : idx + 1 + option.taken] = option.words
            else:
                idx += 1 + option.taken
        else:
            break
    return idx


def _skip_options(tokens: list[str], idx: int, opts_with_value: frozenset[str]) -> int:
    """The index after the `-` options starting at `idx`, each option in `opts_with_value` with its value."""
    while idx < len(tokens) and tokens[idx].startswith("-"):
        idx += 2 if tokens[idx] in opts_with_value else 1
    return idx


def _split_wrappers(words: list[str]) -> tuple[list[str], list[str]]:
    """The wrapper words in front of a command, and the command. The words of an `env -S` string stand in
    the place of the option, so they are part of the prefix (assignments) or of the command (the program)."""
    tokens = list(words)
    idx = 0
    while idx < len(tokens):
        n = len(tokens)
        tok = tokens[idx]
        if tok in SHELL_CONTROL_KEYWORDS:
            idx += 1
            continue
        if tok == "for" and idx + 2 < n and tokens[idx + 2] == "in":
            idx += 3
            while idx < n and tokens[idx] != "do":
                idx += 1
            continue
        if is_env_assignment(tok):
            idx += 1
            continue
        exe = os.path.basename(tok)
        if exe == "env":
            idx = _skip_env_options(tokens, idx + 1)
            continue
        if exe in SIMPLE_WRAPPERS:
            opts_with_val = WRAPPER_OPTS_WITH_VALUE.get(exe, frozenset())
            idx += 1
            while idx < n:
                cur_tok = tokens[idx]
                if cur_tok == "--":
                    idx += 1
                    break
                if is_env_assignment(cur_tok):
                    idx += 1
                    continue
                if cur_tok in opts_with_val:
                    idx += 2
                    continue
                if cur_tok.startswith("-"):
                    idx += 1
                    continue
                break
            continue
        if exe == "timeout":
            idx = _skip_options(tokens, idx + 1, TIMEOUT_OPTS_WITH_VALUE)
            # Skip duration argument (e.g. '30', '10s', '5m')
            if idx < n:
                idx += 1
            continue
        if exe == "xargs":
            idx = _skip_options(tokens, idx + 1, XARGS_OPTS_WITH_VALUE)
            continue
        break

    # Remove shell here-string operator '<<<' so `xargs git push origin <<< main` preserves 'main'
    return tokens[:idx], [t for t in tokens[idx:] if t != "<<<"]


def _separate_unquoted_redirections(cmd_header: str) -> str:
    """Insert spaces before unquoted redirection operators (`>/dev/null`, `2>&1`) without altering quoted strings."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(cmd_header)
    while i < n:
        ch = cmd_header[i]
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = quote != "'"
            out.append(ch)
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if cmd_header[i:].startswith("<<<"):
            out.append(" <<< ")
            i += 3
            continue
        # Check for unquoted redirections (`2>&1`, `&>/path`, `>/path`, `> /path`, `2>/path`, `< /path`)
        if ch in ("<", ">", "&") or ch.isdigit():
            redir_m = UNQUOTED_REDIR_RE.match(cmd_header, i)
            if redir_m and (
                not ch.isdigit()
                or i == 0
                or cmd_header[i - 1].isspace()
                or cmd_header[i - 1] in (";", "|", "&", "(")
            ):
                i = redir_m.end()
                out.append(" ")
                continue
        out.append(ch)
        i += 1
    return "".join(out)


_UNQUOTED_WIN_PATH_TOKEN_RE = re.compile(
    r"""
    (?<!\S)                                     # token start (BOL or preceded by whitespace)
    (?:                                         # optionally glued to an option or assignment:
        --[A-Za-z][A-Za-z0-9-]*=                #   `--output=C:\x`
        | [A-Za-z_][A-Za-z0-9_]*=               #   `of=C:\x` (dd), `VAR=C:\x`
        | -[A-Za-z]                             #   `-oC:\x`
    )?
    (?:
        [A-Za-z]:                               # Windows drive prefix, e.g. `C:`
        | ~                                     # home-directory tilde prefix, e.g. `~\dir`
        | \.\.?                                 # relative `.` or `..` prefix, e.g. `..\dir`
        | %[A-Za-z_][A-Za-z0-9_]*%              # cmd.exe `%VAR%` environment prefix
        | \$(?:                                 # POSIX / PowerShell home or env prefix:
            HOME                                #   `$HOME\dir`
            | \{HOME\}                          #   `${HOME}\dir`
            | env:[A-Za-z_][A-Za-z0-9_]*        #   `$env:USERPROFILE\dir`
          )
    )
    \\ [^\s"';|&<>()]*                          # backslash path separator and unquoted tail
    """,
    re.VERBOSE,
)
CD_COMMANDS = frozenset({"cd", "pushd", "chdir", "set-location", "sl"})
POPD_COMMANDS = frozenset({"popd", "pop-location"})


def _preserve_unquoted_windows_backslashes(cmd_header: str) -> str:
    """Double backslashes inside unquoted Windows/tilde/env path tokens before `shlex.split(posix=True)`."""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(cmd_header)
    while i < n:
        ch = cmd_header[i]
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch if quote is None else (None if quote == ch else quote)
            out.append(ch)
            i += 1
            continue
        if quote is None:
            m = _UNQUOTED_WIN_PATH_TOKEN_RE.match(cmd_header, i)
            if m and (
                i == 0 or cmd_header[i - 1].isspace() or cmd_header[i - 1] in (";", "|", "&", "(")
            ):
                out.append(m.group(0).replace("\\", "\\\\"))
                i = m.end()
                continue
        if ch == "\\":
            escaped = quote != "'"
        out.append(ch)
        i += 1
    return "".join(out)


def redirection_targets(segment: str) -> list[str]:
    """The files the unquoted `>`, `>>`, `>|`, `&>` and `&>>` redirections of a simple command write to.

    Only the command's first line is read (a heredoc body follows it), with each heredoc operator and its delimiter
    blanked so that a redirection on either side of `<<EOF` is found; quoted text is never a redirection.
    Descriptor duplications (`2>&1`) and input redirections write nothing and are left out.
    """
    header = _blank_heredoc_openers(segment.split("\n", 1)[0])
    targets: list[str] = []
    quote: str | None = None
    escaped = False
    i, n = 0, len(header)
    while i < n:
        ch = header[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = quote != "'"
        elif quote:
            quote = None if ch == quote else quote
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "<>&" or ch.isdigit():
            match = UNQUOTED_REDIR_RE.match(header, i)
            starts_word = i == 0 or header[i - 1].isspace() or header[i - 1] in ";|&("
            if match and (not ch.isdigit() or starts_word):
                target = _write_target(match.group(0))
                if target:
                    targets.append(target)
                i = match.end()
                continue
        i += 1
    return targets


def _blank_heredoc_openers(line: str) -> str:
    """`line` with every heredoc operator and its delimiter word (`<<'EOF'`, `<<-EOF`) replaced by spaces."""
    return HEREDOC_OPEN_RE.sub(lambda match: " " * len(match.group(0)), line)


def _write_target(redirection: str) -> str | None:
    """The file a matched redirection writes to, None when it is not a file write."""
    text = redirection.lstrip("0123456789")
    for operator in ("&>>", "&>", ">>", ">|", ">"):
        if text.startswith(operator):
            rest = text[len(operator) :].strip()
            break
    else:
        return None
    if rest.startswith("&") and operator in (">>", ">|", ">"):
        return None  # `>&2`, `>&-`: a descriptor, not a file
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
        rest = rest[1:-1]
    return rest or None


def _tokenize_raw_segment(segment: str) -> list[str]:
    first_line = segment.split("\n", 1)[0]
    cmd_header = _blank_heredoc_openers(first_line) if HEREDOC_OPEN_RE.search(first_line) else segment
    cmd_header = _separate_unquoted_redirections(cmd_header)
    cmd_header = _preserve_unquoted_windows_backslashes(cmd_header)
    try:
        return shlex.split(cmd_header, posix=True)
    except ValueError:
        return cmd_header.split()


def _extract_nested_shell_command(tokens: list[str], raw_segment: str = "") -> str | None:
    """If `tokens` invoke `bash -c '...'`, `bash <<EOF`, or `eval ...`, return the inner command string."""
    if not tokens:
        return None
    exe = os.path.basename(tokens[0])
    if exe == "eval" and len(tokens) > 1:
        return " ".join(tokens[1:])
    if exe in SHELL_EXECUTABLES:
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "-c" and i + 1 < len(tokens):
                return tokens[i + 1]
            if (
                tok.startswith("-")
                and not tok.startswith("--")
                and "c" in tok[1:]
                and i + 1 < len(tokens)
            ):
                return tokens[i + 1]
            if tok.startswith("-"):
                i += 1
                continue
            break
        if "\n" in raw_segment and HEREDOC_OPEN_RE.search(raw_segment.split("\n", 1)[0]):
            body = raw_segment.split("\n", 1)[1]
            body_lines = body.splitlines()
            if body_lines:
                return "\n".join(body_lines[:-1])
    return None


@dataclass(frozen=True)
class SimpleCommand:
    """A single tokenized shell segment with its effective working directory."""

    segment: str
    tokens: list[str]
    cwd: str | None
    # The wrapper words that were stripped (`env`, `VAR=x`, `-u VAR`, the words of an `env -S` string, ...).
    prefix: tuple[str, ...] = ()
    # Its stdin is the output of the command before it (`scan_shell_events`): it follows an unquoted `|`.
    piped: bool = False


def expand_shell_path_token(raw: str) -> str:
    """Expand `~`, `$HOME`, `${HOME}`, `$env:HOME`, `$env:USERPROFILE`, and `%USERPROFILE%` in a shell argument."""
    home = str(user_home())
    expanded = re.sub(
        r"\$(?:HOME|\{HOME\}|env:USERPROFILE|env:HOME)(?=[/\\]|$)|%USERPROFILE%",
        lambda _: home,
        raw,
        flags=re.IGNORECASE,
    )
    if expanded == "~" or expanded.startswith(("~/", "~\\")):
        return home + expanded[1:]
    return os.path.expanduser(expanded)


def _join_and_normpath(target: str, current_cwd: str | None) -> str:
    expanded = expand_shell_path_token(target)
    if os.sep == "/" and not re.match(r"^[A-Za-z]:[/\\]", expanded):
        expanded = expanded.replace("\\", "/")
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    if current_cwd:
        return os.path.normpath(os.path.join(current_cwd, expanded))
    return os.path.normpath(expanded)


def _attached_env_value(token: str) -> str:
    """The value written into the option word itself: `-C.git`, `-iC.git`, `--chdir=.git`."""
    if token.startswith("--"):
        return token.partition("=")[2]
    flags = token[1:]
    index = next(i for i, flag in enumerate(flags) if flag in ENV_SHORT_VALUE_OPTIONS)
    return flags[index + 1 :]


def _consume_wrapper_chdir_options(
    exe: str, raw_tokens: list[str], idx: int, eff_cwd: str | None
) -> tuple[int, str | None]:
    opts_with_val = WRAPPER_OPTS_WITH_VALUE.get(exe, frozenset())
    n = len(raw_tokens)
    while idx < n:
        cur_tok = raw_tokens[idx]
        if cur_tok == "--":
            return idx + 1, eff_cwd
        if is_env_assignment(cur_tok):
            idx += 1
        elif exe == "env" and cur_tok.startswith("-"):
            option = read_env_option(cur_tok, raw_tokens[idx + 1 : idx + 2])
            if option.letter == "C":
                value = raw_tokens[idx + 1] if option.taken else _attached_env_value(cur_tok)
                eff_cwd = _join_and_normpath(value, eff_cwd)
            idx += 1 + option.taken
        elif (exe == "sudo" and cur_tok in ("-D", "--chdir")) and idx + 1 < n:
            eff_cwd = _join_and_normpath(raw_tokens[idx + 1], eff_cwd)
            idx += 2
        elif exe == "sudo" and cur_tok.startswith("--chdir="):
            eff_cwd = _join_and_normpath(cur_tok.split("=", 1)[1], eff_cwd)
            idx += 1
        elif cur_tok in opts_with_val:
            idx += 2
        elif cur_tok.startswith("-"):
            idx += 1
        else:
            break
    return idx, eff_cwd


def _extract_wrapper_chdir(raw_tokens: list[str], current_cwd: str | None) -> str | None:
    """Inspect the leading wrapper words (the prefix of a command) for `env -C`, `sudo -D` or `--chdir` and
    return the effective cwd."""
    eff_cwd = current_cwd
    idx, n = 0, len(raw_tokens)
    while idx < n:
        tok = raw_tokens[idx]
        if tok in SHELL_CONTROL_KEYWORDS:
            idx += 1
        elif tok == "for" and idx + 2 < n and raw_tokens[idx + 2] == "in":
            idx += 3
            while idx < n and raw_tokens[idx] != "do":
                idx += 1
        elif is_env_assignment(tok):
            idx += 1
        elif (exe := os.path.basename(tok)) in SIMPLE_WRAPPERS:
            idx, eff_cwd = _consume_wrapper_chdir_options(exe, raw_tokens, idx + 1, eff_cwd)
        elif exe == "timeout":  # the options, then the duration: `timeout 5 env -C .git rm x`
            idx = _skip_options(raw_tokens, idx + 1, TIMEOUT_OPTS_WITH_VALUE) + 1
        elif exe == "xargs":
            idx = _skip_options(raw_tokens, idx + 1, XARGS_OPTS_WITH_VALUE)
        else:
            break
    return eff_cwd


def _resolve_cd_target(tokens: list[str], current_cwd: str | None) -> str | None:
    """Return the updated working directory after a `cd`, `pushd`, `chdir`, or `Set-Location`/`sl` command."""
    if not tokens or tokens[0].lower() not in CD_COMMANDS:
        return current_cwd
    cmd = tokens[0].lower()
    args: list[str] = []
    idx = 1
    while idx < len(tokens):
        tok = tokens[idx]
        low = tok.lower()
        if tok == "--":
            args.extend(tokens[idx + 1 :])
            break
        if low in ("-path", "-literalpath") and idx + 1 < len(tokens):
            args.append(tokens[idx + 1])
            idx += 2
            continue
        if tok.startswith("-") and tok != "-":
            idx += 1
            continue
        args.append(tok)
        idx += 1

    if not args:
        if cmd in ("cd", "chdir", "set-location", "sl"):
            return expand_shell_path_token("~")
        return current_cwd

    target = args[0]
    if target == "-":
        return current_cwd

    return _join_and_normpath(target, current_cwd)


_FIND_EXEC = (("-exec", "-execdir", "-ok", "-okdir"), (";", "\\;", "+"))
_FD_EXEC = (("-x", "--exec", "-X", "--exec-batch"), (";", "\\;"))
# Per executable: the options that start a subcommand, and the tokens that end it.
_EXEC_OPTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "find": _FIND_EXEC,
    "fd": _FD_EXEC,
    "fdfind": _FD_EXEC,
}


def _extract_find_or_fd_exec_commands(tokens: list[str]) -> list[tuple[list[str], list[str]]]:
    """Extract (wrapper words, command) of the subcommands passed to `find ... -exec/-execdir/-ok/-okdir` or `fd ... -x/--exec/-X/--exec-batch`."""
    if not tokens:
        return []
    exec_options = _EXEC_OPTIONS.get(os.path.basename(tokens[0]).lower())
    if exec_options is None:
        return []
    starters, terminators = exec_options
    subcommands: list[tuple[list[str], list[str]]] = []
    idx = 1
    n = len(tokens)
    while idx < n:
        if tokens[idx] in starters:
            idx += 1
            sub: list[str] = []
            while idx < n and tokens[idx] not in terminators:
                sub.append(tokens[idx])
                idx += 1
            prefix, cleaned = _split_wrappers(sub)
            if cleaned:
                subcommands.append((prefix, cleaned))
        idx += 1
    return subcommands


def _expand_nested_commands(
    tokens: list[str], seg: str, cmd_cwd: str | None, max_depth: int
) -> list[SimpleCommand]:
    if max_depth <= 0:
        return []
    out: list[SimpleCommand] = []
    nested = _extract_nested_shell_command(tokens, seg)
    if nested:
        out.extend(iter_simple_commands(nested, cwd_str=cmd_cwd, max_depth=max_depth - 1))
    for sub_prefix, sub_tokens in _extract_find_or_fd_exec_commands(tokens):
        sub_seg = " ".join(sub_tokens)
        out.append(SimpleCommand(segment=sub_seg, tokens=sub_tokens, cwd=cmd_cwd, prefix=tuple(sub_prefix)))
        sub_nested = _extract_nested_shell_command(sub_tokens, sub_seg)
        if sub_nested:
            out.extend(iter_simple_commands(sub_nested, cwd_str=cmd_cwd, max_depth=max_depth - 1))
    return out


def iter_simple_commands(
    command_line: str, cwd_str: str | None = None, max_depth: int = 4
) -> list[SimpleCommand]:
    """Split a shell command line into tokenized segments, tracking `cd`/`pushd`/`popd`, subshell `( ... )` scope, and expanding nested shells."""
    commands: list[SimpleCommand] = []
    cur_cwd = cwd_str
    dir_stack: list[str | None] = []
    subshell_stack: list[tuple[str | None, list[str | None]]] = []

    for event in scan_shell_events(command_line):
        ev = event.text
        if ev == "(":
            subshell_stack.append((cur_cwd, list(dir_stack)))
            continue
        if ev == ")":
            if subshell_stack:
                cur_cwd, dir_stack = subshell_stack.pop()
            continue
        raw_tokens = _tokenize_raw_segment(ev)
        prefix, tokens = _split_wrappers(raw_tokens)
        cmd_cwd = _extract_wrapper_chdir(prefix, cur_cwd)
        commands.append(SimpleCommand(segment=ev, tokens=tokens, cwd=cmd_cwd, prefix=tuple(prefix), piped=event.piped))
        commands.extend(_expand_nested_commands(tokens, ev, cmd_cwd, max_depth))
        if tokens:
            cmd_low = tokens[0].lower()
            if cmd_low in CD_COMMANDS:
                if cmd_low == "pushd":
                    dir_stack.append(cur_cwd)
                cur_cwd = _resolve_cd_target(tokens, cur_cwd)
            elif cmd_low in POPD_COMMANDS and dir_stack:
                cur_cwd = dir_stack.pop()
    return commands


def _record_git_config_alias(config_expr: str, aliases: dict[str, list[str]]) -> None:
    if "=" not in config_expr:
        return
    key, val = config_expr.split("=", 1)
    key = key.strip()
    val = val.strip()
    if key.lower().startswith("alias.") and val:
        alias_name = key[6:].strip()
        if not alias_name:
            return
        if val.startswith("!"):
            val = val[1:].strip()
            if val.startswith("git "):
                val = val[4:].strip()
        try:
            alias_tokens = shlex.split(val, posix=True)
        except ValueError:
            alias_tokens = val.split()
        if alias_tokens:
            aliases[alias_name] = alias_tokens


def parse_git_invocation(
    tokens: list[str], default_cwd: str | None
) -> tuple[str | None, list[str], str | None]:
    """If tokens represent a `git` command, return (subcommand, sub_args, effective_cwd)."""
    if not tokens:
        return None, [], default_cwd
    exe = os.path.basename(tokens[0])
    if exe != "git":
        return None, [], default_cwd

    cwd = default_cwd
    aliases: dict[str, list[str]] = {}
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in GIT_GLOBAL_OPTS_WITH_VALUE:
            if tok == "-C" and i + 1 < len(tokens):
                cwd = tokens[i + 1]
            elif tok == "-c" and i + 1 < len(tokens):
                _record_git_config_alias(tokens[i + 1], aliases)
            i += 2
            continue
        if any(tok.startswith(f"{opt}=") for opt in GIT_GLOBAL_OPTS_WITH_VALUE):
            if tok.startswith("-C="):
                cwd = tok.split("=", 1)[1]
            elif tok.startswith("-c="):
                _record_git_config_alias(tok.split("=", 1)[1], aliases)
            i += 1
            continue
        if tok.startswith("-C") and len(tok) > 2:
            cwd = tok[2:]
            i += 1
            continue
        if tok.startswith("-c") and len(tok) > 2:
            _record_git_config_alias(tok[2:], aliases)
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        sub = tok
        sub_args = tokens[i + 1 :]
        if sub in aliases:
            resolved = aliases[sub]
            sub = resolved[0]
            sub_args = resolved[1:] + sub_args
        if sub == "subtree" and sub_args and sub_args[0] == "push":
            sub = "push"
            sub_args = sub_args[1:]
        return sub, sub_args, cwd

    return None, [], cwd


# Python options that take a value (as the rest of their cluster or as the next word).
PYTHON_OPTIONS_WITH_VALUE = frozenset("cmWX")
PYTHON_LONG_OPTIONS_WITH_VALUE = frozenset({"--check-hash-based-pycs"})


def python_program(args: Sequence[str]) -> tuple[str, str, Sequence[str]]:
    """What `python <args>` is asked to run, read the way the interpreter reads its options: `("code", program, ())`
    for `-c`, `("module", name, arguments)` for `-m`, `("script", path, arguments)`, else `("stdin", "", ())`.

    A short option cluster is read letter by letter (`-um`, `-Im`, `-Bc`), and `-c`, `-m`, `-W` and `-X` take the rest
    of the cluster or the next word as their value, so `-W ignore -m craftsman.hooks` still names the module.
    """
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if word == "--":
            return ("script", args[index], args[index + 1 :]) if index < len(args) else ("stdin", "", ())
        if word == "-":
            return "stdin", "", ()
        if word.startswith("--"):
            index += word in PYTHON_LONG_OPTIONS_WITH_VALUE
            continue
        if not word.startswith("-"):
            return "script", word, args[index:]
        for position, flag in enumerate(word[1:], start=1):
            if flag not in PYTHON_OPTIONS_WITH_VALUE:
                continue
            value = word[position + 1 :]
            if not value:
                value = args[index] if index < len(args) else ""
                index += 1
            if flag == "c":
                return "code", value, ()
            if flag == "m":
                return "module", value, args[index:]
            break
    return "stdin", "", ()
