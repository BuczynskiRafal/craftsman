"""Scopes, symbols and references of a work tree: what `WorktreeIndex` knows about the code around a change.

Everything here is read-only. A file is read and parsed at most once per index; Python files are parsed with
the standard `ast` module and other languages with the definition heuristics below, so a scope is a best guess
for them. The diff itself (hunks, digests, signals, git plumbing) is `craftsman.diffscan`.
"""

from __future__ import annotations

import ast
import functools
import itertools
import re
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from craftsman.diffscan import (
    BINARY_SNIFF_BYTES,
    DiffScanError,
    Hunk,
    decode_text,
    git_bytes,
    git_text,
    is_docs_hunk_paths,
    list_untracked_paths,
    repo_file,
    walk_hunk,
    worktree_root,
)
from craftsman.diffscan_paths import is_docs_path, is_test_path

ScopeKind = Literal["function", "class", "block"]

MAX_SOURCE_BYTES = 1_048_576  # files read for scopes, symbols and references
SCOPE_SCAN_MAX_LINES = 200
BLOCK_SCOPE_RADIUS = 30
REFERENCE_TEXT_MAX_CHARS = 200
REFERENCES_PER_FILE = 5

_PYTHON_SUFFIXES = (".py", ".pyi")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_SYMBOL_STOPLIST = frozenset(
    {
        "main", "run", "get", "set", "init", "__init__", "setup", "teardown", "test", "call",
        "update", "handle", "process", "apply", "wrapper", "inner", "helper", "render", "build",
    }
)  # fmt: skip

