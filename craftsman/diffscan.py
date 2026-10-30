"""Deterministic snapshots of a work tree diff: hunks, digests, scopes, references and risk signals.

Everything here is read-only. Paths are repository-root-relative POSIX strings no matter which
subdirectory the caller starts from, and every changed file yields at least one hunk, so nothing
that changed can escape review.
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import hashlib
import itertools
import os
import re
import subprocess
import tempfile
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from craftsman.diffscan_signals import CONTENT_SIGNAL_PATTERNS

ChangeStatus = Literal["modified", "added", "deleted", "renamed"]
ScopeKind = Literal["function", "class", "block"]

GIT_TIMEOUT_S = 60.0
MAX_UNTRACKED_BYTES = 1_048_576  # 1 MiB
MAX_SOURCE_BYTES = 1_048_576  # files read for scopes, symbols and references
BINARY_SNIFF_BYTES = 8000  # the prefix git itself checks for NUL bytes
SCOPE_SCAN_MAX_LINES = 200
BLOCK_SCOPE_RADIUS = 30
REFERENCE_TEXT_MAX_CHARS = 200
REFERENCES_PER_FILE = 5
NO_NEWLINE_MARKER = "\\ No newline at end of file"

SIGNALS = (
    "process",
    "filesystem",
    "network",
    "sql",
    "deserialization",
    "concurrency",
    "time",
    "regex",
    "auth",
    "errors",
    "datamodel",
    "platform",
    "config",
    "tests",
    "deletion",
    "docs",
)

_NULL_BLOB = "0" * 40
_GIT_ENV = {"LC_ALL": "C", "LANGUAGE": "C"}  # unlocalized messages such as "Binary files ... differ"
_MODE_KEYS = ("old mode", "new mode", "new file mode", "deleted file mode")
_PYTHON_SUFFIXES = (".py", ".pyi")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
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
_CONFIG_SUFFIXES = frozenset(
    {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env", ".lock", ".gradle", ".cmake"}
)  # fmt: skip
_CONFIG_NAMES = frozenset(
    {"Dockerfile", "Makefile", "package.json", "pyproject.toml", "setup.py", "setup.cfg", ".env",
     "CMakeLists.txt", "Jenkinsfile", "build.gradle.kts", "settings.gradle.kts", "conanfile.txt",
     "conanfile.py"}
)  # fmt: skip
_CONFIG_NAME_GLOBS = (
    "Dockerfile.*", ".env.*", "requirements*.txt", "constraints*.txt", "Jenkinsfile.*", "*.Jenkinsfile",
)  # fmt: skip
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__", "testing"})
_TEST_NAME_GLOBS = (
    "test_*.py", "*_test.py", "*.test.ts", "*.test.tsx", "*.test.js", "*.test.jsx",
    "*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx", "*_test.go",
    "*_test.c", "*_test.cc", "*_test.cpp", "*_test.cxx", "*_unittest.cc", "*_unittest.cpp",
    "test_*.c", "test_*.cc", "test_*.cpp", "*Test.java", "*Tests.java", "*Test.kt", "*Test.groovy",
    "*Spec.groovy",
)  # fmt: skip
_DOCS_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})
# Languages whose risky calls the content patterns above know; changed code in any other language
# is listed by `files_without_signal_patterns` so the reviewer reads it for risks instead.
_PATTERN_LANGUAGE_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".kt", ".cs",
     ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".groovy", ".gradle"}
)  # fmt: skip
_PATTERN_LANGUAGE_NAME_GLOBS = ("Jenkinsfile", "Jenkinsfile.*", "*.Jenkinsfile")
_DATA_SUFFIXES = frozenset({".json", ".lock", ".toml", ".ini", ".cfg", ".csv", ".tsv", ".svg"})


class DiffScanError(RuntimeError):
    """Raised when the repository or a file in it cannot be inspected."""


@dataclass(frozen=True)
class Hunk:
    id: str
    path: str
    old_path: str | None
    status: ChangeStatus
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: tuple[str, ...]
    digest: str

    @property
    def added_lines(self) -> tuple[tuple[int, str], ...]:
        return tuple((new_no, text) for tag, _, new_no, text in _walk(self) if tag == "+")

    @property
    def removed_lines(self) -> tuple[tuple[int, str], ...]:
        return tuple((old_no, text) for tag, old_no, _, text in _walk(self) if tag == "-")


@dataclass(frozen=True)
class DiffSnapshot:
    base_sha: str
    fingerprint: str
    hunks: tuple[Hunk, ...]
    files: tuple[str, ...]
    skipped: tuple[str, ...]
    added: int
    deleted: int

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def docs_only(self) -> bool:
        renamed_from = (hunk.old_path for hunk in self.hunks if hunk.old_path is not None)
        return bool(self.files) and all(is_docs_path(path) for path in (*self.files, *renamed_from))


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
class _RawHunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: tuple[str, ...]
    salt: str | None  # change metadata mixed into synthetic digests; None for content hunks


@dataclass(frozen=True)
class _FileDiff:
    path: str
    old_path: str | None
    status: ChangeStatus
    meta: dict[str, str]  # parsed header lines (modes, index, binary) plus synthetic markers
    salt: str  # the change metadata that identifies the file's state independently of git's wording
    hunks: tuple[_RawHunk, ...]
    skipped: bool


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


# --- git plumbing ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _existing_dir(cwd: str | Path) -> Path:
    directory = Path(cwd)
    if not directory.is_dir():
        raise DiffScanError(f"not a directory: {directory}")
    return directory


def _git_bytes(cwd: Path, *args: str) -> tuple[int, bytes, str]:
    """Run git with stable, unlocalized output; return (returncode, raw stdout, decoded stderr)."""
    argv = ["git", "-c", "core.quotepath=off", "-c", "color.ui=never", *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, **_GIT_ENV},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise DiffScanError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiffScanError(f"`git {args[0]}` timed out after {GIT_TIMEOUT_S:.0f}s in {cwd}") from exc
    except OSError as exc:
        raise DiffScanError(f"cannot run git in {cwd}: {exc}") from exc
    return proc.returncode, proc.stdout, _decode(proc.stderr)


def _git(cwd: Path, *args: str) -> tuple[int, str, str]:
    """Like `_git_bytes`, with stdout decoded as UTF-8."""
    code, out, err = _git_bytes(cwd, *args)
    return code, _decode(out), err


def git_output(cwd: Path, *args: str) -> str:
    """Stdout of a git command that must succeed; failures raise `DiffScanError` with stderr."""
    code, out, err = _git(cwd, *args)
    if code != 0:
        raise DiffScanError(f"`git {' '.join(args)}` failed in {cwd} (exit {code}): {err.strip()}")
    return out


def worktree_root(cwd: str | Path) -> Path:
    """Return the root of the git work tree containing `cwd`."""
    directory = _existing_dir(cwd)
    code, out, err = _git(directory, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        raise DiffScanError(f"{directory} is not inside a git work tree: {err.strip()}")
    return Path(out.strip())


def git_dir(cwd: str | Path) -> Path:
    """Return the absolute, per-worktree git directory for `cwd`."""
    directory = _existing_dir(cwd)
    code, out, err = _git(directory, "rev-parse", "--absolute-git-dir")
    if code != 0 or not out.strip():
        raise DiffScanError(f"{directory} is not inside a git repository: {err.strip()}")
    return Path(out.strip()).resolve()


def commit_sha(cwd: str | Path, ref: str) -> str | None:
    """Full SHA of the commit `ref` names, or None when it names no commit (e.g. an unborn HEAD)."""
    if not ref or ref.startswith("-"):
        raise DiffScanError(f"invalid git revision {ref!r}")
    code, out, _ = _git(_existing_dir(cwd), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if code == 0 and out.strip() else None


def _base_revision(root: Path, ref: str) -> str:
    """A commit SHA, or the empty tree for HEAD before the first commit (unborn or orphan branch)."""
    sha = commit_sha(root, ref)
    if sha is not None:
        return sha
    empty_tree = git_output(root, "hash-object", "-t", "tree", "--stdin").strip()
    if ref in ("HEAD", empty_tree):
        return empty_tree
    raise DiffScanError(
        f"{ref!r} does not name a commit in {root} (does the repository have any commits?)"
    )


def resolve_base_sha(cwd: str | Path, ref: str | None) -> str:
    """Resolve `ref` (default HEAD) to a full commit SHA; before the first commit, the empty tree."""
    return _base_revision(worktree_root(cwd), ref or "HEAD")


def _repo_file(root: Path, path: str) -> Path:
    parts = PurePosixPath(path).parts
    candidate = Path(os.path.normpath(root.joinpath(*parts)))
    if not parts or ".." in parts or not candidate.is_relative_to(root):
        raise DiffScanError(f"expected a repository-relative POSIX path, got {path!r}")
    return candidate


# --- snapshot -------------------------------------------------------------------------------


def _normalize_filter_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        raise DiffScanError(f"expected a repository-relative POSIX path, got {path!r}")
    trailing = "/" if path.endswith("/") else ""
    core = path[:-1] if trailing else path
    parts = core.split("/")
    has_drive = bool(parts) and len(parts[0]) >= 2 and parts[0][1] == ":" and parts[0][0].isalpha()
    if not core or has_drive or any(part in ("", ".", "..") for part in parts):
        raise DiffScanError(f"expected a repository-relative POSIX path, got {path!r}")
    return f"{core}{trailing}"


def _normalize_filter_paths(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_normalize_filter_path(path) for path in paths))


def _filter_untracked(untracked: Sequence[str], filters: tuple[str, ...] | None) -> list[str]:
    if filters is None:
        return list(untracked)
    exact = {item for item in filters if not item.endswith("/")}
    prefixes = tuple(item for item in filters if item.endswith("/"))
    return [path for path in untracked if path in exact or path.startswith(prefixes)]


def snapshot(
    cwd: str | Path, base_sha: str, paths: Sequence[str] | None = None
) -> DiffSnapshot:
    """Capture changes between `base_sha` and the work tree (all of it, or restricted to `paths`)."""
    root = worktree_root(cwd)
    base = _base_revision(root, base_sha)
    filters = None if paths is None else _normalize_filter_paths(paths)
    if filters is not None and not filters:
        return _assemble(base, ())
    pathspecs = () if filters is None else tuple(f":(literal){path}" for path in filters)
    tracked_text = git_output(
        root,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--full-index",
        "--submodule=short",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "-U3",
        "--find-renames",
        base,
        "--",
        *pathspecs,
    )
    tracked = [_parse_file_section(section) for section in _split_file_sections(tracked_text)]
    untracked = _filter_untracked(_untracked_paths(root), filters)
    # `git rm --cached` leaves a file on disk that the diff reports as deleted and ls-files as new.
    deleted = {chunk.path: chunk for chunk in tracked if chunk.status == "deleted"}
    unindexed = {path: deleted[path] for path in untracked if path in deleted}
    chunks = [chunk for chunk in tracked if chunk.path not in unindexed]
    for path in untracked:
        if path not in unindexed:
            chunks.append(_untracked_file_diff(root, path))
        elif (restored := _unindexed_file_diff(root, base, unindexed[path])) is not None:
            chunks.append(restored)
    return _assemble(base, sorted(chunks, key=lambda chunk: chunk.path))


def _assemble(base: str, chunks: Sequence[_FileDiff]) -> DiffSnapshot:
    """Number the hunks and fingerprint the diff from normalized content, not git's raw text.

    The fingerprint therefore stays the same when an untracked file is staged or committed
    without changing its content.
    """
    fingerprint = hashlib.sha256()
    hunks: list[Hunk] = []
    occurrences: dict[str, int] = {}
    for chunk in chunks:
        fingerprint.update(f"{chunk.path}\0{chunk.salt}\0".encode())
        for raw in chunk.hunks:
            digest = _digest(chunk.path, raw)
            occurrences[digest] = occurrences.get(digest, 0) + 1
            if occurrences[digest] > 1:  # only byte-identical hunks with identical context remain
                digest = f"{digest}#{occurrences[digest]}"
            fingerprint.update(
                f"{digest} {raw.old_start},{raw.old_len} {raw.new_start},{raw.new_len}\n".encode()
            )
            hunks.append(
                Hunk(
                    id=f"H{len(hunks) + 1}",
                    path=chunk.path,
                    old_path=chunk.old_path,
                    status=chunk.status,
                    old_start=raw.old_start,
                    old_len=raw.old_len,
                    new_start=raw.new_start,
                    new_len=raw.new_len,
                    header=raw.header,
                    lines=raw.lines,
                    digest=digest,
                )
            )
    content_lines = [line for hunk in hunks for line in hunk.lines]
    return DiffSnapshot(
        base_sha=base,
        fingerprint=fingerprint.hexdigest(),
        hunks=tuple(hunks),
        files=tuple(sorted({chunk.path for chunk in chunks})),
        skipped=tuple(sorted({chunk.path for chunk in chunks if chunk.skipped})),
        added=sum(1 for line in content_lines if line.startswith("+")),
        deleted=sum(1 for line in content_lines if line.startswith("-")),
    )


def _digest(path: str, raw: _RawHunk) -> str:
    """Hash the path, git's function header and every hunk line, context included.

    Line positions are left out so an edit above does not change the digest; carriage returns
    are dropped so a CRLF work tree file and its LF-normalized staged copy hash the same.
    """
    body = "\n".join(line.removesuffix("\r") for line in raw.lines)
    payload = f"{path}\0{raw.header}\0{body}\0{raw.salt or ''}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _split_file_sections(diff_text: str) -> list[list[str]]:
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            raise DiffScanError(f"unexpected git diff output before the first file header: {line!r}")
    return sections


def _parse_file_section(lines: list[str]) -> _FileDiff:
    meta, hunks = _parse_section_body(lines)
    status = _section_status(meta)
    old_path, new_path = _section_paths(lines[0], meta)
    path = old_path if status == "deleted" else new_path
    return _file_diff(path, old_path if status == "renamed" else None, status, meta, hunks)


def _parse_section_body(lines: list[str]) -> tuple[dict[str, str], list[_RawHunk]]:
    """Header metadata and content hunks of one `diff --git` section."""
    index = 1
    meta: dict[str, str] = {}
    while index < len(lines) and not lines[index].startswith("@@"):
        _read_header_line(lines[index], meta)
        index += 1
    hunks: list[_RawHunk] = []
    while index < len(lines):
        hunk, index = _parse_hunk(lines, index)
        hunks.append(hunk)
    return meta, hunks


def _file_diff(
    path: str,
    old_path: str | None,
    status: ChangeStatus,
    meta: dict[str, str],
    hunks: Sequence[_RawHunk],
) -> _FileDiff:
    """Build a file's diff; metadata-only changes get a placeholder hunk so they need review."""
    salt = _change_salt(status, old_path, meta)
    notes = _mode_notes(meta)
    if not hunks:
        hunks = [_synthetic_hunk(_describe_contentless_change(status, old_path, meta), salt)]
    elif notes:  # a mode change next to content edits must be visible and digested too
        hunks = [replace(hunks[0], lines=(*notes, *hunks[0].lines)), *hunks[1:]]
    skipped = "binary" in meta or "oversized" in meta or "special" in meta
    return _FileDiff(path, old_path, status, meta, salt, tuple(hunks), skipped)


