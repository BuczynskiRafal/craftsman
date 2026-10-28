"""Static reading of shell command lines for the git guard and the search scope.

`parse_command_line` lists every simple command a command line would run, nested ones included:
command substitutions (`$(...)`, backticks, `<(...)`), scripts given to a shell (`bash -c`,
`pwsh -Command`, here-documents, here-strings, `echo ... | sh`), `eval`, and the commands of
`find -exec` and `fd -x`. Each command carries the directory it runs in, following `cd`, `pushd`,
`popd`, subshells, pipelines, background jobs and conditional code.

The reader understands bash and zsh syntax, plus the Windows paths and home variables of
PowerShell. It runs nothing, so it marks what only the running shell would know: a word holding a
variable other than the home directory, a command substitution or arithmetic is `dynamic`, a word
holding an unquoted glob or brace pattern is a `glob`, and a directory that depends on either is
unknown (None). A construct it cannot follow raises `ParseError`; callers treat that as a failed
check, never as a harmless command.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePath

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
POWERSHELLS = frozenset({"pwsh", "powershell"})
CD_COMMANDS = frozenset({"cd", "chdir", "set-location", "sl"})
PUSHD_COMMANDS = frozenset({"pushd", "push-location"})
POPD_COMMANDS = frozenset({"popd", "pop-location"})
HOME_VARIABLES = frozenset({"HOME", "USERPROFILE"})
MAX_NESTING = 8

# Options of command wrappers that take a value; every wrapper is listed, so this is also the
# list of commands that run the command after their options.
WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
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
            "--close-from",
            "--host",
            "--role",
            "--type",
            "--command-timeout",
        }
    ),  # fmt: skip
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "exec": frozenset({"-a"}),
    "timeout": frozenset({"-k", "-s", "--kill-after", "--signal"}),
    "xargs": frozenset(
        {
            "-n",
            "-L",
            "-I",
            "-P",
            "-d",
            "-E",
            "-a",
            "-s",
            "--max-args",
            "--max-procs",
            "--max-lines",
            "--delimiter",
            "--arg-file",
            "--eof",
            "--max-chars",
            "--replace",
        }
    ),  # fmt: skip
    "command": frozenset(),
    "builtin": frozenset(),
    "nohup": frozenset(),
}
WRAPPER_CHDIR_OPTIONS: dict[str, frozenset[str]] = {
    "env": frozenset({"-C", "--chdir"}),
    "sudo": frozenset({"-D", "--chdir"}),
}
SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})
FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
FIND_EXEC_IN_FILE_DIR = frozenset({"-execdir", "-okdir"})
FD_EXEC_FLAGS = frozenset({"-x", "--exec", "-X", "--exec-batch"})

_RESERVED = frozenset(
    {"if", "then", "elif", "else", "fi", "do", "done", "while", "until", "for", "select", "in",
     "case", "esac", "function", "{", "}", "!", "[["}
)  # fmt: skip
_COMPOUND_ENDS = frozenset({"then", "elif", "else", "fi", "do", "done", "esac", "}", "in"})
_OPERATORS = (";;&", ";;", ";&", "&&", "||", "|&", ";", "&", "|", "(", ")")
_CASE_ENDS = frozenset({";;", ";&", ";;&"})
_BLANKS = " \t"
_METACHARS = frozenset(" \t\n;&|()<>")
_REDIRECT_RE = re.compile(r"\d*(<<<|<<-|<<|<>|<&|>&|>>|>\||&>>|&>|<|>)")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_SPECIAL_PARAMETERS = frozenset("@*#?$!-0123456789")
_WINDOWS_WORD_RE = re.compile(
    r"(?:[A-Za-z]:|~|\.\.?|%[A-Za-z_]\w*%|\$(?:HOME|\{HOME\}|env:[A-Za-z_]\w*))\\", re.IGNORECASE
)
_DRIVE_RE = re.compile(r"^([A-Za-z]):(?:[/\\](.*))?$", re.DOTALL)
_USERPROFILE_TOKEN = "%USERPROFILE%"
_ANSI_C_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
                   "e": "\x1b", "E": "\x1b", "\\": "\\", "'": "'", '"': '"', "?": "?"}  # fmt: skip
_ECHO_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\"}


class ParseError(ValueError):
    """The command line uses a construct the reader cannot follow statically."""


def home_dir() -> str:
    """The home directory the shell expands `~` and `$HOME` to."""
    return os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())


def expand_home(path: str) -> str:
    """`path` (a plain path, not shell syntax) with a leading `~` or home variable expanded."""
    home = home_dir()
    expanded = re.sub(
        r"^(?:\$HOME|\$\{HOME\}|\$env:HOME|\$env:USERPROFILE|%USERPROFILE%)(?=[/\\]|$)",
        lambda _: home.replace("\\", "\\\\"),
        path,
        flags=re.IGNORECASE,
    )
    if expanded == "~" or expanded.startswith(("~/", "~\\")):
        return home + expanded[1:]
    return expanded


def resolve_path(text: str, base: Path | None) -> Path | None:
    """The normalized absolute path `text` names when run in `base`; None when `base` is unknown.

    A Windows drive path on a POSIX host is kept absolute under `/<drive>/`, as MSYS spells it.
    """
    drive = _DRIVE_RE.match(text)
    if drive and os.name != "nt":
        rest = (drive.group(2) or "").replace("\\", "/")
        return Path(os.path.normpath(f"/{drive.group(1).lower()}/{rest}"))
    path = Path(text)
    if path.is_absolute():
        return Path(os.path.normpath(path))
    if base is None:
        return None
    return Path(os.path.normpath(base / path))


@dataclass(frozen=True)
class Word:
    """One shell word after quote removal and expansion of `~` and the home variables.

    `dynamic`: the word holds a value only the running shell knows (a variable, `$(...)`,
    backticks, arithmetic); its text keeps that part as written. `glob`: the word holds an
    unquoted glob or brace pattern, which the shell expands to file names.
    """

    text: str
    dynamic: bool = False
    glob: bool = False

    @property
    def static(self) -> bool:
        return not self.dynamic and not self.glob


@dataclass(frozen=True)
class Assignment:
    name: str
    value: Word


@dataclass(frozen=True)
class SimpleCommand:
    """One command a command line runs.

    `words` is the command and its arguments, after leading assignments and wrappers such as
    `env`, `sudo`, `timeout` and `xargs`. `cwd` is the directory it runs in, None when only the
    running shell knows it. `runtime_args`: more arguments arrive at run time (`xargs`).
    `redirect_targets` are the files its redirections read or write.
    """

    words: tuple[Word, ...]
    cwd: Path | None
    source: str
    assignments: tuple[Assignment, ...] = ()
    redirect_targets: tuple[Word, ...] = ()
    runtime_args: bool = False

    @property
    def name(self) -> str:
        """The command's lowercased file name without directory or `.exe`; "" without words."""
        return command_name(self.words[0].text) if self.words else ""

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(word.text for word in self.words)

    @property
    def args(self) -> tuple[Word, ...]:
        return self.words[1:]

    def assigned(self, name: str) -> Word | None:
        """The value the command's environment gets for `name` from its own assignments."""
        values = [item.value for item in self.assignments if item.name == name]
        return values[-1] if values else None


