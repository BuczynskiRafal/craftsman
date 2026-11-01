"""Craftsman-generated file markers, JSON object helpers, and managed Markdown blocks."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from craftsman.files import copy_once_backup, exists, move_aside, write_text_atomic

CRAFTSMAN_BEGIN_MARKER = "<!-- craftsman:begin -->"
CRAFTSMAN_END_MARKER = "<!-- craftsman:end -->"
CRAFTSMAN_GENERATED_MARKER = "<!-- craftsman:generated -->"


class ExportError(RuntimeError):
    """An export target cannot be written without losing or corrupting data."""


def is_craftsman_generated_file(path: Path) -> bool:
    """Whether `path` carries the marker Craftsman writes into every file it generates."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return CRAFTSMAN_GENERATED_MARKER in text


@dataclass(frozen=True)
class FileChange:
    """What writing one file did: whether it changed, and where a user's copy was kept."""

    path: Path
    changed: bool
    backup: Path | None = None


def write_generated(path: Path, text: str, *, owned: Callable[[Path], bool]) -> FileChange:
    """Write a generated file; a file in the way that `owned` does not claim is moved aside."""
    backup = None if not exists(path) or owned(path) else move_aside(path, dry_run=False)
    if backup is None and path.is_file() and not path.is_symlink():
        if path.read_bytes() == text.encode("utf-8"):
            return FileChange(path, changed=False)
    if path.is_symlink():  # never write through a link into the file it points to
        path.unlink()
    write_text_atomic(path, text)
    return FileChange(path, changed=True, backup=backup)


def write_marked(path: Path, text: str) -> FileChange:
    return write_generated(path, text, owned=is_craftsman_generated_file)


def read_json_object(path: Path) -> dict[str, Any]:
    """The JSON object in `path`; anything else is an error, so a user's file is never clobbered."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"refusing to modify {path}: cannot read it as JSON ({exc})") from exc
    if not isinstance(loaded, dict):
        raise ExportError(f"refusing to modify {path}: the top level is not a JSON object")
    return loaded


def write_json_object(path: Path, data: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def rewrite_json(
    path: Path, original: dict[str, Any], updated: dict[str, Any], *, backup: bool, dry_run: bool
) -> FileChange:
    if updated == original and exists(path):
        return FileChange(path, changed=False)
    kept = copy_once_backup(path, dry_run=dry_run) if backup else None
    if not dry_run:
        write_json_object(path, updated)
    return FileChange(path, changed=True, backup=kept)


def find_managed_block(text: str, source: Path) -> tuple[int, int] | None:
    """The span of the Craftsman block in `text`, its trailing newline included, or None when the
    text has no markers. Markers that do not form one block are an error: cutting at them could
    take the user's text with the block."""
    begins, ends = text.count(CRAFTSMAN_BEGIN_MARKER), text.count(CRAFTSMAN_END_MARKER)
    if begins == ends == 0:
        return None
    begin = text.find(CRAFTSMAN_BEGIN_MARKER)
    end = text.find(CRAFTSMAN_END_MARKER)
    if begins != 1 or ends != 1 or end < begin:
        raise ExportError(
            f"refusing to modify {source}: its {CRAFTSMAN_BEGIN_MARKER} and {CRAFTSMAN_END_MARKER} "
            "markers do not enclose one block; fix them by hand"
        )
    end += len(CRAFTSMAN_END_MARKER)
    return begin, end + 1 if text[end : end + 1] == "\n" else end


def read_markdown(target_file: Path) -> str:
    try:
        return target_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"refusing to modify {target_file}: cannot read it as UTF-8 ({exc})") from exc


def require_resolves_within(path: Path, root: Path) -> None:
    """A symlinked workspace file is updated where it lives, which must be inside `root`."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExportError(f"refusing to modify {path}: it resolves outside {root}")


def upsert_managed_markdown(
    target_file: Path, inner_content: str, *, within: Path, dry_run: bool = False
) -> FileChange:
    """Insert or update the Craftsman block, keeping everything else in the file.

    A file without the block gets it appended, after a one-time `.bak` copy of the original.
    """
    require_resolves_within(target_file, within)
    block = f"{CRAFTSMAN_BEGIN_MARKER}\n{inner_content.strip()}\n{CRAFTSMAN_END_MARKER}\n"
    existing = read_markdown(target_file) if exists(target_file) else None
    span = find_managed_block(existing, target_file) if existing is not None else None
    if existing is None:
        updated = block
    elif span is not None:
        updated = existing[: span[0]] + block + existing[span[1] :]
    else:
        updated = (f"{existing.rstrip()}\n\n" if existing.strip() else "") + block
    if updated == existing:
        return FileChange(target_file, changed=False)
    kept = copy_once_backup(target_file, dry_run=dry_run) if existing and span is None else None
    if not dry_run:
        write_text_atomic(target_file, updated)
    return FileChange(target_file, changed=True, backup=kept)


def strip_managed_block(text: str, source: Path) -> str:
    """`text` without the Craftsman block, joining what surrounded it."""
    span = find_managed_block(text, source)
    if span is None:
        return text
    before, after = text[: span[0]].rstrip(), text[span[1] :].strip()
    joined = "\n\n".join(part for part in (before, after) if part)
    return f"{joined}\n" if joined else ""
