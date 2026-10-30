"""What one install target writes, tracked so its manifest lists exactly what Craftsman owns.

`Tracker` collects what happened for the report; `TargetSession` stages the paths of one target
under its base folder and saves the target's manifest entry once the target is complete.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from craftsman.export import (
    COPY_IGNORED_NAMES,
    env_var_for_home,
    resolve_craftsman_pkg_dir,
)
from craftsman.files import (
    UnsafePathError,
    copy_file,
    copy_once_backup,
    exists,
    is_within,
    move_aside,
    remove_path,
    require_no_symlinked_parent,
    write_text_atomic,
)
from craftsman.generated import (
    ExportError,
    FileChange,
    read_json_object,
    read_markdown,
    strip_managed_block,
    upsert_managed_markdown,
    write_json_object,
)
from craftsman.manifest import (
    InstallError,
    InstallManifest,
    ModifiedKind,
    TargetKind,
    TargetManifest,
    load_manifest,
    save_manifest,
)


class InstallMode(str, Enum):
    COPY = "copy"
    SYMLINK = "symlink"
    PLUGIN = "plugin"


class EventKind(Enum):
    INSTALLED = "+"
    UNCHANGED = "="
    BACKED_UP = "!"
    REMOVED = "-"
    RESTORED = "~"
    NOTE = "*"
    WARNING = "WARN"


@dataclass(frozen=True)
class Event:
    kind: EventKind
    text: str


@dataclass
class Tracker:
    """Everything an install or uninstall did (or, on a dry run, would do), in order."""

    dry_run: bool
    events: list[Event] = field(default_factory=list)

    def add(self, kind: EventKind, text: str | Path) -> None:
        self.events.append(Event(kind, str(text)))

    def backed_up(self, original: Path, backup: Path) -> None:
        self.add(EventKind.BACKED_UP, f"{original} -> {backup}")

    def count(self, kind: EventKind) -> int:
        return sum(1 for event in self.events if event.kind is kind)

    def render(self, out: TextIO, err: TextIO) -> None:
        for event in self.events:
            if event.kind is EventKind.WARNING:
                err.write(f"[WARN] {event.text}\n")
            else:
                out.write(f"  {event.kind.value} {event.text}\n")


def stop_roots_for(home: Path, *extra: Path) -> frozenset[Path]:
    """Folders above which Craftsman never records, creates or checks parents: the home folder,
    the configured folders outside it, and the workspaces given on the command line."""
    abs_home = Path(os.path.abspath(home))
    roots = {abs_home, *(Path(os.path.abspath(item)) for item in extra)}
    for env_key in ("XDG_CONFIG_HOME", "APPDATA", "CRAFTSMAN_HOME"):
        value = env_var_for_home(env_key, home)
        if value and not Path(os.path.abspath(value)).is_relative_to(abs_home):
            roots.add(Path(os.path.abspath(value)).parent)
    return frozenset(roots)


def path_key(path: Path) -> str:
    """How the manifest spells `path`."""
    return os.path.abspath(path)


@dataclass
class TargetSession:
    """The paths one target creates and the user files it changes under `base_dir`."""

    base_dir: Path
    kind: TargetKind
    tracker: Tracker
    manifest: InstallManifest | None
    stop_roots: frozenset[Path]
    created_paths: list[str] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)
    modified_files: dict[str, ModifiedKind] = field(default_factory=dict)
    hooks_json_keys: tuple[str, ...] = ()

    @classmethod
    def begin(
        cls, base_dir: Path, kind: TargetKind, home: Path, tracker: Tracker, *workspaces: Path
    ) -> TargetSession:
        return cls(base_dir, kind, tracker, load_manifest(base_dir), stop_roots_for(home, *workspaces))

    @property
    def dry_run(self) -> bool:
        return self.tracker.dry_run

    @property
    def previous(self) -> TargetManifest | None:
        return self.manifest.targets.get(self.kind) if self.manifest else None

    def owns(self, path: Path) -> bool:
        """Whether a Craftsman target of this base folder created `path` or a folder above it."""
        owned = set(self.manifest.created_paths) if self.manifest else set()
        absolute = Path(path_key(path))
        return str(absolute) in owned or any(str(parent) in owned for parent in absolute.parents)

    def make_dirs(self, directory: Path) -> None:
        """Create `directory`, recording each folder Craftsman created (or another target did)."""
        known = set(self.manifest.created_dirs) if self.manifest else set()
        chain: list[str] = []
        current = Path(path_key(directory))
        while current not in self.stop_roots and current != current.parent:
            if exists(current) and str(current) not in known:
                break
            chain.append(str(current))
            current = current.parent
        for created in reversed(chain):
            if created not in self.created_dirs:
                self.created_dirs.append(created)
        if not self.dry_run:
            directory.mkdir(parents=True, exist_ok=True)

    def clear(self, target: Path) -> None:
        """Free `target`: a path Craftsman owns is removed, anything else is moved aside."""
        require_no_symlinked_parent(target, self.stop_roots)
        if not exists(target):
            return
        if self.owns(target):
            remove_path(target, stop_roots=self.stop_roots, dry_run=self.dry_run)
        else:
            self.tracker.backed_up(target, move_aside(target, dry_run=self.dry_run))

    def stage(self, target: Path) -> None:
        """Free `target` and record it as created by this target."""
        self.clear(target)
        self.created(target)

    def created(self, target: Path) -> None:
        if path_key(target) not in self.created_paths:
            self.created_paths.append(path_key(target))
        self.tracker.add(EventKind.INSTALLED, target)

    def merged_hooks_json(self, path: Path, keys: Collection[str]) -> None:
        self.modified(path, ModifiedKind.HOOKS_JSON)
        self.hooks_json_keys = tuple(sorted(keys))

    def modified(self, path: Path, kind: ModifiedKind) -> None:
        self.modified_files[path_key(path)] = kind

    def finish(self, *, previous_hooks_path: str | None = None, hooks_dir: str | None = None) -> None:
        """Remove what the previous install of this target had and this one no longer has, then
        record the target in the manifest."""
        self._remove_obsolete()
        target = TargetManifest(
            created_paths=tuple(self.created_paths),
            created_dirs=tuple(self.created_dirs),
            modified_files=self.modified_files,
            hooks_json_keys=self.hooks_json_keys,
            previous_hooks_path=previous_hooks_path,
            hooks_dir=hooks_dir,
        )
        manifest = self.manifest or InstallManifest({})
        save_manifest(self.base_dir, manifest.with_target(self.kind, target), dry_run=self.dry_run)

    def _remove_obsolete(self) -> None:
        if self.previous is None or self.manifest is None:
            return
        # A user file Craftsman created and merged into is the user's once they edit it: reverting
        # Craftsman's part removes the file only when nothing else is left in it.
        keep = {
            *self.created_paths,
            *self.manifest.paths_of_others(self.kind),
            *self.previous.modified_files,
        }
        for old in sorted(self.previous.created_paths, key=lambda p: len(Path(p).parts), reverse=True):
            if old in keep or any(Path(old).is_relative_to(new) for new in self.created_paths):
                continue
            if remove_path(Path(old), stop_roots=self.stop_roots, dry_run=self.dry_run):
                self.tracker.add(EventKind.REMOVED, old)


def install_tree(
    session: TargetSession, source: Path, target: Path, mode: InstallMode, *, executable: bool = False
) -> None:
    """Install `source` (a file or a folder) at `target` as a copy or a symlink; `executable`
    makes a copied file runnable whatever the checkout's permissions."""
    session.stage(target)
    if mode is not InstallMode.COPY:
        if not session.dry_run:
            target.symlink_to(source, target_is_directory=source.is_dir())
        return
    if source.is_file():
        if not session.dry_run:
            copy_file(source, target, executable=executable)
        return
    session.make_dirs(target)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if item.is_file() and not any(part in COPY_IGNORED_NAMES for part in relative.parts):
            if not session.dry_run:
                copy_file(item, target / relative)