_MODIFIERS = (
    r"(?:(?:export|default|public|private|protected|internal|static|abstract|final|sealed|async|"
    r"pub(?:\([^)]*\))?|open|data|partial|unsafe|extern|override|virtual|local)\s+)*"
)
_DEF_KEYWORDS = (
    r"(?P<kw>class|struct|impl|interface|trait|type|enum|module|namespace|object|record|fn|fun|func|"
    r"function|def)"
)
_KEYWORD_DEF_RE = re.compile(
    rf"""
    ^\s*
    {_MODIFIERS}                                # optional visibility/storage modifiers
    {_DEF_KEYWORDS}\b\s*                        # declaration keyword (class, def, fn, ...)
    """
    r"""
    (?:<[^>]*>\s*)?                             # optional generic type parameters
    (?:\([^)]*\)\s*)?                           # optional receiver, e.g. Go `func (r *Repo)`
    (?P<name>[A-Za-z_$][\w$]*)?                 # optional symbol identifier
    """,
    re.VERBOSE,
)
# Stricter than _KEYWORD_DEF_RE: the name must be followed by definition syntax, so prose such as
# "type annotations for this module" or "record the result" never yields a symbol.
_DEFINITION_RE = re.compile(
    rf"""
    ^\s*
    {_MODIFIERS}                                # optional visibility/storage modifiers
    {_DEF_KEYWORDS}                             # declaration keyword
    """
    r"""
    (?:<[^>]*>)?\s+                             # optional generics on keyword + required whitespace
    (?:\([^)]*\)\s*)?                           # optional receiver, e.g. Go `func (r *Repo)`
    (?:<[^>]*>\s*)?                             # optional generic parameters before symbol name
    (?P<name>[A-Za-z_$][\w$]*)\s*               # defined symbol identifier
    (?:[(<{=:;] | $ | (?:extends|implements)\b) # definition punctuation, EOL, or inheritance clause
    """,
    re.VERBOSE,
)
_PYTHON_DEF_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:async\s+)? def\s+ (?P<function>[A-Za-z_]\w*)\s*\(   # Python sync/async function header
        | class\s+ (?P<class>[A-Za-z_]\w*)\s*[(:]              # Python class header
    )
    """,
    re.VERBOSE,
)
_CLASS_KEYWORDS = frozenset(
    {"class", "struct", "impl", "interface", "trait", "type", "enum", "module", "namespace",
     "object", "record"}
)  # fmt: skip
_ASSIGNED_FUNCTION_RE = re.compile(
    r"""
    ^\s*
    (?:export\s+)?                              # optional JS/TS export prefix
    (?:const|let|var)\s+                        # variable binding keyword
    (?P<name>[A-Za-z_$][\w$]*)\s*=\s*           # bound function identifier
    (?:async\s+)?                               # optional async modifier
    (?:
        function\b                              # `function (...)` expression
        | \([^)]*\)\s*(?::[^=]+)?=>             # parenthesized arrow function (with optional TS return type)
        | [A-Za-z_$][\w$]*\s*=>                 # single-parameter arrow function
    )
    """,
    re.VERBOSE,
)
_MODIFIED_METHOD_RE = re.compile(
    r"""
    ^\s*
    (?:
        (?:
            public | private | protected | internal | static | final
            | abstract | override | virtual | async | synchronized | suspend
        )\s+                                    # one or more Java/C#/Kotlin method modifiers
    )+
    (?:[\w<>\[\],.?]+\s+)*?                     # optional return-type tokens (generics, arrays, nullable)
    (?P<name>[A-Za-z_]\w*)\s*\(                 # method identifier and opening parameter list
    """,
    re.VERBOSE,
)
_PROPERTY_FUNCTION_RE = re.compile(
    r"""
    (?P<name>[A-Za-z_$][\w$]*)\s*[:=]\s*        # object property or field name followed by `:` or `=`
    (?:async\s+)?                               # optional async modifier
    (?:
        function\b                              # `key: function(...)`
        | \(                                    # `key: (...) =>`
        | [A-Za-z_$][\w$]*\s*=>                 # `key: arg =>`
    )
    """,
    re.VERBOSE,
)
_CALLEE_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*$")
_CONTROL_WORDS = frozenset(
    {
        "if", "for", "foreach", "while", "switch", "catch", "else", "elif", "elsif", "return",
        "case", "unless", "until", "with", "using", "lock", "synchronized", "do", "try", "throw",
        "await", "yield", "assert", "match", "when", "select", "defer", "go", "new", "delete",
    }
)  # fmt: skip
_COMMENT_PREFIXES = ("//", "#", "/*", "*", "--", ";")
_CLOSER_RE = re.compile(r"^(?:end\b|[}\])])")
_RUST_LIFETIME_RE = re.compile(r"'[A-Za-z_]\w*\b(?!')")


@dataclass(frozen=True)
class Scope:
    path: str
    name: str
    kind: ScopeKind
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    name: str
    kind: ScopeKind
    header_line: int  # the `def`/`class` line itself, after any decorators


class _Source:
    """A work tree text file split into lines; Python definitions are parsed on first use."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self._text = text
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()  # a trailing newline ends the last line; it does not start a new one
        self.lines = tuple(lines)

    @functools.cached_property
    def python_spans(self) -> tuple[_Span, ...] | None:
        return _python_spans(self._text) if _is_python(self.path) else None


# --- scopes, symbols and references ---------------------------------------------------------


class WorktreeIndex:
    """Scopes, symbols and references in one work tree; each file is read and parsed at most once.

    Build one per snapshot and share it across hunks. It does not notice files that change
    after they were first read.
    """

    def __init__(self, cwd: str | Path) -> None:
        self.root = worktree_root(cwd)
        self._real_root = self.root.resolve()
        self._sources: dict[str, _Source | None] = {}
        self._untracked: tuple[str, ...] | None = None
        self._mentions: dict[str, tuple[Reference, ...]] = {}

    def scope(self, path: str, line: int) -> Scope | None:
        """Return the innermost function/class around `line` of the work tree file, or a block window.

        Returns None when the file is missing, binary, too large or a link out of the work tree,
        or `line` is outside it.
        """
        source = self._source(path)
        return None if source is None else _scope_at(source, line)

    def hunk_scope(self, hunk: Hunk) -> Scope | None:
        """Scope of the hunk's first added line; for a pure deletion see `_anchor_line`.

        Hunks without content lines (renames, mode changes, binary files) have no scope.
        """
        added = hunk.added_lines
        if added:
            return self.scope(hunk.path, added[0][0])
        lines = _touched_new_lines(hunk)
        return self.scope(hunk.path, lines[0]) if lines else None

    def symbols(self, hunk: Hunk) -> tuple[str, ...]:
        """Names defined on the hunk's changed lines plus the names of the scopes it touches.

        Comments, docstrings and prose never yield names: Python definitions come from the
        syntax tree when the file parses, other lines must use definition syntax.
        """
        if is_docs_hunk_paths(hunk):
            return ()
        python = _is_python(hunk.path)
        source = self._source(hunk.path)
        spans = None if source is None else source.python_spans
        if spans is not None:
            added = {number for number, _ in hunk.added_lines}
            names = [
                span.name.rsplit(".", 1)[-1]
                for span in sorted(spans, key=lambda span: span.header_line)
                if span.header_line in added
            ]
        else:
            names = [
                name for _, text in hunk.added_lines if (name := _defined_name(text, python=python))
            ]
        names += [name for _, text in hunk.removed_lines if (name := _defined_name(text, python=python))]
        if source is not None:
            for line in _touched_new_lines(hunk):
                scope = _scope_at(source, line)
                if scope is not None and scope.kind != "block":
                    names.append(scope.name.rsplit(".", 1)[-1])
        return tuple(dict.fromkeys(name for name in names if _is_meaningful_symbol(name)))

    def references(
        self, symbol: str, *, limit: int = 20, defined_in: Collection[str] = ()
    ) -> tuple[Reference, ...]:
        """Whole-word occurrences of `symbol` in tracked and untracked (not ignored) text files.

        Files outside `defined_in` come first; each file contributes at most
        `REFERENCES_PER_FILE` lines. Untracked files are the snapshot's own, so generated tool
        caches and `.craftsmanignore` entries are never searched.
        """
        if limit < 1:
            raise DiffScanError(f"limit must be at least 1, got {limit}")
        # `mentions` lists each file's references together, so a stable sort keeps them grouped.
        found = sorted(self.mentions(symbol), key=lambda ref: ref.path in defined_in)
        return tuple(found[:limit])

    def mentions(self, symbol: str) -> tuple[Reference, ...]:
        """The first `REFERENCES_PER_FILE` whole-word occurrences of `symbol` in every file that
        has one, by path; searched once per index."""
        if not symbol.strip() or any(char in symbol for char in "\n\r\0"):
            raise DiffScanError(f"invalid symbol to search for: {symbol!r}")
        if symbol not in self._mentions:
            self._mentions[symbol] = self._search(symbol)
        return self._mentions[symbol]

    def _search(self, symbol: str) -> tuple[Reference, ...]:
        word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")
        candidates = {*self._tracked_files_mentioning(symbol), *self._untracked_paths()}
        found: list[Reference] = []
        for path in sorted(candidates):
            source = self._source(path)
            if source is None:
                continue
            matches = (
                Reference(path, number, text.strip()[:REFERENCE_TEXT_MAX_CHARS])
                for number, text in enumerate(source.lines, start=1)
                if word.search(text)
            )
            found.extend(itertools.islice(matches, REFERENCES_PER_FILE))
        return tuple(found)

    def lines(self, path: str) -> tuple[str, ...] | None:
        """The lines of a work tree text file; None when it is missing, binary or too large."""
        source = self._source(path)
        return None if source is None else source.lines

    def _source(self, path: str) -> _Source | None:
        if path not in self._sources:
            self._sources[path] = _load_source(self.root, self._real_root, path)
        return self._sources[path]

    def _untracked_paths(self) -> tuple[str, ...]:
        if self._untracked is None:
            self._untracked = tuple(list_untracked_paths(self.root))
        return self._untracked

    def _tracked_files_mentioning(self, symbol: str) -> list[str]:
        """Tracked work tree files with a whole-word match; `-l` stops reading each at the first."""
        code, out, err = git_text(self.root, "grep", "-l", "-I", "-w", "-F", "-z", "-e", symbol, "--")
        if code == 1:
            return []
        if code != 0:
            raise DiffScanError(f"`git grep` for {symbol!r} failed (exit {code}): {err.strip()}")
        return [path for path in out.split("\0") if path]


def file_at_revision(cwd: str | Path, revision: str, path: str) -> str | None:
    """The text of `path` as committed at `revision`; None when it did not exist there or is binary."""
    root = worktree_root(cwd)
    repo_file(root, path)  # rejects absolute and `..` paths
    code, data, _ = git_bytes(root, "show", f"{revision}:{path}")
    if code != 0 or b"\0" in data[:BINARY_SNIFF_BYTES] or len(data) > MAX_SOURCE_BYTES:
        return None
    return decode_text(data)


def text_scope_at(path: str, text: str, line: int) -> Scope | None:
    """The innermost function/class (or block window) around `line` of `text`, as `scope` finds it."""
    return _scope_at(_Source(path, text), line)


def text_scope_named(path: str, text: str, name: str, near: int = 1) -> Scope | None:
    """The definition called `name` in `text`; of several, the one starting closest to `near`.

    `name` is a scope name as `Scope.name` spells it (`Class.method` for Python), or its last part.
    """
    source = _Source(path, text)
    short = name.rsplit(".", 1)[-1]
    found: list[Scope] = []
    if source.python_spans is not None:
        spans = [span for span in source.python_spans if span.name == name] or [
            span for span in source.python_spans if span.name.rsplit(".", 1)[-1] == short
        ]
        found = [scope for span in spans if (scope := _scope_at(source, span.header_line)) is not None]
    else:
        for index in range(len(source.lines)):
            if _looks_like_definition(source.lines, index) and _definition_name(source.lines[index].strip())[0] == short:
                scope = _scope_at(source, index + 1)
                if scope is not None and scope.name == short:
                    found.append(scope)
    return min(found, key=lambda scope: abs(scope.start - near), default=None)


def defines_symbol(path: str, line_text: str, symbol: str) -> bool:
    """Whether the single line `line_text` defines `symbol` (a `def`, `class`, `function`, ...)."""
    return _defined_name(line_text, python=_is_python(path)) == symbol


def _load_source(root: Path, real_root: Path, path: str) -> _Source | None:
    file = repo_file(root, path)
    try:
        if not file.is_file() or not file.resolve().is_relative_to(real_root):
            return None
        if file.stat().st_size > MAX_SOURCE_BYTES:
            return None
        data = file.read_bytes()
    except OSError as exc:
        raise DiffScanError(f"cannot read {path}: {exc}") from exc
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return None
    return _Source(path, decode_text(data))


def _is_python(path: str) -> bool:
    return PurePosixPath(path).suffix in _PYTHON_SUFFIXES


def _python_spans(text: str) -> tuple[_Span, ...] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # Deeply nested generated code exhausts the parser's stack (RecursionError/MemoryError).
        return None
    spans: list[_Span] = []
    stack: list[tuple[ast.AST, str]] = [(tree, "")]
    while stack:
        node, prefix = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = min([child.lineno, *(dec.lineno for dec in child.decorator_list)])
                kind: ScopeKind = "class" if isinstance(child, ast.ClassDef) else "function"
                end = child.end_lineno or child.lineno
                spans.append(_Span(start, end, name, kind, child.lineno))
                stack.append((child, f"{name}."))
            else:
                stack.append((child, prefix))
    return tuple(spans)


def _scope_at(source: _Source, line: int) -> Scope | None:
    if not 1 <= line <= len(source.lines):
        return None
    if source.python_spans is not None:
        span = _innermost_span(source.python_spans, line)
    else:
        span = _heuristic_span(source.lines, line, rust=source.path.endswith(".rs"))
    if span is None:
        start = max(1, line - BLOCK_SCOPE_RADIUS)
        end = min(len(source.lines), line + BLOCK_SCOPE_RADIUS)
        span = _Span(start, end, f"lines {start}-{end}", "block", start)
    text = "\n".join(source.lines[span.start - 1 : span.end])
    return Scope(source.path, span.name, span.kind, span.start, span.end, text)


def _innermost_span(spans: Sequence[_Span], line: int) -> _Span | None:
    containing = [span for span in spans if span.start <= line <= span.end]
    return max(containing, key=lambda span: (span.start, -span.end), default=None)


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _effective_indent(lines: Sequence[str], index: int) -> int:
    """Indentation of the line, or of the nearest non-blank line below/above when it is blank."""
    for candidate in (*range(index, len(lines)), *range(index - 1, -1, -1)):
        if lines[candidate].strip():
            return _indent(lines[candidate])
    return 0


def _heuristic_span(lines: Sequence[str], line: int, *, rust: bool) -> _Span | None:
    target = line - 1
    target_indent = _effective_indent(lines, target)
    for index in range(target, max(-1, target - SCOPE_SCAN_MAX_LINES), -1):
        text = lines[index]
        indent = _indent(text)
        if index != target and indent >= target_indent and indent != 0:
            continue
        if not _looks_like_definition(lines, index):
            continue
        end = _definition_end(lines, index, indent, rust=rust)
        if end >= target:
            name, kind = _definition_name(text.strip())
            return _Span(index + 1, end + 1, name, kind, index + 1)
    return None


def _looks_like_definition(lines: Sequence[str], index: int) -> bool:
    stripped = lines[index].strip()
    if not stripped or stripped.startswith(_COMMENT_PREFIXES):
        return False
    code = stripped.lstrip("}").lstrip()
    first_word = _IDENTIFIER_RE.match(code)
    if first_word is not None and first_word.group(0) in _CONTROL_WORDS:
        return False
    if code.endswith(";"):
        return False
    if any(regex.match(code) for regex in (_DEFINITION_RE, _MODIFIED_METHOD_RE, _ASSIGNED_FUNCTION_RE)):
        return True
    # Otherwise a body must open: `impl X for Y {` or `name(...) {`, but not a bare `log.info(`.
    following = lines[index + 1].lstrip() if index + 1 < len(lines) else ""
    keyword = _KEYWORD_DEF_RE.match(code)
    if keyword is not None and keyword.group("name"):
        return "{" in code or following.startswith("{")
    if "(" not in code:
        return False
    return "{" in code[code.index("(") :] or following.startswith("{")


def _definition_name(stripped: str) -> tuple[str, ScopeKind]:
    keyword = _KEYWORD_DEF_RE.match(stripped)
    if keyword is not None and keyword.group("name"):
        kind: ScopeKind = "class" if keyword.group("kw") in _CLASS_KEYWORDS else "function"
        return keyword.group("name"), kind
    assigned = _PROPERTY_FUNCTION_RE.search(stripped)
    if assigned is not None:
        return assigned.group("name"), "function"
    callee = _CALLEE_RE.search(stripped.split("(", 1)[0])
    if callee is not None and callee.group(1) != "function":
        return callee.group(1), "function"
    return "<anonymous>", "function"


def _definition_end(lines: Sequence[str], start: int, indent: int, *, rust: bool) -> int:
    following = lines[start + 1] if start + 1 < len(lines) else ""
    if "{" in lines[start] or following.lstrip().startswith("{"):
        return _brace_end(lines, start, rust=rust)
    end = start
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            continue
        if _indent(lines[index]) > indent:
            end = index
            continue
        if _CLOSER_RE.match(stripped):
            end = index
        break
    return end


def _brace_end(lines: Sequence[str], start: int, *, rust: bool) -> int:
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        for char in _code_chars(lines[index], rust=rust):
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
            if opened and depth <= 0:
                return index
    return len(lines) - 1


def _code_chars(line: str, *, rust: bool) -> Iterator[str]:
    """Yield the characters of `line` that are outside simple string literals and `//` comments."""
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif rust and char == "'" and _RUST_LIFETIME_RE.match(line, index):
            pass  # a lifetime such as 'a or 'static, not a character literal
        elif char in "\"'`":
            quote = char
        elif line.startswith("//", index):
            return
        else:
            yield char
        index += 1


def _defined_name(text: str, *, python: bool) -> str | None:
    """The name a single changed line defines; None for comments, prose and plain statements."""
    stripped = text.strip()
    if not stripped or stripped.startswith(_COMMENT_PREFIXES):
        return None
    if python:
        match = _PYTHON_DEF_RE.match(text)
        return None if match is None else match.group("function") or match.group("class")
    for pattern in (_DEFINITION_RE, _ASSIGNED_FUNCTION_RE, _MODIFIED_METHOD_RE):
        definition = pattern.match(text)
        if definition is not None:
            return definition.group("name")
    return None


def _touched_new_lines(hunk: Hunk) -> tuple[int, ...]:
    """The new-file line that stands for each changed line, in order, see `_anchor_line`."""
    lines = (_anchor_line(tag, new_no) for tag, _, new_no, _ in walk_hunk(hunk) if tag in ("+", "-"))
    return tuple(dict.fromkeys(lines))


def _anchor_line(tag: str, new_no: int) -> int:
    """An added line stands for itself; a removed line for the surviving line just above it.

    The line now at a deletion's place may already belong to the next definition, the line above
    still belongs to the one the deletion was made in.
    """
    return new_no if tag == "+" else max(1, new_no - 1)


def _is_meaningful_symbol(name: str) -> bool:
    return len(name) >= 3 and name not in _SYMBOL_STOPLIST and _IDENTIFIER_RE.fullmatch(name) is not None



_IMPORT_LINE_RE = re.compile(
    r"^(?:from\s+\S+\s+import\b|import\b|use\s|using\s|#\s*include\b|require\b|extern\s+crate\b"
    r"|(?:const|let|var)\b.*\brequire\()"
)


def reference_kind(ref: Reference) -> Literal["call", "test", "import", "docs"]:
    """What a reference most likely is, so real uses can rank before imports and prose."""
    if is_docs_path(ref.path):
        return "docs"
    if _IMPORT_LINE_RE.match(ref.text):
        return "import"
    return "test" if is_test_path(ref.path) else "call"
