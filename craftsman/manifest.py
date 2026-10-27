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
    def from_json(cls, raw: Any, where: str) -> TargetManifest:
        fields = _exact_object(raw, where, _TARGET_FIELDS)
        modified = _object(fields["modified_files"], f"{where}.modified_files")
        try:
            kinds = {
                _text(path, f"{where}.modified_files"): ModifiedKind(kind)
                for path, kind in modified.items()
            }
        except ValueError as exc:
            raise InstallError(f"{where}.modified_files: {exc}") from None
        return cls(
            created_paths=_texts(fields["created_paths"], f"{where}.created_paths"),
            created_dirs=_texts(fields["created_dirs"], f"{where}.created_dirs"),
            modified_files=kinds,
            hooks_json_keys=_texts(fields["hooks_json_keys"], f"{where}.hooks_json_keys"),
            previous_hooks_path=_optional_text(
                fields["previous_hooks_path"], f"{where}.previous_hooks_path"
            ),
            hooks_dir=_optional_text(fields["hooks_dir"], f"{where}.hooks_dir"),
        )


_TARGET_FIELDS = frozenset(TargetManifest().to_json())


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
    def from_json(cls, raw: Any, source: str) -> InstallManifest:
        targets = _object(
            _exact_object(raw, source, frozenset({"targets"}))["targets"], f"{source}: targets"
        )
        if not targets:
            raise InstallError(f"{source}: `targets` is empty")
        parsed: dict[TargetKind, TargetManifest] = {}
        for key, value in targets.items():
            try:
                kind = TargetKind(key)
            except ValueError:
                raise InstallError(f"{source}: unknown target {key!r}") from None
            where = f"{source}: targets.{key}"
            parsed[kind] = _require_fields_of(kind, TargetManifest.from_json(value, where), where)
        return cls(parsed)


def _require_fields_of(kind: TargetKind, target: TargetManifest, where: str) -> TargetManifest:
    """The git hooks fields belong to the git hooks target, the hooks.json keys to Antigravity."""
    is_hooks = kind is TargetKind.GIT_HOOKS
    if is_hooks and target.hooks_dir is None:
        raise InstallError(f"{where}: the git hooks target needs `hooks_dir`")
    if not is_hooks and (target.hooks_dir is not None or target.previous_hooks_path is not None):
        raise InstallError(
            f"{where}: only the git hooks target has `hooks_dir` and `previous_hooks_path`"
        )
    if kind is not TargetKind.ANTIGRAVITY and target.hooks_json_keys:
        raise InstallError(f"{where}: only the Antigravity target has `hooks_json_keys`")
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
    return InstallManifest.from_json(raw, str(path))


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


def _object(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InstallError(f"{where} must be a JSON object")
    return raw


def _exact_object(raw: Any, where: str, expected: Collection[str]) -> dict[str, Any]:
    fields = _object(raw, where)
    if set(fields) != set(expected):
        raise InstallError(
            f"{where} must have exactly the fields {', '.join(sorted(expected))} "
            f"(got {', '.join(sorted(fields)) or 'none'})"
        )
    return fields


def _text(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise InstallError(f"{where}: expected a non-empty string, got {raw!r}")
    return raw


def _texts(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise InstallError(f"{where} must be a list of strings")
    return tuple(_text(item, where) for item in raw)


def _optional_text(raw: Any, where: str) -> str | None:
    return None if raw is None else _text(raw, where)