def command_name(text: str) -> str:
    return PurePath(text.replace("\\", "/")).name.lower().removesuffix(".exe")


def parse_command_line(command_line: str, cwd: Path) -> tuple[SimpleCommand, ...]:
    """Every simple command `command_line` runs when started in `cwd`; see the module docstring."""
    out: list[SimpleCommand] = []
    parser = _Parser(command_line, _Dirs(cwd=cwd), depth=0, out=out)
    parser.parse_program()
    return tuple(out)


def split_words(text: str) -> tuple[Word, ...]:
    """The words of `text`, a single simple command without operators or redirections."""
    lexer = _Lexer(text)
    words: list[Word] = []
    while True:
        token = lexer.next_token()
        if isinstance(token, _WordToken):
            if token.nested:
                raise ParseError(f"`{text}` runs a command substitution")
            words.append(token.word)
        elif isinstance(token, _OpToken) and token.op == "eof":
            return tuple(words)
        else:
            raise ParseError(f"`{text}` is not a single simple command")


# --- lexer ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _WordToken:
    word: Word
    start: int
    end: int
    nested: tuple[str, ...]
    plain: bool  # no quoting or expansion: may be a reserved word or an assignment name


@dataclass(frozen=True)
class _OpToken:
    op: str  # one of _OPERATORS, "\n", or "eof"
    start: int


class _RedirectKind(Enum):
    FILE = "file"
    DUPLICATE = "duplicate"
    HEREDOC = "heredoc"
    HERESTRING = "herestring"


@dataclass
class _Heredoc:
    """A here-document whose body the lexer reads at the end of the line."""

    delimiter: str
    strip_tabs: bool
    expands: bool
    body: str | None = None
    nested: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RedirectToken:
    kind: _RedirectKind
    target: Word | None
    nested: tuple[str, ...]
    heredoc: _Heredoc | None
    start: int
    end: int


_Token = _WordToken | _OpToken | _RedirectToken


class _WordBuilder:
    """Characters of a word being read, each marked quoted or not, plus what it expands."""

    def __init__(self) -> None:
        self.chars: list[str] = []
        self.quoted: list[bool] = []
        self.dynamic = False
        self.nested: list[str] = []
        self.plain = True

    def add(self, text: str, *, quoted: bool) -> None:
        self.chars.extend(text)
        self.quoted.extend([quoted] * len(text))

    def word(self) -> Word:
        text = "".join(self.chars)
        return Word(text, dynamic=self.dynamic, glob=_has_glob(self.chars, self.quoted))


def _has_glob(chars: Sequence[str], quoted: Sequence[bool]) -> bool:
    unquoted = [(index, char) for index, char in enumerate(chars) if not quoted[index]]
    if any(char in "*?[" for _, char in unquoted):
        return True
    opens = [index for index, char in unquoted if char == "{"]
    for start in opens:
        closes = [index for index, char in unquoted if char == "}" and index > start]
        if closes:
            inner = "".join(chars[start + 1 : closes[0]])
            if "," in inner or ".." in inner:
                return True
    return False