def _change_salt(status: ChangeStatus, old_path: str | None, meta: dict[str, str]) -> str:
    """Canonical metadata of a change; blob ids only where no content lines carry the change."""
    fields = [status, old_path or ""]
    fields += [f"{key} {meta[key]}" for key in _MODE_KEYS if key in meta]
    if "binary" in meta or "oversized" in meta:
        fields.append(f"index {meta.get('index', '')}")
    return "\n".join(fields)


def _mode_notes(meta: dict[str, str]) -> tuple[str, ...]:
    if "old mode" in meta and "new mode" in meta:
        return (f"~ mode {meta['old mode']} -> {meta['new mode']}",)
    return ()


def _section_status(meta: dict[str, str]) -> ChangeStatus:
    if "new file mode" in meta:
        return "added"
    if "deleted file mode" in meta:
        return "deleted"
    if "rename to" in meta:
        return "renamed"
    return "modified"


def _read_header_line(line: str, meta: dict[str, str]) -> None:
    for key in (*_MODE_KEYS, "rename from", "rename to"):
        if line.startswith(f"{key} "):
            meta[key] = line[len(key) + 1 :]
            return
    if line.startswith("index "):
        meta["index"] = line[len("index ") :].split(" ", 1)[0]  # drop the trailing mode, if any
    elif line.startswith("Binary files ") and line.endswith(" differ"):
        meta["binary"] = line