def install_runtime(session: TargetSession, repo_dir: Path, mode: InstallMode) -> list[str]:
    """Install the `craftsman` package and `scripts/*.py` next to each other in the base folder,
    so the hooks run this version; return the script names."""
    if repo_dir.resolve() == session.base_dir.resolve():
        return []
    session.make_dirs(session.base_dir / "scripts")
    runtime_mode = InstallMode.COPY if mode is InstallMode.COPY else InstallMode.SYMLINK
    install_tree(
        session, resolve_craftsman_pkg_dir(repo_dir), session.base_dir / "craftsman", runtime_mode
    )
    scripts = sorted(path for path in (repo_dir / "scripts").glob("*.py") if path.is_file())
    for script in scripts:
        install_tree(
            session, script, session.base_dir / "scripts" / script.name, runtime_mode, executable=True
        )
    return [script.name for script in scripts]


def created_before(session: TargetSession, path: Path) -> bool:
    return session.previous is not None and path_key(path) in session.previous.created_paths


def report_change(tracker: Tracker, change: FileChange) -> None:
    if change.backup is not None:
        tracker.backed_up(change.path, change.backup)
    tracker.add(EventKind.INSTALLED if change.changed else EventKind.UNCHANGED, change.path)


def upsert_markdown(session: TargetSession, target: Path, content: str) -> None:
    """Merge the Craftsman block into a workspace Markdown file (where a symlink inside the
    workspace points, as `AGENTS.md -> CLAUDE.md` setups do)."""
    require_within(target, (session.base_dir,))
    existed = target.is_file()
    session.make_dirs(target.parent)
    change = upsert_managed_markdown(target, content, within=session.base_dir, dry_run=session.dry_run)
    session.modified(target, ModifiedKind.MANAGED_MARKDOWN)
    if not existed or created_before(session, target):
        session.created(target)
    else:
        report_change(session.tracker, change)