class _Lexer:
    def __init__(self, src: str) -> None:
        self.src = src
        self.pos = 0
        self.pending: list[_Heredoc] = []

    def next_token(self) -> _Token:
        self._skip_blanks()
        src, pos = self.src, self.pos
        if pos >= len(src):
            self._end_heredocs()
            return _OpToken("eof", pos)
        char = src[pos]
        if char == "#":
            end = src.find("\n", pos)
            self.pos = len(src) if end == -1 else end
            return self.next_token()
        if char == "\n":
            self.pos += 1
            self._read_heredoc_bodies()
            return _OpToken("\n", pos)
        if src.startswith(("<(", ">("), pos):
            return self._read_word()
        redirect = _REDIRECT_RE.match(src, pos)
        if redirect and (char in "<>&" or redirect.group(0)[: -len(redirect.group(1))].isdigit()):
            return self._read_redirect(redirect)
        for op in _OPERATORS:
            if src.startswith(op, pos):
                self.pos += len(op)
                return _OpToken(op, pos)
        return self._read_word()

    def _skip_blanks(self) -> None:
        src = self.src
        while self.pos < len(src):
            if src[self.pos] in _BLANKS:
                self.pos += 1
            elif src.startswith("\\\n", self.pos):
                self.pos += 2
            else:
                return

    # -- redirections

    def _read_redirect(self, match: re.Match[str]) -> _RedirectToken:
        start, op = self.pos, match.group(1)
        self.pos = match.end()
        self._skip_blanks()
        if op in ("<<", "<<-"):
            return self._read_heredoc_start(op, start)
        target = self._read_word()
        if not isinstance(target, _WordToken):
            raise ParseError(f"`{op}` has no target")
        kind = _RedirectKind.FILE
        if op == "<<<":
            kind = _RedirectKind.HERESTRING
        elif op in ("<&", ">&") and (target.word.text == "-" or target.word.text.isdigit()):
            kind = _RedirectKind.DUPLICATE
        return _RedirectToken(kind, target.word, target.nested, None, start, target.end)

    def _read_heredoc_start(self, op: str, start: int) -> _RedirectToken:
        delimiter = self._read_word()
        if not isinstance(delimiter, _WordToken) or not delimiter.word.text:
            raise ParseError(f"`{op}` has no delimiter")
        heredoc = _Heredoc(
            delimiter=delimiter.word.text, strip_tabs=op == "<<-", expands=delimiter.plain
        )
        self.pending.append(heredoc)
        return _RedirectToken(_RedirectKind.HEREDOC, None, (), heredoc, start, delimiter.end)

    def _read_heredoc_bodies(self) -> None:
        """Read the bodies of the line's here-documents; a missing terminator ends at the end."""
        for heredoc in self.pending:
            lines: list[str] = []
            while self.pos < len(self.src):
                end = self.src.find("\n", self.pos)
                line = self.src[self.pos :] if end == -1 else self.src[self.pos : end]
                self.pos = len(self.src) if end == -1 else end + 1
                if (line.lstrip("\t") if heredoc.strip_tabs else line) == heredoc.delimiter:
                    break
                lines.append(line)
            heredoc.body = "".join(line + "\n" for line in lines)
            if heredoc.expands:
                heredoc.nested = _substitutions_in_text(heredoc.body)
        self.pending = []

    def _end_heredocs(self) -> None:
        for heredoc in self.pending:
            heredoc.body = ""
        self.pending = []

    # -- words

    def _read_word(self) -> _WordToken:
        src, start = self.src, self.pos
        builder = _WordBuilder()
        if src.startswith(("<(", ">("), start):
            inner, self.pos = _balanced(src, start + 1, "(", ")")
            builder.add(src[start : self.pos], quoted=True)
            builder.dynamic, builder.plain = True, False
            builder.nested.append(inner)
            return self._token(builder, start)
        windows = _WINDOWS_WORD_RE.match(src, start) is not None
        self._read_tilde(builder, windows)
        while self.pos < len(src) and src[self.pos] not in _METACHARS:
            self._read_word_part(builder, windows)
        if self.pos == start:
            raise ParseError(f"unexpected `{src[start]}`")
        return self._token(builder, start)

    def _token(self, builder: _WordBuilder, start: int) -> _WordToken:
        return _WordToken(builder.word(), start, self.pos, tuple(builder.nested), builder.plain)

    def _read_tilde(self, builder: _WordBuilder, windows: bool) -> None:
        src, pos = self.src, self.pos
        if not src.startswith("~", pos):
            return
        after = src[pos + 1] if pos + 1 < len(src) else ""
        if after == "" or after in _METACHARS or after == "/" or (windows and after == "\\"):
            builder.add(home_dir(), quoted=True)
        else:  # `~user`, `~+`, `~-`: another user's home or the shell's directory stack
            builder.add("~", quoted=True)
            builder.dynamic = True
        builder.plain = False
        self.pos += 1

    def _read_word_part(self, builder: _WordBuilder, windows: bool) -> None:
        src, pos = self.src, self.pos
        char = src[pos]
        if char == "\\":
            self._read_backslash(builder, windows)
        elif char == "'":
            end = src.find("'", pos + 1)
            if end == -1:
                raise ParseError("unterminated single quote")
            builder.add(src[pos + 1 : end], quoted=True)
            builder.plain = False
            self.pos = end + 1
        elif char == '"':
            self.pos += 1
            self._read_double_quoted(builder)
        elif char == "$":
            self._read_dollar(builder, in_double_quotes=False)
        elif char == "`":
            self._read_backticks(builder)
        elif (
            src.startswith(_USERPROFILE_TOKEN, pos) or src[pos : pos + 13].upper() == _USERPROFILE_TOKEN
        ):
            builder.add(home_dir(), quoted=True)
            builder.plain = False
            self.pos += len(_USERPROFILE_TOKEN)
        else:
            builder.add(char, quoted=False)
            self.pos += 1

    def _read_backslash(self, builder: _WordBuilder, windows: bool) -> None:
        src, pos = self.src, self.pos
        after = src[pos + 1] if pos + 1 < len(src) else ""
        builder.plain = False
        if after == "\n":
            self.pos += 2
        elif windows or after == "":
            # A Windows path separator: the platform's own separator, so the path resolves.
            builder.add(os.sep, quoted=True)
            self.pos += 1
        else:
            builder.add(after, quoted=True)
            self.pos += 2

    def _read_double_quoted(self, builder: _WordBuilder) -> None:
        src = self.src
        builder.plain = False
        while self.pos < len(src):
            char = src[self.pos]
            if char == '"':
                self.pos += 1
                return
            if char == "\\" and self.pos + 1 < len(src) and src[self.pos + 1] in '$`"\\\n':
                if src[self.pos + 1] != "\n":
                    builder.add(src[self.pos + 1], quoted=True)
                self.pos += 2
            elif char == "$":
                self._read_dollar(builder, in_double_quotes=True)
            elif char == "`":
                self._read_backticks(builder)
            else:
                builder.add(char, quoted=True)
                self.pos += 1
        raise ParseError("unterminated double quote")

    def _read_dollar(self, builder: _WordBuilder, *, in_double_quotes: bool) -> None:
        src, pos = self.src, self.pos
        builder.plain = False
        if src.startswith("$((", pos):
            inner, self.pos = _balanced(src, pos + 2, "(", ")")
            if "$(" in inner or "`" in inner:
                raise ParseError("command substitution inside arithmetic")
            self._dynamic(builder, src[pos : self.pos])
        elif src.startswith("$(", pos):
            inner, self.pos = _balanced(src, pos + 1, "(", ")")
            builder.nested.append(inner)
            self._dynamic(builder, src[pos : self.pos])
        elif src.startswith("${", pos):
            inner, self.pos = _balanced(src, pos + 1, "{", "}")
            if "$(" in inner or "`" in inner:
                raise ParseError("command substitution inside `${...}`")
            self._variable(builder, inner, src[pos : self.pos])
        elif src.startswith("$'", pos) and not in_double_quotes:
            self._read_ansi_c(builder)
        elif src.startswith('$"', pos) and not in_double_quotes:
            self.pos += 2
            self._read_double_quoted(builder)
        elif env := re.match(r"\$env:([A-Za-z_]\w*)", src[pos:], re.IGNORECASE):
            self.pos += env.end()
            self._variable(builder, env.group(1), env.group(0))
        elif name := _NAME_RE.match(src, pos + 1):
            self.pos = name.end()
            self._variable(builder, name.group(0), src[pos : self.pos])
        elif pos + 1 < len(src) and src[pos + 1] in _SPECIAL_PARAMETERS:
            self.pos += 2
            self._dynamic(builder, src[pos : self.pos])
        else:
            builder.add("$", quoted=True)
            self.pos += 1

    def _variable(self, builder: _WordBuilder, name: str, written: str) -> None:
        if name.upper() in HOME_VARIABLES:
            builder.add(home_dir(), quoted=True)
        else:
            self._dynamic(builder, written)

    @staticmethod
    def _dynamic(builder: _WordBuilder, written: str) -> None:
        builder.add(written, quoted=True)
        builder.dynamic = True

    def _read_backticks(self, builder: _WordBuilder) -> None:
        src, start = self.src, self.pos
        inner: list[str] = []
        pos = start + 1
        while pos < len(src) and src[pos] != "`":
            if src[pos] == "\\" and pos + 1 < len(src) and src[pos + 1] in "`$\\":
                pos += 1
            inner.append(src[pos])
            pos += 1
        if pos >= len(src):
            raise ParseError("unterminated backtick")
        self.pos = pos + 1
        builder.nested.append("".join(inner))
        builder.plain = False
        self._dynamic(builder, src[start : self.pos])

    def _read_ansi_c(self, builder: _WordBuilder) -> None:
        src = self.src
        pos = self.pos + 2
        while pos < len(src) and src[pos] != "'":
            if src[pos] == "\\" and pos + 1 < len(src):
                builder.add(_ANSI_C_ESCAPES.get(src[pos + 1], src[pos + 1]), quoted=True)
                pos += 2
            else:
                builder.add(src[pos], quoted=True)
                pos += 1
        if pos >= len(src):
            raise ParseError("unterminated `$'...'` string")
        self.pos = pos + 1


