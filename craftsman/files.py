"""File operations the installer and the exporter share: atomic writes, backups and removal."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path

# Replaced user files are moved into this folder next to them: inside it a skill folder is two
# levels deep, so no agent discovers the backup as a second copy of the skill.
BACKUP_DIR_NAME = ".craftsman-backup"
REPLACE_RETRIES = 5
REPLACE_RETRY_S = 0.05


class UnsafePathError(RuntimeError):
    """A path cannot be written or removed without following a symlink out of its folder."""


def write_bytes_atomic(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace `path` atomically; a symlinked file (dotfiles) is updated where it really lives.

    `mode` defaults to the permissions of the replaced file, or 0o644 for a new one. The data is
    flushed to disk first: without that, a crash right after the rename can leave an empty file.
    """
    path = path.resolve() if path.is_symlink() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, staged = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(staged, mode)
        replace_with_retry(Path(staged), path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def replace_with_retry(source: Path, target: Path) -> None:
    """`os.replace`, retried briefly: Windows refuses while another process has the file open."""
    for attempt in range(REPLACE_RETRIES + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES:
                raise
            time.sleep(REPLACE_RETRY_S)


def write_text_atomic(path: Path, text: str) -> None:
    write_bytes_atomic(path, text.encode("utf-8"))


def copy_file(source: Path, target: Path, *, executable: bool = False) -> None:
    """Copy `source` over `target`; a symlink at `target` is replaced, never written through."""
    if target.is_symlink():
        target.unlink()
    mode = 0o755 if executable else (source.stat().st_mode & 0o777 or 0o644)
    write_bytes_atomic(target, source.read_bytes(), mode=mode)


def exists(path: Path) -> bool:
    """Whether anything is at `path`, a dangling symlink included."""
    return path.exists() or path.is_symlink()


def move_aside(path: Path, *, dry_run: bool) -> Path:
    """Move `path` into the backup folder next to it; return where it went (or would go)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = path.parent / BACKUP_DIR_NAME
    backup = backup_dir / f"{path.name}.{stamp}"
    counter = 1
    while exists(backup):
        counter += 1
        backup = backup_dir / f"{path.name}.{stamp}.{counter}"
    if not dry_run:
        backup_dir.mkdir(exist_ok=True)
        os.replace(path, backup)
    return backup


def copy_once_backup(path: Path, *, dry_run: bool) -> Path | None:
    """Copy `path` to `<path>.bak` before Craftsman first changes it; None when that copy exists."""
    backup = path.with_name(path.name + ".bak")
    if exists(backup):
        return None
    if not dry_run:
        shutil.copy2(path, backup)
    return backup


def _stop_set(stop_roots: Collection[Path]) -> set[Path]:
    """The stop roots as written and as resolved, so a symlinked home (macOS /var) still stops."""
    stops = {Path(os.path.abspath(root)) for root in stop_roots}
    return stops | {root.resolve() for root in stops}


def has_symlinked_parent(path: Path, stop_roots: Collection[Path]) -> bool:
    """Whether a folder between `path` and the nearest stop root is a symlink."""
    stops = _stop_set(stop_roots)
    current = Path(os.path.abspath(path)).parent
    while current != current.parent and current not in stops and current.resolve() not in stops:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def require_no_symlinked_parent(path: Path, stop_roots: Collection[Path]) -> None:
    if has_symlinked_parent(path, stop_roots):
        raise UnsafePathError(f"refusing to touch {path}: a folder above it is a symlink")


def remove_path(path: Path, *, stop_roots: Collection[Path], dry_run: bool) -> bool:
    """Remove the file, symlink or folder at `path`; return whether anything was there."""
    require_no_symlinked_parent(path, stop_roots)
    if not exists(path):
        return False
    if not dry_run:
        if path.is_symlink() or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path)
    return True


def is_within(path: Path, root: Path) -> bool:
    """Whether `path` lies inside `root`, both taken lexically and with symlinks resolved."""
    absolute, absolute_root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if absolute_root not in absolute.parents:
        return False
    return absolute.parent.resolve().is_relative_to(absolute_root.resolve())
