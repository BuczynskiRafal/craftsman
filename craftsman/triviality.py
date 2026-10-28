"""Recognize commands that prove nothing, such as `true`, `echo ok` or `sh -c 'exit 0'`.

A recorded run only counts as verification when its command exercises code; these checks see
through `env` prefixes, interpreter flags and inline shell or Python scripts.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Sequence

_TRIVIAL_PROGRAMS = frozenset({"true", ":", "exit", "echo", "printf"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")"})
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?|py")
_PYTHON_EXITS = frozenset({"exit", "quit"})
# `env` options that consume a value: -u NAME, -C DIR, -S STRING and their long forms.
_ENV_SHORT_VALUE_OPTIONS = frozenset("uCS")
_ENV_LONG_VALUE_OPTIONS = {"--unset": "u", "--chdir": "C", "--split-string": "S"}


def is_trivial_run(argv: Sequence[str]) -> bool:
    """True for commands that prove nothing, such as `true`, `echo ok` or `sh -c 'exit 0'`."""
    tokens = command_without_env(argv)
    if not tokens:
        return True
    program = _program_name(tokens[0])
    if program in _TRIVIAL_PROGRAMS:
        return True
    if _PYTHON_RE.fullmatch(program):
        code = _python_inline_code(tokens[1:])
        return code is not None and _is_trivial_python(code)
    script = _shell_script(program, tokens[1:])
    return script is not None and _is_trivial_script(script)


def _program_name(token: str) -> str:
    name = re.split(r"[\\/]", token)[-1].lower()
    return name.removesuffix(".exe")


def command_without_env(argv: Sequence[str]) -> list[str]:
    """`argv` without a leading `env [options] [NAME=VALUE ...]` wrapper."""
    tokens = list(argv)
    if not tokens or _program_name(tokens[0]) != "env":
        return tokens
    rest = tokens[1:]
    while rest:
        token = rest.pop(0)
        if token == "--":
            break
        if token.startswith("--"):
            _consume_env_long_option(token, rest)
        elif token.startswith("-") and token != "-":
            _consume_env_short_options(token[1:], rest)
        elif "=" not in token and token != "-":
            rest.insert(0, token)
            break
    return rest


def _consume_env_long_option(token: str, rest: list[str]) -> None:
    name, has_value, value = token.partition("=")
    option = _ENV_LONG_VALUE_OPTIONS.get(name)
    if option is None:
        return
    _apply_env_value(option, value if has_value else _pop_value(rest), rest)


def _consume_env_short_options(flags: str, rest: list[str]) -> None:
    for index, flag in enumerate(flags):
        if flag in _ENV_SHORT_VALUE_OPTIONS:
            inline = flags[index + 1 :]
            _apply_env_value(flag, inline or _pop_value(rest), rest)
            return


def _apply_env_value(option: str, value: str, rest: list[str]) -> None:
    if option != "S":
        return
    try:  # the value is itself the start of the command line
        rest[:0] = shlex.split(value)
    except ValueError:  # unbalanced quotes: env rejects it, so keep it as an opaque program
        rest[:0] = [value]


def _pop_value(rest: list[str]) -> str:
    return rest.pop(0) if rest else ""


def _python_inline_code(args: Sequence[str]) -> str | None:
    """The program of `python [options] -c <code>`, or None when Python runs a file or module."""
    remaining = list(args)
    while remaining and remaining[0].startswith("-") and not remaining[0].startswith("--"):
        flags = remaining.pop(0)[1:]
        for index, flag in enumerate(flags):
            rest = flags[index + 1 :]
            if flag == "c":
                return rest or (remaining[0] if remaining else "")
            if flag == "m":
                return None
            if flag in "WX":  # these take a value: -Wignore or -W ignore
                if not rest and remaining:
                    remaining.pop(0)
                break
    return None


def _is_trivial_python(code: str) -> bool:
    """Code made only of `pass`, constants, `import sys` and exit calls with constant arguments."""
    try:
        statements = ast.parse(code).body
    except (SyntaxError, ValueError, RecursionError):
        return False  # Python would reject it, so it is not a trivial pass
    return all(_is_trivial_statement(statement) for statement in statements)


def _is_trivial_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Import):
        return all(alias.name == "sys" and alias.asname is None for alias in statement.names)
    if not isinstance(statement, ast.Expr):
        return False
    value = statement.value
    if isinstance(value, ast.Constant):
        return True
    if not isinstance(value, ast.Call) or value.keywords:
        return False
    func = value.func
    exits = (isinstance(func, ast.Name) and func.id in _PYTHON_EXITS) or (
        isinstance(func, ast.Attribute)
        and func.attr == "exit"
        and isinstance(func.value, ast.Name)
        and func.value.id == "sys"
    )
    return exits and all(isinstance(arg, ast.Constant) for arg in value.args)


def _shell_script(program: str, args: Sequence[str]) -> str | None:
    """The inline script of a shell invocation such as `bash -lc '...'` or `cmd /d /c ...`."""
    if program in _SHELLS:
        return _posix_shell_script(args)
    if program == "cmd":
        for index, arg in enumerate(args):
            if arg.lower() in ("/c", "/k"):
                return " ".join(args[index + 1 :])
            if not arg.startswith("/"):
                return None
        return None
    if program in _POWERSHELLS:
        for index, arg in enumerate(args):
            name = arg[1:].lower() if arg[:1] in ("-", "/") else ""
            if name and "command".startswith(name):  # -Command, -c
                return " ".join(args[index + 1 :])
            if name and ("file".startswith(name) or "encodedcommand".startswith(name) or name == "ec"):
                return None
        return None
    return None


def _posix_shell_script(args: Sequence[str]) -> str | None:
    remaining = list(args)
    inline = False
    while remaining and remaining[0][:1] in ("-", "+") and remaining[0] not in ("-", "--"):
        flags = remaining.pop(0)
        if flags.startswith("--"):
            continue  # long options such as --norc or --login take no value we need to skip
        inline = inline or "c" in flags
        if ("o" in flags or "O" in flags) and remaining:
            remaining.pop(0)  # -o pipefail, -O extglob
    if remaining and remaining[0] == "--":
        remaining.pop(0)
    return remaining[0] if inline and remaining else None


def _is_trivial_script(script: str) -> bool:
    lexer = shlex.shlex(script, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False  # unbalanced quotes: the shell would reject it, so it is not a trivial pass
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return all(is_trivial_run(segment) for segment in segments if segment)