# --- taking merged content out of a user file -----------------------------------------------


def _strip_hooks_json(keys: Collection[str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda data: {key: value for key, value in data.items() if key not in keys}


def revert_modified_file(
    path: Path, kind: ModifiedKind, target: TargetManifest, tracker: Tracker
) -> None:
    """Take Craftsman's part out of a user file; a file Craftsman created and that is left
    empty by that is removed. A dry run reports exactly what a real run does."""
    if not path.is_file():
        return
    created = path_key(path) in target.created_paths
    if kind is ModifiedKind.MANAGED_MARKDOWN:
        text = read_markdown(path)
        updated_text = strip_managed_block(text, path)
        if updated_text == text:
            return
        _apply(
            path,
            tracker,
            created=created,
            empty=not updated_text.strip(),
            write=lambda: write_text_atomic(path, updated_text),
        )
        return
    if kind is ModifiedKind.CLAUDE_SETTINGS:
        from craftsman.export import strip_claude_hooks

        strip: Callable[[dict[str, Any]], dict[str, Any]] = strip_claude_hooks
    else:
        strip = _strip_hooks_json(target.hooks_json_keys)
    data = read_json_object(path)
    updated = strip(data)
    if updated == data:
        return
    _apply(
        path, tracker, created=created, empty=not updated, write=lambda: write_json_object(path, updated)
    )


def _apply(
    path: Path, tracker: Tracker, *, created: bool, empty: bool, write: Callable[[], None]
) -> None:
    if created and empty:
        if not tracker.dry_run:
            path.unlink()
        tracker.add(EventKind.REMOVED, path)
        return
    if not created and (backup := copy_once_backup(path, dry_run=tracker.dry_run)) is not None:
        tracker.backed_up(path, backup)
    if not tracker.dry_run:
        write()
    tracker.add(EventKind.RESTORED, f"removed Craftsman's part of {path}")


def require_within(path: Path, roots: Collection[Path]) -> None:
    if not any(is_within(path, root) for root in roots):
        raise InstallError(f"refusing to touch {path}: it is outside {', '.join(map(str, roots))}")


def run_reported(
    dry_run: bool, run: Callable[[Tracker], None], summary: Callable[[Tracker], str]
) -> int:
    """Run an install or uninstall and print what it did; an error ends it with exit code 1."""
    tracker = Tracker(dry_run=dry_run)
    try:
        run(tracker)
    except (InstallError, ExportError, UnsafePathError, OSError) as exc:
        tracker.render(sys.stdout, sys.stderr)  # what was done before the failure
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    tracker.render(sys.stdout, sys.stderr)
    print(f"{'[DRY-RUN] ' if dry_run else ''}{summary(tracker)}")
    return 0
