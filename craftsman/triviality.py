"""Recognize commands that prove nothing, such as `true`, `echo ok` or `sh -c 'exit 0'`.

A recorded run only counts as verification when its command exercises code; these checks see
through `env` prefixes, interpreter flags and inline shell or Python scripts. For a run of kind
`test` an inline Python program (`python -c`, or a heredoc fed to `python -`) is also trivial when it
checks nothing: no `assert`, no `raise`, no test framework and no exit status computed from the code.

`counts_as_verification` is the one rule for "this run is passing evidence" that the gate, the
attribution counters and the ledger share.
"""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Sequence

from craftsman.records import Run, RunKind

# Test and end-to-end runs are what counts as verification.
PASSING_KINDS: tuple[RunKind, ...] = ("test", "e2e")
_TEST_FRAMEWORKS = frozenset({"unittest", "pytest", "doctest"})
_EXIT_CALLS = frozenset({"exit", "quit", "_exit"})
# Calls that raise when what they run fails: `subprocess.check_call([...])`, `subprocess.run([...], check=True)`.
_RAISING_SUBPROCESS_CALLS = frozenset({"check_call", "check_output"})
_RUNPY_CALLS = frozenset({"run_path", "run_module"})
_HEREDOC_RE = re.compile(r"(?<!<)<<(-?)\s*([\"']?)([A-Za-z_]\w*)\2")
_HEREDOC_WORD = "@@heredoc{}@@"
_HEREDOC_WORD_RE = re.compile(r"@@heredoc(\d+)@@")
_TRIVIAL_PROGRAMS = frozenset({"true", ":", "exit", "echo", "printf"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_POWERSHELLS = frozenset({"powershell", "pwsh"})
_SHELL_PUNCTUATION = "();<>|&\n"
_SEPARATOR_CHARS = frozenset("();|&\n")
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?|py")
_PYTHON_EXITS = frozenset({"exit", "quit"})
# `env` options that consume a value: -u NAME, -C DIR, -S STRING and their long forms.
_ENV_SHORT_VALUE_OPTIONS = frozenset("uCS")
_ENV_LONG_VALUE_OPTIONS = {"--unset": "u", "--chdir": "C", "--split-string": "S"}


def is_trivial_run(argv: Sequence[str], kind: str | None = None) -> bool:
    """True for commands that prove nothing, such as `true`, `echo ok` or `sh -c 'exit 0'`.

    `kind` is the run kind; `test` also rejects an inline Python program that checks nothing.
    """
    return _is_trivial(argv, kind)


def counts_as_verification(run: Run) -> bool:
    """A test or e2e run that exited 0, left the work tree alone, ran tests (if a test run) and proves something."""
    return (
        run.kind in PASSING_KINDS and run.counts_as_passing and not is_trivial_run(run.argv, run.kind)
    )


def _is_trivial(argv: Sequence[str], kind: str | None) -> bool:
    tokens = command_without_env(argv)
    if not tokens:
        return True
    program = _program_name(tokens[0])
    if program in _TRIVIAL_PROGRAMS:
        return True
    if _PYTHON_RE.fullmatch(program):
        code = _python_inline_code(tokens[1:])
        if code is None:
            return False
        return _is_trivial_python(code) or (kind == "test" and _checks_nothing(code))
    script = _shell_script(program, tokens[1:])
    return script is not None and _is_trivial_script(script, kind)


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


def _checks_nothing(code: str) -> bool:
    """Python that can neither fail a check nor fail on a computed exit status."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return False  # Python would reject it, so it is not a trivial pass
    return not any(_checks(node) for node in ast.walk(tree))


def _checks(node: ast.AST) -> bool:
    if isinstance(node, (ast.Assert, ast.Raise)):
        return True
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] in _TEST_FRAMEWORKS for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] in _TEST_FRAMEWORKS
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in ("__import__", "import_module"):  # __import__("pytest").main([...])
            first = node.args[0] if node.args else None
            return (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.split(".")[0] in _TEST_FRAMEWORKS
            )
        if name in _RAISING_SUBPROCESS_CALLS or _is_checked_run(node, name):
            return True
        # sys.exit(0 if ok() else 1): an exit status computed from the code is the check
        return name in _EXIT_CALLS and not all(isinstance(arg, ast.Constant) for arg in node.args)
    return False


def _is_checked_run(call: ast.Call, name: str) -> bool:
    """`subprocess.run(..., check=True)` and `runpy.run_path(...)`: both raise when the code they run fails."""
    if name == "run":
        return any(
            keyword.arg == "check" and not (isinstance(keyword.value, ast.Constant) and not keyword.value.value)
            for keyword in call.keywords
        )
    func = call.func
    return (
        name in _RUNPY_CALLS
        and isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "runpy"
    )


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


def _is_trivial_script(script: str, kind: str | None) -> bool:
    heredocs: list[str] = []
    if kind == "test":
        script, heredocs = _extract_heredocs(script)
    # A newline ends a command like `;` does: it is a punctuation character here, not whitespace.
    lexer = shlex.shlex(script, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return False  # unbalanced quotes: the shell would reject it, so it is not a trivial pass
    segments: list[list[str]] = [[]]
    for token in tokens:
        if _is_separator(token):
            segments.append([])
        else:
            segments[-1].append(token)
    return all(_is_trivial_segment(segment, kind, heredocs) for segment in segments if segment)


def _is_separator(token: str) -> bool:
    """`&&`, `||`, `;`, `|`, `&`, parentheses and newlines, alone or run together (`;\n`)."""
    return bool(token) and all(char in _SEPARATOR_CHARS for char in token)


def _is_trivial_segment(segment: list[str], kind: str | None, heredocs: Sequence[str]) -> bool:
    fed = [int(m[1]) for token in segment if (m := _HEREDOC_WORD_RE.fullmatch(token))]
    words = [token for token in segment if not _HEREDOC_WORD_RE.fullmatch(token)]
    if fed and words:
        tokens = command_without_env(words)
        stdin_python = (
            tokens
            and _PYTHON_RE.fullmatch(_program_name(tokens[0]))
            and (len(tokens) == 1 or tokens[1] == "-")
        )
        if stdin_python:
            return _checks_nothing(heredocs[fed[-1]])
    return _is_trivial(words, kind) if words else True


def _heredoc_in(line: str) -> re.Match[str] | None:
    """The first `<<WORD` of `line` that starts a heredoc, not a shift inside `$(( ... ))` or `(( ... ))`."""
    for match in _HEREDOC_RE.finditer(line):
        before = line[: match.start()]
        if before.count("((") <= before.count("))"):
            return match
    return None


def _extract_heredocs(script: str) -> tuple[str, list[str]]:
    """The script without its heredoc bodies, each `<<WORD` replaced by a marker, and the bodies in order."""
    kept: list[str] = []
    bodies: list[str] = []
    lines = iter(script.split("\n"))
    for line in lines:
        match = _heredoc_in(line)
        if match is None:
            kept.append(line)
            continue
        marker = _HEREDOC_WORD.format(len(bodies))
        # The body ends the command, so the next line starts a new one: end this line with a separator.
        kept.append(f"{line[: match.start()]} {marker} {line[match.end() :]} ;")
        body: list[str] = []
        for body_line in lines:
            if body_line.strip() == match[3]:
                break
            body.append(body_line.lstrip("\t") if match[1] else body_line)
        bodies.append("\n".join(body))
    return "\n".join(kept), bodies
