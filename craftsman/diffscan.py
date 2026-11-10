"""Deterministic snapshots of a work tree diff: hunks, digests and risk signals.

Everything here is read-only. Paths are repository-root-relative POSIX strings no matter which
subdirectory the caller starts from, and every changed file yields at least one hunk, so nothing
that changed can escape review. Scopes, symbols and references live in `craftsman.worktree_index`, the path
classifiers (config, tests, docs, data, agent prompts) in `craftsman.diffscan_paths`.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from craftsman.diffscan_paths import (
    is_agent_prompt_path,
    is_config_path,
    is_docs_path,
    is_test_path,
    outside_signal_patterns,
)
from craftsman.diffscan_signals import CONTENT_SIGNAL_PATTERNS, DEEP_HINT_PATTERNS

ChangeStatus = Literal["modified", "added", "deleted", "renamed"]

GIT_TIMEOUT_S = 60.0
MAX_UNTRACKED_BYTES = 1_048_576  # 1 MiB
BINARY_SNIFF_BYTES = 8000  # the prefix git itself checks for NUL bytes
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
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
class DiffScanError(RuntimeError):
    """Raised when the repository or a file in it cannot be inspected."""


class GitTimeoutError(DiffScanError):
    """A git call ran out of time: its own limit (`GIT_TIMEOUT_S`) or the budget of the hook it ran in."""


@dataclass(frozen=True)
class _GitBudget:
    seconds: float
    deadline: float  # `time.monotonic()` value
    on_timeout: Callable[[str], None] | None


_GIT_BUDGET: ContextVar[_GitBudget | None] = ContextVar("craftsman_git_budget", default=None)


@contextmanager
def git_budget(seconds: float, on_timeout: Callable[[str], None] | None = None) -> Iterator[None]:
    """Everything that runs git inside this block shares `seconds` of time (a hook must answer within its platform's timeout).

    Each git call gets what is left of the budget, at most `GIT_TIMEOUT_S`; a call that finds it spent, or runs out
    of it, raises `GitTimeoutError`. `on_timeout` receives the message of every timeout before it is raised, so a
    caller that catches the error somewhere in the middle still leaves a trace (the hooks log it through
    `hook_errors`). Nested blocks cannot extend the budget they sit in.
    """
    outer = _GIT_BUDGET.get()
    deadline = time.monotonic() + seconds
    if outer is not None:
        deadline = min(deadline, outer.deadline)
    token = _GIT_BUDGET.set(_GitBudget(seconds, deadline, on_timeout))
    try:
        yield
    finally:
        _GIT_BUDGET.reset(token)


def git_time_left(limit: float | None = None) -> float:
    """The seconds the next git call may take: `limit`, or less when a `git_budget` is nearly spent; 0 when it is spent."""
    limit = GIT_TIMEOUT_S if limit is None else limit
    budget = _GIT_BUDGET.get()
    if budget is None:
        return limit
    return max(0.0, min(limit, budget.deadline - time.monotonic()))


def note_git_timeout(message: str) -> None:
    """Tell the `on_timeout` of the surrounding `git_budget`, if any, that a git call ran out of time."""
    budget = _GIT_BUDGET.get()
    if budget is not None and budget.on_timeout is not None:
        budget.on_timeout(message)


def git_timed_out(message: str) -> GitTimeoutError:
    """The error for a git call that ran out of time, after `note_git_timeout`."""
    note_git_timeout(message)
    return GitTimeoutError(message)


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
        return tuple((new_no, text) for tag, _, new_no, text in walk_hunk(self) if tag == "+")

    @property
    def removed_lines(self) -> tuple[tuple[int, str], ...]:
        return tuple((old_no, text) for tag, old_no, _, text in walk_hunk(self) if tag == "-")


@dataclass(frozen=True)
class DiffSnapshot:
    base_sha: str
    fingerprint: str
    hunks: tuple[Hunk, ...]
    files: tuple[str, ...]
    added: int
    deleted: int

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def docs_only(self) -> bool:
        return hunks_docs_only(self.hunks)

    def only(self, hunk_ids: Collection[str]) -> DiffSnapshot:
        """The part of this snapshot made of the hunks named in `hunk_ids`."""
        keep = set(hunk_ids)
        hunks = tuple(hunk for hunk in self.hunks if hunk.id in keep)
        lines = [line for hunk in hunks for line in hunk.lines]
        return replace(
            self,
            hunks=hunks,
            files=tuple(sorted({hunk.path for hunk in hunks})),
            added=sum(1 for line in lines if line.startswith("+")),
            deleted=sum(1 for line in lines if line.startswith("-")),
        )


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


# --- git plumbing ---------------------------------------------------------------------------


def decode_text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _existing_dir(cwd: str | Path) -> Path:
    directory = Path(cwd)
    if not directory.is_dir():
        raise DiffScanError(f"not a directory: {directory}")
    return directory


def git_bytes(cwd: Path, *args: str) -> tuple[int, bytes, str]:
    """Run git with stable, unlocalized output; return (returncode, raw stdout, decoded stderr)."""
    argv = ["git", "-c", "core.quotepath=off", "-c", "color.ui=never", *args]
    timeout = git_time_left()
    if timeout <= 0:
        raise git_timed_out(f"`git {args[0]}` was not run in {cwd}: the hook's git budget is spent")
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env={**os.environ, **_GIT_ENV},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise DiffScanError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise git_timed_out(f"`git {args[0]}` timed out after {timeout:g}s in {cwd}") from exc
    except OSError as exc:
        raise DiffScanError(f"cannot run git in {cwd}: {exc}") from exc
    return proc.returncode, proc.stdout, decode_text(proc.stderr)


def git_text(cwd: Path, *args: str) -> tuple[int, str, str]:
    """Like `git_bytes`, with stdout decoded as UTF-8."""
    code, out, err = git_bytes(cwd, *args)
    return code, decode_text(out), err


def git_output(cwd: Path, *args: str) -> str:
    """Stdout of a git command that must succeed; failures raise `DiffScanError` with stderr."""
    code, out, err = git_text(cwd, *args)
    if code != 0:
        raise DiffScanError(f"`git {' '.join(args)}` failed in {cwd} (exit {code}): {err.strip()}")
    return out


def _path_line(line: str) -> str:
    """`line` without the line ending git adds (`\\n`, or `\\r\\n` from Git for Windows), keeping blanks."""
    line = line.removesuffix("\n")
    return line.removesuffix("\r")


def worktree_root(cwd: str | Path) -> Path:
    """Return the root of the git work tree containing `cwd`."""
    directory = _existing_dir(cwd)
    code, out, err = git_text(directory, "rev-parse", "--show-toplevel")
    if code != 0 or not _path_line(out):
        raise DiffScanError(f"{directory} is not inside a git work tree: {err.strip()}")
    return Path(_path_line(out))


def git_dir(cwd: str | Path) -> Path:
    """Return the absolute, per-worktree git directory for `cwd`."""
    directory = _existing_dir(cwd)
    code, out, err = git_text(directory, "rev-parse", "--absolute-git-dir")
    if code != 0 or not _path_line(out):
        raise DiffScanError(f"{directory} is not inside a git repository: {err.strip()}")
    return Path(_path_line(out)).resolve()


def worktree_paths(cwd: str | Path) -> tuple[Path, Path]:
    """`worktree_root` and `git_dir` of `cwd`, from one git call.

    Raises `DiffScanError` like `worktree_root` where there is no work tree (in a bare repository too).
    """
    directory = _existing_dir(cwd)
    code, out, err = git_text(directory, "rev-parse", "--show-toplevel", "--absolute-git-dir")
    if code != 0 or not out.strip():
        raise DiffScanError(f"{directory} is not inside a git work tree: {err.strip()}")
    lines = [_path_line(line + "\n") for line in out.split("\n")]
    if len(lines) == 3 and not lines[2].strip() and lines[0] and lines[1]:
        return Path(lines[0]), Path(lines[1]).resolve()
    # A line break in a path makes the two answers ambiguous: ask for each on its own.
    return worktree_root(directory), git_dir(directory)


def commit_sha(cwd: str | Path, ref: str) -> str | None:
    """Full SHA of the commit `ref` names, or None when it names no commit (e.g. an unborn HEAD)."""
    if not ref or ref.startswith("-"):
        raise DiffScanError(f"invalid git revision {ref!r}")
    code, out, _ = git_text(_existing_dir(cwd), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if code == 0 and out.strip() else None


def base_revision(root: Path, ref: str) -> str:
    """`resolve_base_sha` for a caller that has the work tree root: `ref` as a commit SHA, or the empty
    tree for HEAD before the first commit (unborn or orphan branch)."""
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
    return base_revision(worktree_root(cwd), ref or "HEAD")


def repo_file(root: Path, path: str) -> Path:
    parts = PurePosixPath(path).parts
    candidate = Path(os.path.normpath(root.joinpath(*parts)))
    if not parts or ".." in parts or not candidate.is_relative_to(root):
        raise DiffScanError(f"expected a repository-relative POSIX path, got {path!r}")
    return candidate


# --- snapshot -------------------------------------------------------------------------------


def is_relative_posix_path(path: str) -> bool:
    """True for a normalized repository-relative POSIX file path: no drive, `.`, `..` or `//`."""
    if not path or path.startswith("/") or path.endswith("/") or "\\" in path or "\0" in path:
        return False
    parts = path.split("/")
    has_drive = len(parts[0]) >= 2 and parts[0][1] == ":" and parts[0][0].isalpha()
    return not has_drive and not any(part in ("", ".", "..") for part in parts)


def _normalize_filter_path(path: str) -> str:
    """`path` itself once it is a relative POSIX file path, or a directory with a trailing `/`."""
    if not is_relative_posix_path(path.removesuffix("/")):
        raise DiffScanError(f"expected a repository-relative POSIX path, got {path!r}")
    return path


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
    return snapshot_at(root, base_revision(root, base_sha), paths)


def snapshot_at(root: Path, base: str, paths: Sequence[str] | None = None) -> DiffSnapshot:
    """`snapshot` for a caller that has the work tree `root` and a `base` that `base_revision` or
    `resolve_base_sha` returned: git is asked for neither again."""
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
    untracked = _filter_untracked(list_untracked_paths(root), filters)
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
    return decode_text(bytes(out))


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


def list_untracked_paths(root: Path) -> list[str]:
    args = ["ls-files", "--others", "--exclude-standard", "-z"]
    for pattern in GENERATED_ARTIFACT_PATTERNS:
        args += ["--exclude", pattern]
    if (root / IGNORE_FILE).is_file():
        args.append(f"--exclude-from={IGNORE_FILE}")
    out = git_output(root, *args)
    return [path for path in out.split("\0") if path]


def untracked_paths(cwd: str | Path) -> tuple[str, ...]:
    """Repository-relative paths of untracked, non-ignored files in the work tree."""
    return tuple(list_untracked_paths(worktree_root(cwd)))


def staged_paths(cwd: str | Path, base_sha: str) -> tuple[str, ...]:
    """Repository-relative paths staged in the index relative to `base_sha` (both sides of renames)."""
    root = worktree_root(cwd)
    base = base_revision(root, base_sha)
    out = git_output(root, "diff", "--cached", "--name-only", "--no-renames", "-z", base, "--")
    return tuple(sorted({path for path in out.split("\0") if path}))


def _untracked_file_diff(root: Path, path: str) -> _FileDiff:
    """Describe an untracked file the way `git diff` describes it once staged."""
    file = repo_file(root, path)
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
    file = repo_file(root, path)
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
    code, base_data, err = git_bytes(root, "cat-file", "blob", f"{base}:{path}")
    if code != 0:
        raise DiffScanError(f"cannot read {path} at {base[:12]} (exit {code}): {err.strip()}")
    base_mode = deleted.meta.get("deleted file mode", mode)
    if data == base_data and mode == base_mode:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="craftsman-diff-") as scratch:
            base_copy = Path(scratch) / "base"
            base_copy.write_bytes(base_data)
            code, out, err = git_text(
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
    text = decode_text(data)
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


def walk_hunk(hunk: Hunk) -> Iterator[tuple[str, int, int, str]]:
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
    if is_docs_hunk_paths(hunk):
        signals.add("docs")
    else:
        content = "\n".join(text for _, text in (*hunk.added_lines, *hunk.removed_lines))
        signals.update(
            name for name, pattern in CONTENT_SIGNAL_PATTERNS.items() if pattern.search(content)
        )
    if hunk.status == "deleted" or any(text.strip() for _, text in hunk.removed_lines):
        signals.add("deletion")
    return signals


def files_without_signal_patterns(snap: DiffSnapshot) -> tuple[str, ...]:
    """Changed files that add code in a language the risk-signal patterns were not written for.

    No signal firing for these files says nothing about their risks, e.g. a `curl` in a shell
    script or a Dockerfile `RUN` step.
    """
    paths = dict.fromkeys(
        hunk.path for hunk in snap.hunks if hunk.added_lines and not is_docs_hunk_paths(hunk)
    )
    return tuple(path for path in paths if outside_signal_patterns(path))


def hunks_docs_only(hunks: Sequence[Hunk]) -> bool:
    """Whether every changed path (renames included) is documentation, not code or an agent prompt."""
    paths = [path for hunk in hunks for path in (hunk.path, hunk.old_path) if path is not None]
    return bool(paths) and all(is_docs_path(path) and not is_agent_prompt_path(path) for path in paths)


def detect_deep_hint_signals(hunks: Sequence[Hunk]) -> dict[str, tuple[str, ...]]:
    """The hunks with real security or concurrency usage, in production code only.

    Tests, configuration and documentation never count, and a bare word such as `token` does not:
    the hint needs a call or usage pattern.
    """
    fired: dict[str, list[str]] = {}
    for hunk in hunks:
        if is_test_path(hunk.path) or is_config_path(hunk.path) or is_docs_hunk_paths(hunk):
            continue
        content = "\n".join(text for _, text in (*hunk.added_lines, *hunk.removed_lines))
        for name, pattern in DEEP_HINT_PATTERNS.items():
            if pattern.search(content):
                fired.setdefault(name, []).append(hunk.id)
    return {name: tuple(ids) for name, ids in fired.items()}


def is_docs_hunk(hunk: Hunk) -> bool:
    """Whether the hunk alone is documentation: its path (and its old path, for a rename) is prose, not an agent prompt."""
    return hunks_docs_only([hunk])


def is_docs_hunk_paths(hunk: Hunk) -> bool:
    """A rename counts as documentation only when both its old and new paths are documentation."""
    return is_docs_path(hunk.path) and (hunk.old_path is None or is_docs_path(hunk.old_path))


_PATH_SIGNALS = {"config": is_config_path, "tests": is_test_path}