def _section_paths(git_header: str, meta: dict[str, str]) -> tuple[str, str]:
    """Return (old_path, new_path); without a rename both sides of the `diff --git` line agree."""
    if "rename from" in meta and "rename to" in meta:
        return _unquote(meta["rename from"]), _unquote(meta["rename to"])
    rest = git_header[len("diff --git ") :]
    if rest.startswith('"'):
        end = _closing_quote(rest)
        old_token, new_token = rest[: end + 1], rest[end + 2 :]
    else:
        middle = (len(rest) - 1) // 2  # "a/P b/P": both halves name the same file
        old_token, new_token = rest[:middle], rest[middle + 1 :]
    old = _strip_prefix(_unquote(old_token), "a/")
    new = _strip_prefix(_unquote(new_token), "b/")
    if old != new:
        raise DiffScanError(f"cannot parse file names from git diff header: {git_header!r}")
    return old, new


def _closing_quote(text: str) -> int:
    index = 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index
        index += 1
    raise DiffScanError(f"unterminated quoted path in git diff header: {text!r}")


def _strip_prefix(path: str, prefix: str) -> str:
    if not path.startswith(prefix):
        raise DiffScanError(f"expected git path {path!r} to start with {prefix!r}")
    return path[len(prefix) :]


def _unquote(token: str) -> str:
    """Decode a git C-style quoted path; unquoted tokens are returned unchanged."""
    if len(token) < 2 or not (token.startswith('"') and token.endswith('"')):
        return token
    body = token[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out += char.encode("utf-8")
            index += 1
            continue
        escape = body[index + 1 : index + 2]
        octal = body[index + 1 : index + 4]
        if escape in _C_ESCAPES:
            out.append(_C_ESCAPES[escape])
            index += 2
        elif len(octal) == 3 and all(digit in "01234567" for digit in octal):
            out.append(int(octal, 8))
            index += 4
        else:
            raise DiffScanError(f"invalid escape sequence in quoted git path {token!r}")
    return _decode(bytes(out))


def _parse_hunk(lines: list[str], index: int) -> tuple[_RawHunk, int]:
    """Parse one hunk; `\\ No newline at end of file` markers stay so EOF edits remain visible."""
    match = _HUNK_HEADER_RE.match(lines[index])
    if match is None:
        raise DiffScanError(f"malformed hunk header in git diff output: {lines[index]!r}")
    old_start, new_start = int(match.group(1)), int(match.group(3))
    old_len = 1 if match.group(2) is None else int(match.group(2))
    new_len = 1 if match.group(4) is None else int(match.group(4))
    old_left, new_left = old_len, new_len
    body: list[str] = []
    index += 1
    while old_left > 0 or new_left > 0:
        if index >= len(lines):
            raise DiffScanError(f"truncated hunk in git diff output after {lines[index - 1]!r}")
        line = lines[index]
        index += 1
        tag = line[:1]
        if tag == "\\":
            body.append(line)
            continue
        if tag in (" ", ""):  # diff.suppressBlankEmpty prints blank context lines without a space
            line = line or " "
            old_left -= 1
            new_left -= 1
        elif tag == "-":
            old_left -= 1
        elif tag == "+":
            new_left -= 1
        else:
            raise DiffScanError(f"unexpected line inside a hunk of git diff output: {line!r}")
        if old_left < 0 or new_left < 0:
            raise DiffScanError(f"hunk body does not match its header: {match.group(0)!r}")
        body.append(line)
    while index < len(lines) and lines[index].startswith("\\"):
        body.append(lines[index])
        index += 1
    hunk = _RawHunk(
        old_start, old_len, new_start, new_len, match.group(5).strip(), tuple(body), salt=None
    )
    return hunk, index


def _describe_contentless_change(
    status: ChangeStatus, old_path: str | None, meta: dict[str, str]
) -> tuple[str, ...]:
    if "special" in meta:
        return ("~ untracked directory or special file",)
    notes: list[str] = []
    if status == "renamed":
        notes.append(f"~ renamed from {old_path}")
    notes.extend(_mode_notes(meta))
    if "binary" in meta:
        notes.append("~ binary file changed")
    elif "oversized" in meta:
        notes.append(f"~ file too large to inline: {meta['oversized']} bytes")
    elif status in ("added", "deleted"):
        notes.append(f"~ empty file {status}")
    return tuple(notes) or ("~ file changed without content hunks",)


def _synthetic_hunk(lines: tuple[str, ...], salt: str) -> _RawHunk:
    return _RawHunk(0, 0, 0, 0, "", lines, salt=salt)


# Tool caches that test and build runs create; without these, running the tests in a repository
# lacking a .gitignore would change the diff fingerprint and make every run look stale.
GENERATED_ARTIFACT_PATTERNS = (
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".coverage",
    ".coverage.*",
    "htmlcov/",
    "*.egg-info/",
    "node_modules/",
    ".DS_Store",
    "Thumbs.db",
)
IGNORE_FILE = ".craftsmanignore"


def _untracked_paths(root: Path) -> list[str]:
    args = ["ls-files", "--others", "--exclude-standard", "-z"]
    for pattern in GENERATED_ARTIFACT_PATTERNS:
        args += ["--exclude", pattern]
    if (root / IGNORE_FILE).is_file():
        args.append(f"--exclude-from={IGNORE_FILE}")
    out = git_output(root, *args)
    return [path for path in out.split("\0") if path]


def untracked_paths(cwd: str | Path) -> tuple[str, ...]:
    """Repository-relative paths of untracked, non-ignored files in the work tree."""
    return tuple(_untracked_paths(worktree_root(cwd)))


def staged_paths(cwd: str | Path, base_sha: str) -> tuple[str, ...]:
    """Repository-relative paths staged in the index relative to `base_sha` (both sides of renames)."""
    root = worktree_root(cwd)
    base = _base_revision(root, base_sha)
    out = git_output(root, "diff", "--cached", "--name-only", "--no-renames", "-z", base, "--")
    return tuple(sorted({path for path in out.split("\0") if path}))


def _untracked_file_diff(root: Path, path: str) -> _FileDiff:
    """Describe an untracked file the way `git diff` describes it once staged."""
    file = _repo_file(root, path)
    try:
        if file.is_symlink():
            return _new_text_file_diff(path, "120000", os.fsencode(os.readlink(file)))
        if not file.is_file():
            return _file_diff(path, None, "added", {"special": "1"}, ())
        info = file.stat()
        mode = _work_tree_mode(info)
        if info.st_size > MAX_UNTRACKED_BYTES:
            meta = _new_file_meta(mode, _streamed_blob_id(file, info.st_size))
            return _file_diff(path, None, "added", {**meta, "oversized": str(info.st_size)}, ())
        data = file.read_bytes()
    except OSError as exc:
        raise DiffScanError(f"cannot read untracked file {path}: {exc}") from exc
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        meta = _new_file_meta(mode, _blob_id(data))
        return _file_diff(path, None, "added", {**meta, "binary": "1"}, ())
    return _new_text_file_diff(path, mode, data)


def _unindexed_file_diff(root: Path, base: str, deleted: _FileDiff) -> _FileDiff | None:
    """A file removed from the index but still on disk, diffed against its base content.

    Returns None when the file on disk matches the base exactly.
    """
    path = deleted.path
    file = _repo_file(root, path)
    if file.is_symlink() or not file.is_file():
        added = _untracked_file_diff(root, path)
        return _FileDiff(
            path,
            None,
            "modified",
            {},
            f"{deleted.salt}\n{added.salt}",
            (*deleted.hunks, *added.hunks),
            deleted.skipped or added.skipped,
        )
    try:
        mode = _work_tree_mode(file.stat())
        data = file.read_bytes()
    except OSError as exc:
        raise DiffScanError(f"cannot read {path}: {exc}") from exc
    code, base_data, err = _git_bytes(root, "cat-file", "blob", f"{base}:{path}")
    if code != 0:
        raise DiffScanError(f"cannot read {path} at {base[:12]} (exit {code}): {err.strip()}")
    base_mode = deleted.meta.get("deleted file mode", mode)
    if data == base_data and mode == base_mode:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="craftsman-diff-") as scratch:
            base_copy = Path(scratch) / "base"
            base_copy.write_bytes(base_data)
            code, out, err = _git(
                root,
                "diff",
                "--no-index",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "-U3",
                "--",
                str(base_copy),
                path,
            )
    except OSError as exc:
        raise DiffScanError(f"cannot stage the base copy of {path} for diffing: {exc}") from exc
    if code not in (0, 1):
        raise DiffScanError(f"`git diff --no-index` failed for {path} (exit {code}): {err.strip()}")
    sections = _split_file_sections(out)
    parsed_meta, hunks = _parse_section_body(sections[0]) if sections else ({}, [])
    meta = {"index": f"{_blob_id(base_data)}..{_blob_id(data)}"}
    if "binary" in parsed_meta:
        meta["binary"] = parsed_meta["binary"]
    if mode != base_mode:
        meta.update({"old mode": base_mode, "new mode": mode})
    return _file_diff(path, None, "modified", meta, hunks)


