"""Install manifests: what `craftsman install` created or changed under one base folder.

The manifest is the only record uninstall works from: a path it does not list is never touched.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from craftsman.files import write_text_atomic
from craftsman.records import FieldError, Fields, as_choice

MANIFEST_FILENAME = ".craftsman-install.json"


class InstallError(RuntimeError):
    """Installation or uninstallation cannot proceed safely."""


class TargetKind(str, Enum):
    ANTIGRAVITY = "antigravity"
    CLAUDE = "claude"
    COPILOT = "copilot"
    COPILOT_WORKSPACE = "copilot_workspace"
    AGENTS_WORKSPACE = "agents_workspace"
    GIT_HOOKS = "git_hooks"


class ModifiedKind(str, Enum):
    """A user file Craftsman merged its part into, and so how to take that part out again."""

    CLAUDE_SETTINGS = "claude_settings"
    HOOKS_JSON = "hooks_json"
    MANAGED_MARKDOWN = "managed_markdown"


@dataclass(frozen=True)
class TargetManifest:
    created_paths: tuple[str, ...] = ()
    created_dirs: tuple[str, ...] = ()
    modified_files: Mapping[str, ModifiedKind] = field(default_factory=dict)
    hooks_json_keys: tuple[str, ...] = ()  # the Antigravity hooks.json entries Craftsman added
    previous_hooks_path: str | None = None  # git hooks: the core.hooksPath to restore
    hooks_dir: str | None = None  # git hooks: the core.hooksPath Craftsman set

    def __post_init__(self) -> None:
        object.__setattr__(self, "modified_files", MappingProxyType(dict(self.modified_files)))

    def to_json(self) -> dict[str, Any]:
        return {
            "created_paths": list(self.created_paths),
            "created_dirs": list(self.created_dirs),
            "modified_files": {path: kind.value for path, kind in self.modified_files.items()},
            "hooks_json_keys": list(self.hooks_json_keys),
            "previous_hooks_path": self.previous_hooks_path,
            "hooks_dir": self.hooks_dir,
        }

    @classmethod
    def from_fields(cls, raw: Any) -> TargetManifest:
        fields = Fields(raw)
        target = cls(
            created_paths=fields.texts("created_paths"),
            created_dirs=fields.texts("created_dirs"),
            modified_files=fields.record("modified_files", _modified_kinds),
            hooks_json_keys=fields.texts("hooks_json_keys"),
            previous_hooks_path=fields.optional_text("previous_hooks_path"),
            hooks_dir=fields.optional_text("hooks_dir"),
        )
        return fields.done(target)


def _modified_kinds(raw: Any) -> dict[str, ModifiedKind]:
    fields = Fields(raw)
    return fields.done({path: fields.member(path, ModifiedKind) for path in raw})


@dataclass(frozen=True)
class InstallManifest:
    targets: Mapping[TargetKind, TargetManifest]

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))

    @property
    def created_paths(self) -> tuple[str, ...]:
        return _union(target.created_paths for target in self.targets.values())

    @property
    def created_dirs(self) -> tuple[str, ...]:
        return _union(target.created_dirs for target in self.targets.values())

    def paths_of_others(self, kind: TargetKind) -> frozenset[str]:
        """The paths created by every target but `kind`, which uninstalling `kind` must keep."""
        return frozenset(p for k, t in self.targets.items() if k is not kind for p in t.created_paths)

    def with_target(self, kind: TargetKind, target: TargetManifest) -> InstallManifest:
        return InstallManifest({**self.targets, kind: target})

    def without(self, kinds: Collection[TargetKind]) -> InstallManifest:
        return InstallManifest({k: t for k, t in self.targets.items() if k not in kinds})

    def to_json(self) -> dict[str, Any]:
        return {"targets": {kind.value: self.targets[kind].to_json() for kind in sorted(self.targets)}}

    @classmethod
    def from_fields(cls, raw: Any) -> InstallManifest:
        fields = Fields(raw)
        return fields.done(cls(fields.record("targets", _targets)))


def _targets(raw: Any) -> dict[TargetKind, TargetManifest]:
    fields = Fields(raw)
    if not raw:
        raise FieldError("", "is empty")
    targets: dict[TargetKind, TargetManifest] = {}
    for key in raw:
        kind = as_choice(key, tuple(TargetKind))
        if kind is None:
            raise FieldError(key, "is not a target")
        targets[TargetKind(kind)] = _require_fields_of(
            kind, fields.record(key, TargetManifest.from_fields), key
        )
    return fields.done(targets)


def _require_fields_of(kind: TargetKind, target: TargetManifest, key: str) -> TargetManifest:
    """The git hooks fields belong to the git hooks target, the hooks.json keys to Antigravity."""
    is_hooks = kind is TargetKind.GIT_HOOKS
    if is_hooks and target.hooks_dir is None:
        raise FieldError(key, "needs `hooks_dir`, as the git hooks target")
    if not is_hooks and (target.hooks_dir is not None or target.previous_hooks_path is not None):
        raise FieldError(key, "has `hooks_dir` or `previous_hooks_path`, which only git hooks have")
    if kind is not TargetKind.ANTIGRAVITY and target.hooks_json_keys:
        raise FieldError(key, "has `hooks_json_keys`, which only Antigravity has")
    return target


def load_manifest(base_dir: Path) -> InstallManifest | None:
    """The manifest in `base_dir`, or None when there is none; a corrupt one is an error."""
    path = base_dir / MANIFEST_FILENAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"cannot read install manifest {path}: {exc}") from exc
    try:
        return InstallManifest.from_fields(raw)
    except FieldError as exc:
        raise InstallError(f"invalid install manifest {path}: {exc}") from None


def save_manifest(base_dir: Path, manifest: InstallManifest, *, dry_run: bool) -> None:
    """Write the manifest, or remove its file once no target is left."""
    path = base_dir / MANIFEST_FILENAME
    if dry_run:
        return
    if not manifest.targets:
        path.unlink(missing_ok=True)
        return
    write_text_atomic(path, json.dumps(manifest.to_json(), indent=2, ensure_ascii=False) + "\n")


def _union(groups: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))
