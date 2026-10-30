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
from dataclasses import dataclass

from craftsman.export import user_home

HEREDOC_OPEN_RE = re.compile(r"(?<!<)<<(?!<)-?\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))")
_REDIR_TARGET_PAT = r"[ \t]*(?:'[^']*'|\"[^\"]*\"|[^\s;&|()<>]*)"
UNQUOTED_REDIR_RE = re.compile(
    r"(?:(?:\d+)?[<>]&[ \t]*(?:\d+|-)"
    r"|(?:\d+)?(?:&>>?|>>?|>\|" + r"|<(?!<<|\())" + _REDIR_TARGET_PAT + r")"
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


def _flush_segment(current: list[str], events: list[str]) -> list[str]:
    seg = "".join(current).strip()
    if seg:
        events.append(seg)
    return []


def split_shell_events(command_line: str) -> list[str]:
    """Split a shell command line into command segments and subshell boundary tokens `(` and `)`."""
    events: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
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
            current = _flush_segment(current, events)
        elif ch in ("(", ")"):
            current = _flush_segment(current, events)
            events.append(ch)
            i += 1
        elif ch in (";", "\n", "`") or _is_standalone_brace(command_line, i):
            current = _flush_segment(current, events)
            i += 1
        elif ch in ("|", "&") and not _is_redirection_ampersand(command_line, i):
            i += 2 if (i + 1 < n and command_line[i + 1] == ch) else 1
            current = _flush_segment(current, events)
        else:
            current.append(ch)
            i += 1
    _flush_segment(current, events)
    return events


def _strip_wrappers_from_tokens(tokens: list[str]) -> list[str]:
    """Strip leading env assignments, shell control keywords, and command wrappers (`env`, `nice`, `sudo`, `timeout`, `xargs`, etc.)."""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok in SHELL_CONTROL_KEYWORDS:
            idx += 1
            continue
        if tok == "for" and idx + 2 < n and tokens[idx + 2] == "in":
            idx += 3
            while idx < n and tokens[idx] not in ("do",):
                idx += 1
            continue
        if "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            idx += 1
            continue
        exe = os.path.basename(tok)
        if exe in SIMPLE_WRAPPERS:
            opts_with_val = WRAPPER_OPTS_WITH_VALUE.get(exe, frozenset())
            idx += 1
            while idx < n:
                cur_tok = tokens[idx]
                if cur_tok == "--":
                    idx += 1
                    break
                if (
                    "=" in cur_tok
                    and not cur_tok.startswith("-")
                    and cur_tok.split("=", 1)[0].isidentifier()
                ):
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
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                opt = tokens[idx]
                if opt in TIMEOUT_OPTS_WITH_VALUE:
                    idx += 2
                else:
                    idx += 1
            # Skip duration argument (e.g. '30', '10s', '5m')
            if idx < n:
                idx += 1
            continue
        if exe == "xargs":
            idx += 1
            while idx < n and tokens[idx].startswith("-"):
                opt = tokens[idx]
                if opt in XARGS_OPTS_WITH_VALUE:
                    idx += 2
                else:
                    idx += 1
            continue
        break

    # Remove shell here-string operator '<<<' so `xargs git push origin <<< main` preserves 'main'
    return [t for t in tokens[idx:] if t != "<<<"]


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
    r"(?<!\S)(?:[A-Za-z]:|~|\.\.?|%[A-Za-z_][A-Za-z0-9_]*%|\$(?:HOME|\{HOME\}|env:[A-Za-z_][A-Za-z0-9_]*))\\[^\s\"';|&<>()]*"
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


def _tokenize_raw_segment(segment: str) -> list[str]:
    first_line = segment.split("\n", 1)[0]
    hd_match = HEREDOC_OPEN_RE.search(first_line)
    cmd_header = first_line[: hd_match.start()] if hd_match else segment
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


def expand_shell_path_token(raw: str) -> str:
    """Expand `~`, `$HOME`, `${HOME}`, `$env:HOME`, `$env:USERPROFILE`, and `%USERPROFILE%` in a shell argument."""
    home = str(user_home())
    expanded = re.sub(
        r"\$(?:HOME|\{HOME\}|env:USERPROFILE|env:HOME)(?=[/\\]|$)|%USERPROFILE%",
        lambda _: home,
        raw,
        flags=re.IGNORECASE,
    )
    if expanded == "~" or expanded.startswith("~/") or expanded.startswith("~\\"):
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


def _consume_wrapper_chdir_options(
    exe: str, raw_tokens: list[str], idx: int, eff_cwd: str | None
) -> tuple[int, str | None]:
    opts_with_val = WRAPPER_OPTS_WITH_VALUE.get(exe, frozenset())
    n = len(raw_tokens)
    while idx < n:
        cur_tok = raw_tokens[idx]
        if cur_tok == "--":
            return idx + 1, eff_cwd
        if "=" in cur_tok and not cur_tok.startswith("-") and cur_tok.split("=", 1)[0].isidentifier():
            idx += 1
        elif (
            (exe == "env" and cur_tok in ("-C", "--chdir"))
            or (exe == "sudo" and cur_tok in ("-D", "--chdir"))
        ) and idx + 1 < n:
            eff_cwd = _join_and_normpath(raw_tokens[idx + 1], eff_cwd)
            idx += 2
        elif exe in ("env", "sudo") and cur_tok.startswith("--chdir="):
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
    """Inspect leading `env`/`sudo` wrappers for `-C`, `-D`, or `--chdir` and return the effective cwd."""
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
        elif "=" in tok and not tok.startswith("-") and tok.split("=", 1)[0].isidentifier():
            idx += 1
        elif (exe := os.path.basename(tok)) in SIMPLE_WRAPPERS:
            idx, eff_cwd = _consume_wrapper_chdir_options(exe, raw_tokens, idx + 1, eff_cwd)
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


def _extract_find_or_fd_exec_commands(tokens: list[str]) -> list[list[str]]:
    """Extract subcommands passed to `find ... -exec/-execdir/-ok/-okdir` or `fd ... -x/--exec/-X/--exec-batch`."""
    if not tokens:
        return []
    exe = os.path.basename(tokens[0]).lower()
    subcommands: list[list[str]] = []
    if exe == "find":
        idx = 1
        n = len(tokens)
        while idx < n:
            if tokens[idx] in ("-exec", "-execdir", "-ok", "-okdir"):
                idx += 1
                sub: list[str] = []
                while idx < n and tokens[idx] not in (";", "\\;", "+"):
                    sub.append(tokens[idx])
                    idx += 1
                cleaned = _strip_wrappers_from_tokens(sub)
                if cleaned:
                    subcommands.append(cleaned)
            idx += 1
    elif exe in ("fd", "fdfind"):
        idx = 1
        n = len(tokens)
        while idx < n:
            if tokens[idx] in ("-x", "--exec", "-X", "--exec-batch"):
                idx += 1
                sub = []
                while idx < n and tokens[idx] not in (";", "\\;"):
                    sub.append(tokens[idx])
                    idx += 1
                cleaned = _strip_wrappers_from_tokens(sub)
                if cleaned:
                    subcommands.append(cleaned)
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
    for sub_tokens in _extract_find_or_fd_exec_commands(tokens):
        sub_seg = " ".join(sub_tokens)
        out.append(SimpleCommand(segment=sub_seg, tokens=sub_tokens, cwd=cmd_cwd))
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

    for ev in split_shell_events(command_line):
        if ev == "(":
            subshell_stack.append((cur_cwd, list(dir_stack)))
            continue
        if ev == ")":
            if subshell_stack:
                cur_cwd, dir_stack = subshell_stack.pop()
            continue
        raw_tokens = _tokenize_raw_segment(ev)
        cmd_cwd = _extract_wrapper_chdir(raw_tokens, cur_cwd)
        tokens = _strip_wrappers_from_tokens(raw_tokens)
        commands.append(SimpleCommand(segment=ev, tokens=tokens, cwd=cmd_cwd))
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