def _work_tree_mode(info: os.stat_result) -> str:
    # Git for Windows does not track the executable bit, so files are staged as 100644 there.
    return "100755" if info.st_mode & 0o111 and os.name != "nt" else "100644"


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def _streamed_blob_id(file: Path, size: int) -> str:
    digest = hashlib.sha1(b"blob %d\0" % size, usedforsecurity=False)
    with file.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _new_file_meta(mode: str, blob: str) -> dict[str, str]:
    return {"new file mode": mode, "index": f"{_NULL_BLOB}..{blob}"}


def _new_text_file_diff(path: str, mode: str, data: bytes) -> _FileDiff:
    meta = _new_file_meta(mode, _blob_id(data))
    text = _decode(data)
    if not text:
        return _file_diff(path, None, "added", meta, ())
    body = text.split("\n")
    missing_newline = body[-1] != ""
    if not missing_newline:
        body.pop()
    lines = [f"+{line}" for line in body]
    if missing_newline:
        lines.append(NO_NEWLINE_MARKER)
    hunk = _RawHunk(0, 0, 1, len(body), "", tuple(lines), salt=None)
    return _file_diff(path, None, "added", meta, (hunk,))


def _walk(hunk: Hunk) -> Iterator[tuple[str, int, int, str]]:
    """Yield (tag, old line, new line, text) per body line; the inactive side is the cursor."""
    old_no, new_no = hunk.old_start, hunk.new_start
    for line in hunk.lines:
        tag, text = line[:1], line[1:]
        if tag in ("~", "\\"):
            continue
        yield tag, old_no, new_no, text
        if tag in (" ", "-"):
            old_no += 1
        if tag in (" ", "+"):
            new_no += 1


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
        if hunk.added_lines:
            return self.scope(hunk.path, hunk.added_lines[0][0])
        lines = _touched_new_lines(hunk)
        return self.scope(hunk.path, lines[0]) if lines else None

    def symbols(self, hunk: Hunk) -> tuple[str, ...]:
        """Names defined on the hunk's changed lines plus the names of the scopes it touches.

        Comments, docstrings and prose never yield names: Python definitions come from the
        syntax tree when the file parses, other lines must use definition syntax.
        """
        if _is_docs_hunk(hunk):
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
        mentions = self.mentions(symbol)
        ranked = sorted(dict.fromkeys(ref.path for ref in mentions), key=lambda path: path in defined_in)
        by_path: dict[str, list[Reference]] = {}
        for ref in mentions:
            by_path.setdefault(ref.path, []).append(ref)
        found = [ref for path in ranked for ref in by_path[path]]
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

    def _source(self, path: str) -> _Source | None:
        if path not in self._sources:
            self._sources[path] = _load_source(self.root, self._real_root, path)
        return self._sources[path]

    def _untracked_paths(self) -> tuple[str, ...]:
        if self._untracked is None:
            self._untracked = tuple(_untracked_paths(self.root))
        return self._untracked

    def _tracked_files_mentioning(self, symbol: str) -> list[str]:
        """Tracked work tree files with a whole-word match; `-l` stops reading each at the first."""
        code, out, err = _git(self.root, "grep", "-l", "-I", "-w", "-F", "-z", "-e", symbol, "--")
        if code == 1:
            return []
        if code != 0:
            raise DiffScanError(f"`git grep` for {symbol!r} failed (exit {code}): {err.strip()}")
        return [path for path in out.split("\0") if path]