def _balanced(src: str, start: int, open_char: str, close_char: str) -> tuple[str, int]:
    """The text inside the bracket at `src[start]` and the index after its match."""
    depth, pos = 0, start
    quote: str | None = None
    while pos < len(src):
        char = src[pos]
        if char == "\\" and quote != "'":
            pos += 2
            continue
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "'\"`":
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return src[start + 1 : pos], pos + 1
        pos += 1
    raise ParseError(f"unterminated `{open_char}`")


def _substitutions_in_text(text: str) -> tuple[str, ...]:
    """The command substitutions an expanding here-document body runs."""
    lexer = _Lexer(text)
    builder = _WordBuilder()
    while lexer.pos < len(text):
        char = text[lexer.pos]
        if char == "\\":
            lexer.pos += 2
        elif char == "$":
            lexer._read_dollar(builder, in_double_quotes=True)
        elif char == "`":
            lexer._read_backticks(builder)
        else:
            lexer.pos += 1
    return tuple(builder.nested)


# --- parser ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Dirs:
    """The shell's directories: None marks one only the running shell knows."""

    cwd: Path | None
    oldpwd: Path | None = None
    stack: tuple[Path | None, ...] | None = ()

    def unknown(self) -> _Dirs:
        return _Dirs(cwd=None, oldpwd=None, stack=None)

    def moved_to(self, target: Path | None) -> _Dirs:
        return replace(self, cwd=target, oldpwd=self.cwd)


@dataclass(frozen=True)
class _PipeText:
    """What a pipeline element writes to the next: known text, or None when not known statically.

    `external`: the output comes from outside the command line (a file, a program), which the
    reader does not analyze, just as it does not read a script file given to a shell.
    """

    text: str | None
    external: bool = False


_NO_PIPE = _PipeText(None, external=True)