def _load_source(root: Path, real_root: Path, path: str) -> _Source | None:
    file = _repo_file(root, path)
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
    return _Source(path, _decode(data))


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
    lines = (_anchor_line(tag, new_no) for tag, _, new_no, _ in _walk(hunk) if tag in ("+", "-"))
    return tuple(dict.fromkeys(lines))


def _anchor_line(tag: str, new_no: int) -> int:
    """An added line stands for itself; a removed line for the surviving line just above it.

    The line now at a deletion's place may already belong to the next definition, the line above
    still belongs to the one the deletion was made in.
    """
    return new_no if tag == "+" else max(1, new_no - 1)


def _is_meaningful_symbol(name: str) -> bool:
    return len(name) >= 3 and name not in _SYMBOL_STOPLIST and _IDENTIFIER_RE.fullmatch(name) is not None


# --- signals --------------------------------------------------------------------------------


def detect_signals(hunks: Sequence[Hunk]) -> dict[str, tuple[str, ...]]:
    """Map each risk signal that fired to the ids of the hunks that triggered it, in hunk order."""
    fired: dict[str, list[str]] = {}
    for hunk in hunks:
        for signal in _hunk_signals(hunk):
            fired.setdefault(signal, []).append(hunk.id)
    return {signal: tuple(fired[signal]) for signal in SIGNALS if signal in fired}


def _hunk_signals(hunk: Hunk) -> set[str]:
    signals = {name for name, matches in _PATH_SIGNALS.items() if matches(hunk.path)}
    if _is_docs_hunk(hunk):
        signals.add("docs")
    else:
        content = "\n".join(text for _, text in (*hunk.added_lines, *hunk.removed_lines))
        signals.update(
            name for name, pattern in CONTENT_SIGNAL_PATTERNS.items() if pattern.search(content)
        )
    if hunk.status == "deleted" or any(text.strip() for _, text in hunk.removed_lines):
        signals.add("deletion")
    return signals


def _name_matches(name: str, globs: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, glob) for glob in globs)


def files_without_signal_patterns(snap: DiffSnapshot) -> tuple[str, ...]:
    """Changed files that add code in a language the risk-signal patterns were not written for.

    No signal firing for these files says nothing about their risks, e.g. a `curl` in a shell
    script or a Dockerfile `RUN` step.
    """
    paths = dict.fromkeys(
        hunk.path for hunk in snap.hunks if hunk.added_lines and not _is_docs_hunk(hunk)
    )
    return tuple(path for path in paths if _outside_signal_patterns(path))


def _outside_signal_patterns(path: str) -> bool:
    posix = PurePosixPath(path)
    suffix = posix.suffix.lower()
    if suffix in _DATA_SUFFIXES or _name_matches(posix.name, _PATTERN_LANGUAGE_NAME_GLOBS):
        return False
    return suffix not in _PATTERN_LANGUAGE_SUFFIXES


def _is_config(path: str) -> bool:
    posix = PurePosixPath(path)
    return (
        posix.suffix in _CONFIG_SUFFIXES
        or posix.name in _CONFIG_NAMES
        or _name_matches(posix.name, _CONFIG_NAME_GLOBS)
        or posix.parts[:2] == (".github", "workflows")
    )


def is_test_path(path: str) -> bool:
    """Test code by location or name, e.g. `tests/x.py`, `test_x.py`, `x.spec.ts`, `x_test.go`."""
    posix = PurePosixPath(path)
    return bool(_TEST_DIRS.intersection(posix.parts[:-1])) or _name_matches(posix.name, _TEST_NAME_GLOBS)


def is_docs_path(path: str) -> bool:
    """Prose by file type alone: a `docs/` folder can hold code (e.g. Sphinx's conf.py)."""
    return PurePosixPath(path).suffix.lower() in _DOCS_SUFFIXES and not _is_config(path)


def _is_docs_hunk(hunk: Hunk) -> bool:
    """A rename counts as documentation only when both its old and new paths are documentation."""
    return is_docs_path(hunk.path) and (hunk.old_path is None or is_docs_path(hunk.old_path))


_PATH_SIGNALS = {"config": _is_config, "tests": is_test_path}