@dataclass
class _Parser:
    src: str
    dirs: _Dirs
    depth: int
    out: list[SimpleCommand]
    lexer: _Lexer = field(init=False)
    lookahead: _Token | None = field(default=None, init=False)
    deferred: list[tuple[_Heredoc, Callable[[str], None]]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.depth > MAX_NESTING:
            raise ParseError("commands are nested too deeply")
        self.lexer = _Lexer(self.src)

    # -- tokens

    def peek(self) -> _Token:
        if self.lookahead is None:
            self.lookahead = self.lexer.next_token()
            if isinstance(self.lookahead, _OpToken) and self.lookahead.op in ("\n", "eof"):
                self._run_deferred()
        return self.lookahead

    def take(self) -> _Token:
        token = self.peek()
        self.lookahead = None
        return token

    def _run_deferred(self) -> None:
        ready = [(heredoc, action) for heredoc, action in self.deferred if heredoc.body is not None]
        self.deferred = [item for item in self.deferred if item[0].body is None]
        for heredoc, action in ready:
            assert heredoc.body is not None
            action(heredoc.body)

    def at_op(self, *ops: str) -> bool:
        token = self.peek()
        return isinstance(token, _OpToken) and token.op in ops

    def at_word(self, *words: str) -> bool:
        token = self.peek()
        return isinstance(token, _WordToken) and token.plain and token.word.text in words

    def expect_word(self, word: str) -> None:
        if not self.at_word(word):
            raise ParseError(f"expected `{word}` before {self._describe(self.peek())}")
        self.take()

    def expect_op(self, op: str) -> None:
        if not self.at_op(op):
            raise ParseError(f"expected `{op}` before {self._describe(self.peek())}")
        self.take()

    def skip_newlines(self) -> None:
        while self.at_op("\n"):
            self.take()

    @staticmethod
    def _describe(token: _Token) -> str:
        if isinstance(token, _OpToken):
            return "the end" if token.op == "eof" else f"`{token.op}`"
        if isinstance(token, _WordToken):
            return f"`{token.word.text}`"
        return "a redirection"

    # -- grammar

    def parse_program(self) -> None:
        self.parse_list(stop_words=frozenset(), stop_ops=frozenset())
        if not self.at_op("eof"):
            raise ParseError(f"unexpected {self._describe(self.peek())}")
        self.take()

    def parse_list(self, *, stop_words: frozenset[str], stop_ops: frozenset[str]) -> None:
        while True:
            while self.at_op("\n", ";"):
                self.take()
            if self.at_op("eof", *stop_ops) or self.at_word(*stop_words):
                return
            before = self.dirs
            self._and_or()
            if self.at_op("&"):
                self.take()
                self.dirs = before  # a background job runs in a subshell
            elif self.at_op(";", "\n"):
                self.take()
            elif not (self.at_op("eof", *stop_ops) or self.at_word(*stop_words)):
                raise ParseError(f"unexpected {self._describe(self.peek())}")

    def _and_or(self) -> None:
        self._pipeline()
        while self.at_op("&&", "||"):
            otherwise = isinstance(op := self.take(), _OpToken) and op.op == "||"
            self.skip_newlines()
            if otherwise:
                self._conditionally(self._pipeline)
            else:
                self._pipeline()

    def _pipeline(self) -> None:
        if self.at_word("!"):
            self.take()
        start = self.dirs
        piped = _NO_PIPE
        elements = 0
        while True:
            if elements:
                self.dirs = start
            piped = self._command(stdin=piped if elements else _NO_PIPE)
            elements += 1
            if not self.at_op("|", "|&"):
                break
            self.take()
            self.skip_newlines()
        if elements > 1:
            # Each element runs in a subshell, except that zsh runs the last in the shell itself.
            self.dirs = start if self.dirs == start else start.unknown()

    def _conditionally(self, run: Callable[[], None]) -> None:
        """Run code the shell may or may not execute: if it moves, the directory is unknown."""
        before = self.dirs
        run()
        if self.dirs != before:
            self.dirs = before.unknown()

    def _command(self, *, stdin: _PipeText) -> _PipeText:
        token = self.peek()
        if isinstance(token, _OpToken) and token.op == "(":
            self.take()
            self._in_subshell(lambda: self._group(")"))
            self._compound_redirects()
            return _NO_PIPE
        if isinstance(token, _WordToken) and token.plain and token.word.text in _RESERVED:
            return self._reserved(token.word.text)
        return self._simple(stdin)

    def _group(self, closer: str) -> None:
        if closer == ")":
            self.parse_list(stop_words=frozenset(), stop_ops=frozenset({")"}))
            self.expect_op(")")
        else:
            self.parse_list(stop_words=frozenset({"}"}), stop_ops=frozenset())
            self.expect_word("}")

    def _in_subshell(self, run: Callable[[], None]) -> None:
        before = self.dirs
        run()
        self.dirs = before

    def _reserved(self, word: str) -> _PipeText:
        handlers: dict[str, Callable[[], None]] = {
            "{": lambda: (self.take(), self._group("}")) and None,
            "if": self._if,
            "while": self._while,
            "until": self._while,
            "for": self._for,
            "select": self._for,
            "case": self._case,
            "function": self._function_keyword,
            "[[": self._test_expression,
        }
        handler = handlers.get(word)
        if handler is None:
            raise ParseError(f"unexpected `{word}`")
        handler()
        if word != "function":
            self._compound_redirects()
        return _NO_PIPE

    def _if(self) -> None:
        self.take()

        def body() -> None:
            self.parse_list(stop_words=frozenset({"then"}), stop_ops=frozenset())
            self.expect_word("then")
            self.parse_list(stop_words=frozenset({"elif", "else", "fi"}), stop_ops=frozenset())
            while self.at_word("elif"):
                self.take()
                self.parse_list(stop_words=frozenset({"then"}), stop_ops=frozenset())
                self.expect_word("then")
                self.parse_list(stop_words=frozenset({"elif", "else", "fi"}), stop_ops=frozenset())
            if self.at_word("else"):
                self.take()
                self.parse_list(stop_words=frozenset({"fi"}), stop_ops=frozenset())
            self.expect_word("fi")

        self._conditionally(body)

    def _while(self) -> None:
        self.take()

        def body() -> None:
            self.parse_list(stop_words=frozenset({"do"}), stop_ops=frozenset())
            self._do_done()

        self._conditionally(body)

    def _do_done(self) -> None:
        self.expect_word("do")
        self.parse_list(stop_words=frozenset({"done"}), stop_ops=frozenset())
        self.expect_word("done")

    def _for(self) -> None:
        self.take()
        name = self.take()
        if not isinstance(name, _WordToken):
            raise ParseError("`for` needs a variable name; arithmetic `for ((...))` is not supported")
        self.skip_newlines()
        if self.at_word("in"):
            self.take()
            while isinstance(item := self.peek(), _WordToken):
                self.take()
                self._substitutions(item.nested)
        if self.at_op(";"):
            self.take()
        self.skip_newlines()
        self._conditionally(self._do_done)

    def _case(self) -> None:
        self.take()
        subject = self.take()
        if not isinstance(subject, _WordToken):
            raise ParseError("`case` needs a word")
        self._substitutions(subject.nested)
        self.skip_newlines()
        self.expect_word("in")
        self.skip_newlines()
        while not self.at_word("esac"):
            if self.at_op("("):
                self.take()
            self._case_patterns()
            self._conditionally(
                lambda: self.parse_list(stop_words=frozenset({"esac"}), stop_ops=_CASE_ENDS)
            )
            if self.at_op(*_CASE_ENDS):
                self.take()
            self.skip_newlines()
        self.take()

    def _case_patterns(self) -> None:
        while True:
            pattern = self.take()
            if not isinstance(pattern, _WordToken):
                raise ParseError("`case` pattern expected")
            self._substitutions(pattern.nested)
            if self.at_op(")"):
                self.take()
                return
            self.expect_op("|")

    def _function_keyword(self) -> None:
        self.take()
        if not isinstance(self.take(), _WordToken):
            raise ParseError("`function` needs a name")
        if self.at_op("("):
            self.take()
            self.expect_op(")")
        self._function_body()

    def _function_body(self) -> None:
        """Read a function's body: it runs only when called, so a body that moves makes later
        directories unknown."""
        self.skip_newlines()
        before = self.dirs
        self._command(stdin=_NO_PIPE)
        moved = self.dirs != before
        self.dirs = before.unknown() if moved else before

    def _test_expression(self) -> None:
        self.take()
        while not self.at_word("]]"):
            token = self.take()
            if isinstance(token, _OpToken) and token.op == "eof":
                raise ParseError("unterminated `[[`")
            if isinstance(token, _WordToken | _RedirectToken):
                self._substitutions(token.nested)
        self.take()

    def _compound_redirects(self) -> None:
        targets: list[Word] = []
        while isinstance(token := self.peek(), _RedirectToken):
            self.take()
            self._redirect_effects(token, targets, on_stdin=None)
        if targets:
            self.out.append(SimpleCommand((), self.dirs.cwd, "", redirect_targets=tuple(targets)))

    # -- simple commands

    def _simple(self, stdin: _PipeText) -> _PipeText:
        words: list[_WordToken] = []
        redirects: list[_RedirectToken] = []
        while True:
            token = self.peek()
            if isinstance(token, _WordToken):
                words.append(token)
                self.take()
            elif isinstance(token, _RedirectToken):
                redirects.append(token)
                self.take()
            elif isinstance(token, _OpToken) and token.op == "(" and len(words) == 1 and not redirects:
                self.take()
                self.expect_op(")")
                self._function_body()
                return _NO_PIPE
            else:
                break
        if not words and not redirects:
            raise ParseError(f"unexpected {self._describe(self.peek())}")
        cwd = self.dirs.cwd
        for token in words:
            self._substitutions(token.nested)
        stdin_script = self._stdin_script(stdin)
        targets: list[Word] = []
        for redirect in redirects:
            stdin_script = self._redirect_effects(redirect, targets, on_stdin=stdin_script)
        assignments, command = _split_assignments(words)
        source = self.src[words[0].start if words else redirects[0].start : self._end(words, redirects)]
        return self._run(
            [token.word for token in command],
            cwd=cwd,
            source=source,
            assignments=assignments,
            targets=targets,
            stdin=stdin_script,
        )

    @staticmethod
    def _end(words: Sequence[_WordToken], redirects: Sequence[_RedirectToken]) -> int:
        return max([token.end for token in words] + [token.end for token in redirects])

    def _stdin_script(self, piped: _PipeText) -> _StdinScript:
        if piped.external:
            return _StdinScript(None, deferred=None)
        if piped.text is None:
            return _StdinScript(None, deferred=None, unknown=True)
        return _StdinScript(piped.text, deferred=None)

    def _redirect_effects(
        self, redirect: _RedirectToken, targets: list[Word], *, on_stdin: _StdinScript | None
    ) -> _StdinScript:
        """Record a redirection's file target and parse what it runs; return the command's stdin."""
        self._substitutions(redirect.nested)
        stdin = on_stdin if on_stdin is not None else _StdinScript(None, deferred=None)
        if redirect.kind is _RedirectKind.FILE and redirect.target is not None:
            targets.append(redirect.target)
            return _StdinScript(None, deferred=None) if redirect.target.text else stdin
        if redirect.kind is _RedirectKind.HERESTRING and redirect.target is not None:
            if redirect.target.dynamic:
                return _StdinScript(None, deferred=None, unknown=True)
            return _StdinScript(redirect.target.text + "\n", deferred=None)
        if redirect.kind is _RedirectKind.HEREDOC and redirect.heredoc is not None:
            heredoc = redirect.heredoc
            cwd = self.dirs.cwd
            self.deferred.append((heredoc, lambda _body: self._substitutions(heredoc.nested, cwd=cwd)))
            return _StdinScript(None, deferred=heredoc)
        return stdin

    def _substitutions(self, scripts: Sequence[str], cwd: Path | None = None) -> None:
        """Parse command substitutions: each runs in a subshell of the current directory."""
        for script in scripts:
            self._nested(script, self.dirs.cwd if cwd is None else cwd)

    def _nested(self, script: str, cwd: Path | None) -> None:
        sub = _Parser(script, replace(self.dirs, cwd=cwd), self.depth + 1, self.out)
        sub.parse_program()

    def _run(
        self,
        words: list[Word],
        *,
        cwd: Path | None,
        source: str,
        assignments: Sequence[Assignment],
        targets: Sequence[Word],
        stdin: _StdinScript,
    ) -> _PipeText:
        """Record a command that runs and follow what it runs or changes; return its output."""
        unwrapped = _unwrap(words, cwd)
        command = SimpleCommand(
            words=tuple(unwrapped.words),
            cwd=unwrapped.cwd,
            source=source,
            assignments=(*assignments, *unwrapped.assignments),
            redirect_targets=tuple(targets),
            runtime_args=unwrapped.runtime_args,
        )
        if command.words and command.words[0].dynamic:
            raise ParseError(f"the command `{command.words[0].text}` is known only at run time")
        if not command.words and not targets:
            return _NO_PIPE
        self.out.append(command)
        name = command.name
        if name in CD_COMMANDS | PUSHD_COMMANDS | POPD_COMMANDS:
            self.dirs = _change_directory(self.dirs, name, command.args, command.cwd)
        elif name == "eval":
            self._eval(command)
        elif name in SHELLS | POWERSHELLS:
            self._shell(command, stdin)
        else:
            for sub in _exec_commands(command):
                self._run(
                    list(sub.words), cwd=sub.cwd, source=sub.source, assignments=(),
                    targets=(), stdin=_StdinScript(None, deferred=None),
                )  # fmt: skip
        return _output_of(command)

    def _eval(self, command: SimpleCommand) -> None:
        """`eval` runs its arguments in the shell itself, so their `cd` moves the shell."""
        if any(word.dynamic for word in command.args):
            raise ParseError("`eval` runs text known only at run time")
        sub = _Parser(" ".join(word.text for word in command.args), self.dirs, self.depth + 1, self.out)
        sub.parse_program()
        self.dirs = sub.dirs

    def _shell(self, command: SimpleCommand, stdin: _StdinScript) -> None:
        script = _shell_script(command)
        if script is _READS_STDIN:
            if stdin.unknown:
                raise ParseError(f"`{command.name}` runs a script known only at run time")
            if stdin.deferred is not None:
                cwd = command.cwd
                self.deferred.append((stdin.deferred, lambda body: self._nested(body, cwd)))
            elif stdin.text is not None:
                self._nested(stdin.text, command.cwd)
        elif isinstance(script, str):
            self._nested(script, command.cwd)


@dataclass(frozen=True)
class _StdinScript:
    """What a command reads on stdin: known text, a here-document read later, or unknown."""

    text: str | None
    deferred: _Heredoc | None
    unknown: bool = False


class _Sentinel(Enum):
    READS_STDIN = "stdin"


_READS_STDIN = _Sentinel.READS_STDIN


def _split_assignments(words: Sequence[_WordToken]) -> tuple[list[Assignment], list[_WordToken]]:
    assignments: list[Assignment] = []
    index = 0
    while index < len(words) and (match := _ASSIGNMENT_RE.match(words[index].word.text)):
        if not _is_assignment(words[index]):
            break
        name = match.group(0).rstrip("+=")
        assignments.append(Assignment(name, _assignment_value(words[index].word, match.end())))
        index += 1
    return assignments, list(words[index:])


def _is_assignment(token: _WordToken) -> bool:
    """`NAME=value` with the name and `=` written unquoted, as the shell requires."""
    raw_name = token.word.text.split("=", 1)[0].rstrip("+")
    return _NAME_RE.fullmatch(raw_name) is not None and token.word.text.startswith(raw_name)


def _assignment_value(word: Word, offset: int) -> Word:
    return Word(word.text[offset:], dynamic=word.dynamic, glob=False)


@dataclass(frozen=True)
class _Unwrapped:
    words: list[Word]
    cwd: Path | None
    assignments: list[Assignment]
    runtime_args: bool


def is_env_assignment(text: str) -> bool:
    """Whether a wrapper argument such as `env NAME=value` sets a variable."""
    return _ASSIGNMENT_RE.match(text) is not None and "+=" not in text.split("=", 1)[0] + "="


def _unwrap(words: list[Word], cwd: Path | None) -> _Unwrapped:
    """Skip wrappers (`env`, `sudo`, `timeout`, `xargs`, ...) and follow their `--chdir`."""
    index, assignments, runtime_args = 0, [], False
    while index < len(words) and (wrapper := command_name(words[index].text)) in WRAPPER_VALUE_OPTIONS:
        if wrapper == "command" and any(word.text in ("-v", "-V") for word in words[index + 1 :]):
            return _Unwrapped([], cwd, assignments, runtime_args)  # only prints where it is
        index, cwd = _wrapper_options(wrapper, words, index + 1, cwd, assignments)
        if wrapper == "timeout":
            index += 1  # the duration
        runtime_args = runtime_args or wrapper == "xargs"
    return _Unwrapped(words[index:], cwd, assignments, runtime_args)


def _wrapper_options(
    wrapper: str, words: Sequence[Word], index: int, cwd: Path | None, assignments: list[Assignment]
) -> tuple[int, Path | None]:
    """Consume a wrapper's options; return the index of the wrapped command and its directory."""
    value_options = WRAPPER_VALUE_OPTIONS[wrapper]
    chdir_options = WRAPPER_CHDIR_OPTIONS.get(wrapper, frozenset())
    while index < len(words):
        text = words[index].text
        option, _, attached = text.partition("=")
        if text == "--":
            return index + 1, cwd
        if wrapper in ("env", "sudo") and is_env_assignment(text):
            name, _, value = text.partition("=")
            assignments.append(Assignment(name, Word(value, dynamic=words[index].dynamic)))
            index += 1
        elif wrapper == "env" and option in ("-S", "--split-string"):
            raise ParseError("`env -S` splits its command at run time")
        elif option in chdir_options:
            value = _option_value(words, index, attached if "=" in text else None)
            cwd = _resolve_word(value, cwd)
            index += 1 if "=" in text else 2
        elif text in value_options:
            index += 2
        elif text.startswith("-") and len(text) > 1:
            index += 1
        else:
            break
    return index, cwd


def _option_value(words: Sequence[Word], index: int, attached: str | None) -> Word:
    if attached is not None:
        return Word(attached, dynamic=words[index].dynamic, glob=words[index].glob)
    if index + 1 >= len(words):
        raise ParseError(f"`{words[index].text}` needs a value")
    return words[index + 1]


def _resolve_word(word: Word, base: Path | None) -> Path | None:
    """The directory a word names, or None when only the running shell knows it."""
    return None if not word.static else resolve_path(word.text, base)


def _change_directory(dirs: _Dirs, name: str, args: Sequence[Word], cwd: Path | None) -> _Dirs:
    operands = _cd_operands(args)
    if name in POPD_COMMANDS:
        if operands or dirs.stack is None:
            return replace(dirs, cwd=None, stack=None) if not operands else replace(dirs, stack=None)
        if not dirs.stack:
            return dirs  # bash reports an empty stack and stays
        return replace(dirs.moved_to(dirs.stack[-1]), stack=dirs.stack[:-1])
    if name in PUSHD_COMMANDS:
        if not operands or operands[0].text.startswith(("+", "-")):
            return dirs.unknown()  # rotation of the directory stack
        target = _resolve_word(operands[0], cwd)
        stack = None if dirs.stack is None else (*dirs.stack, dirs.cwd)
        return replace(dirs.moved_to(target), stack=stack)
    if not operands:
        return dirs.moved_to(Path(home_dir()))
    if operands[0].text == "-":
        return dirs.moved_to(dirs.oldpwd)
    return dirs.moved_to(_resolve_word(operands[0], cwd))


def _cd_operands(args: Sequence[Word]) -> list[Word]:
    operands: list[Word] = []
    index = 0
    while index < len(args):
        text = args[index].text
        if text == "--":
            return operands + list(args[index + 1 :])
        if text.lower() in ("-path", "-literalpath") and index + 1 < len(args):
            operands.append(args[index + 1])
            index += 2
            continue
        if not (text.startswith("-") and text != "-") or args[index].dynamic:
            operands.append(args[index])
        index += 1
    return operands


def _shell_script(command: SimpleCommand) -> str | _Sentinel | None:
    """The script a shell command runs: its text, `_READS_STDIN`, or None for a script file."""
    if command.name in POWERSHELLS:
        return _powershell_script(command.args)
    args = command.args
    index, inline, from_stdin = 0, False, False
    while index < len(args):
        text = args[index].text
        if text == "--":
            index += 1
            break
        if text in SHELL_VALUE_OPTIONS:
            index += 2
            continue
        if text.startswith("--"):
            index += 1
            continue
        if text[:1] in ("-", "+") and len(text) > 1:
            inline = inline or "c" in text[1:]
            from_stdin = from_stdin or "s" in text[1:]
            index += 1
            continue
        break
    operands = args[index:]
    if inline:
        return _known_script(command, operands[0]) if operands else None
    if from_stdin or not operands:
        return _READS_STDIN
    return None


def _powershell_script(args: Sequence[Word]) -> str | _Sentinel | None:
    for index, word in enumerate(args):
        option = word.text.lower()
        if option in ("-encodedcommand", "-ec", "-e", "-enc"):
            raise ParseError("PowerShell runs an encoded command")
        if option in ("-command", "-c"):
            rest = args[index + 1 :]
            if any(item.dynamic for item in rest):
                raise ParseError("PowerShell runs a command known only at run time")
            return " ".join(item.text for item in rest) if rest else _READS_STDIN
        if option in ("-file", "-f"):
            return None
    return None


def _known_script(command: SimpleCommand, word: Word) -> str:
    if word.dynamic:
        raise ParseError(f"`{command.name} -c` runs a script known only at run time")
    return word.text


def _exec_commands(command: SimpleCommand) -> list[SimpleCommand]:
    """The commands `find -exec`/`-execdir`/`-ok` and `fd -x`/`-X` run for each match."""
    name = command.name
    if name == "find":
        return _exec_args(command, FIND_EXEC_FLAGS, terminators=(";", "+"))
    if name in ("fd", "fdfind"):
        return _exec_args(command, FD_EXEC_FLAGS, terminators=(";",))
    return []


def _exec_args(
    command: SimpleCommand, flags: frozenset[str], terminators: tuple[str, ...]
) -> list[SimpleCommand]:
    found: list[SimpleCommand] = []
    args = command.args
    index = 0
    while index < len(args):
        flag = args[index].text
        index += 1
        if flag not in flags:
            continue
        start = index
        while index < len(args) and args[index].text not in terminators:
            index += 1
        words = args[start:index]
        if words:
            cwd = None if flag in FIND_EXEC_IN_FILE_DIR else command.cwd
            found.append(SimpleCommand(words, cwd, " ".join(word.text for word in words)))
    return found


def _output_of(command: SimpleCommand) -> _PipeText:
    """What `echo` and `printf` write, when known; any other command's output is external."""
    if command.name not in ("echo", "printf") or command.runtime_args:
        return _NO_PIPE
    args = list(command.args)
    if any(word.dynamic for word in args):
        return _PipeText(None)
    if command.name == "printf":
        if not args or "%" in args[0].text:
            return _PipeText(None)
        return _PipeText(_unescape(args[0].text))
    while args and args[0].text in ("-n", "-e", "-E", "-ne", "-en"):
        args.pop(0)
    return _PipeText(_unescape(" ".join(word.text for word in args)) + "\n")


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ECHO_ESCAPES.get(m.group(1), m.group(0)), text)
