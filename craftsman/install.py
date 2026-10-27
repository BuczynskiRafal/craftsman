#!/usr/bin/env python3
"""Installer and uninstaller for Craftsman across Antigravity, Claude Code, Copilot, and Git hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from craftsman import __version__
from craftsman.export import (
    COPILOT_DISCOVERY,
    COPY_IGNORED_NAMES,
    CRAFTSMAN_BEGIN_MARKER,
    CRAFTSMAN_END_MARKER,
    DUPLICATE_SCOPE_USER_TO_WS_MSG,
    DUPLICATE_SCOPE_WS_TO_USER_MSG,
    HOOKS_FILENAME,
    ExportError,
    absolute_hook_prefixes,
    craftsman_entity_names,
    default_claude_dir,
    default_copilot_home,
    default_copilot_skills_dir,
    default_repo_dir,
    default_vscode_prompts_dir,
    env_var_for_home,
    export_copilot_home,
    export_to_agents_workspace,
    export_to_claude_user,
    export_to_copilot_skills,
    export_to_vscode_user,
    export_to_workspace,
    extract_skill_name,
    has_user_copilot_install,
    has_workspace_copilot_install,
    is_craftsman_generated_file,
    is_craftsman_hook_command,
    iter_discovered_files,
    load_agent_templates,
    merge_claude_hooks,
    remove_claude_hooks,
    remove_managed_markdown,
    resolve_craftsman_pkg_dir,
    resolve_discovery_dirs,
    resolve_windows_appdata,
    user_home,
    vscode_prompt_dirs,
    write_text_atomic,
)

MANIFEST_FILENAME = ".craftsman-install.json"
PASSTHROUGH_TEMPLATE_NAME = "_passthrough.sh"
GIT_TIMEOUT_S = 5

STANDARD_PASSTHROUGH_GIT_HOOKS: tuple[str, ...] = (
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "pre-merge-commit",
    "prepare-commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "post-rewrite",
    "pre-receive",
    "update",
    "proc-receive",
    "post-receive",
    "post-update",
    "push-to-checkout",
)


class InstallError(RuntimeError):
    """Raised when installation or uninstallation cannot proceed safely."""


class InstallMode(str, Enum):
    COPY = "copy"
    SYMLINK = "symlink"
    PLUGIN = "plugin"


class TargetKind(str, Enum):
    ANTIGRAVITY = "antigravity"
    CLAUDE = "claude"
    COPILOT = "copilot"
    COPILOT_WORKSPACE = "copilot_workspace"
    AGENTS_WORKSPACE = "agents_workspace"
    GIT_HOOKS = "git_hooks"


@dataclass(frozen=True)
class InstallConfig:
    repo_dir: Path
    mode: InstallMode = InstallMode.COPY
    auto_detect: bool = True
    install_antigravity: bool = False
    install_claude: bool = False
    disable_claude_hooks: bool = False
    install_copilot: bool = False
    copilot_prompt_files: bool = False
    allow_duplicate_scopes: bool = False
    copilot_workspace: Path | None = None
    agents_workspace: Path | None = None
    install_git_hooks: bool = False
    disable_git_hooks: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class UninstallConfig:
    repo_dir: Path
    uninstall_antigravity: bool = True
    uninstall_claude: bool = True
    uninstall_copilot: bool = True
    copilot_workspace: Path | None = None
    agents_workspace: Path | None = None
    uninstall_git_hooks: bool = True
    dry_run: bool = False


def _parse_manifest_str_list(raw: dict[str, Any], key: str, source: str) -> tuple[str, ...]:
    if key not in raw:
        return ()
    val = raw[key]
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise InstallError(f"Invalid install manifest {source}: '{key}' must be a list of strings")
    return tuple(val)


@dataclass(frozen=True)
class TargetManifest:
    target: str
    mode: str
    version: str
    created_paths: tuple[str, ...] = ()
    created_dirs: tuple[str, ...] = ()
    modified_files: Mapping[str, str] = field(default_factory=dict)
    previous_hooks_path: str | None = None
    hooks_dir: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "modified_files", MappingProxyType(dict(self.modified_files)))

    def __hash__(self) -> int:
        return hash(
            (
                self.target,
                self.mode,
                self.version,
                self.created_paths,
                self.created_dirs,
                tuple(sorted(self.modified_files.items())),
                self.previous_hooks_path,
                self.hooks_dir,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "target": self.target,
            "mode": self.mode,
            "version": self.version,
            "created_paths": list(self.created_paths),
            "created_dirs": list(self.created_dirs),
            "modified_files": dict(self.modified_files),
        }
        if self.previous_hooks_path is not None or self.target == TargetKind.GIT_HOOKS.value:
            data["previous_hooks_path"] = self.previous_hooks_path
        if self.hooks_dir is not None:
            data["hooks_dir"] = self.hooks_dir
        return data

    @classmethod
    def from_dict(
        cls, target_key: str, raw: dict[str, Any], source: str = "<manifest>"
    ) -> TargetManifest:
        created_paths = _parse_manifest_str_list(raw, "created_paths", source)
        created_dirs = _parse_manifest_str_list(raw, "created_dirs", source)
        raw_mod = raw.get("modified_files", {})
        if not isinstance(raw_mod, dict):
            raise InstallError(f"Invalid install manifest {source}: 'modified_files' must be an object")
        modified_files = {
            str(k): str(v) for k, v in raw_mod.items() if isinstance(k, str) and isinstance(v, str)
        }
        prev_hooks = raw.get("previous_hooks_path")
        hooks_dir = raw.get("hooks_dir")
        return cls(
            target=str(raw.get("target") or target_key),
            mode=str(raw.get("mode") or InstallMode.COPY.value),
            version=str(raw.get("version") or __version__),
            created_paths=created_paths,
            created_dirs=created_dirs,
            modified_files=modified_files,
            previous_hooks_path=prev_hooks if isinstance(prev_hooks, str) and prev_hooks else None,
            hooks_dir=hooks_dir if isinstance(hooks_dir, str) and hooks_dir else None,
        )


@dataclass(frozen=True)
class BaseManifest:
    version: str
    mode: str
    created_paths: tuple[str, ...]
    created_dirs: tuple[str, ...]
    previous_hooks_path: str | None
    hooks_dir: str | None
    targets: Mapping[str, TargetManifest]

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))

    def __hash__(self) -> int:
        return hash(
            (
                self.version,
                self.mode,
                self.created_paths,
                self.created_dirs,
                self.previous_hooks_path,
                self.hooks_dir,
                tuple(sorted(self.targets.items())),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "mode": self.mode,
            "created_paths": list(self.created_paths),
            "created_dirs": list(self.created_dirs),
            "previous_hooks_path": self.previous_hooks_path,
            "hooks_dir": self.hooks_dir,
            "targets": {k: v.to_dict() for k, v in sorted(self.targets.items())},
        }

    @classmethod
    def from_targets(
        cls,
        targets: Mapping[str, TargetManifest],
        *,
        default_mode: str = InstallMode.COPY.value,
    ) -> BaseManifest:
        all_paths: list[str] = []
        all_dirs: list[str] = []
        prev_hooks: str | None = None
        hooks_dir: str | None = None
        primary_modes = [
            t_man.mode for t_key, t_man in sorted(targets.items()) if t_key != TargetKind.GIT_HOOKS.value
        ]
        if TargetKind.ANTIGRAVITY.value in targets:
            mode = targets[TargetKind.ANTIGRAVITY.value].mode
        elif primary_modes:
            mode = primary_modes[0]
        elif TargetKind.GIT_HOOKS.value in targets:
            mode = targets[TargetKind.GIT_HOOKS.value].mode
        else:
            mode = default_mode

        for _, t_man in sorted(targets.items()):
            for p in t_man.created_paths:
                if p not in all_paths:
                    all_paths.append(p)
            for d in t_man.created_dirs:
                if d not in all_dirs:
                    all_dirs.append(d)
            if t_man.previous_hooks_path is not None:
                prev_hooks = t_man.previous_hooks_path
            if t_man.hooks_dir is not None:
                hooks_dir = t_man.hooks_dir
        return cls(
            version=__version__,
            mode=mode,
            created_paths=tuple(all_paths),
            created_dirs=tuple(all_dirs),
            previous_hooks_path=prev_hooks,
            hooks_dir=hooks_dir,
            targets=dict(targets),
        )


@dataclass
class InstallTracker:
    dry_run: bool
    installed: list[Path] = field(default_factory=list)
    backed_up: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record_installed(self, path: Path) -> None:
        if path not in self.installed:
            self.installed.append(path)

    def record_backup(self, original: Path, backup: Path) -> None:
        self.backed_up.append((original, backup))

    def record_skipped(self, path: Path) -> None:
        if path not in self.skipped:
            self.skipped.append(path)


@dataclass
class UninstallTracker:
    dry_run: bool
    removed: list[Path] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record_removed(self, path: Path) -> None:
        if path not in self.removed:
            self.removed.append(path)


@dataclass
class _TargetInstallSession:
    base_dir: Path
    target_kind: TargetKind
    existing_manifest: BaseManifest | None
    prev_target: TargetManifest | None
    owned_paths: set[str]
    other_owned: set[str]
    created_paths: list[str]
    created_dirs: list[str]
    modified_files: dict[str, str]
    stop_roots: set[Path]

    @classmethod
    def begin(
        cls,
        base_dir: Path,
        target_kind: TargetKind,
        home: Path,
        *extra_roots: Path,
        preserve_modified: bool = False,
    ) -> _TargetInstallSession:
        existing_manifest = load_manifest(base_dir)
        prev_target = existing_manifest.targets.get(target_kind.value) if existing_manifest else None
        owned_paths = set(existing_manifest.created_paths) if existing_manifest else set()
        other_owned: set[str] = set()
        if existing_manifest:
            for k, t_man in existing_manifest.targets.items():
                if k != target_kind.value:
                    other_owned.update(t_man.created_paths)
        return cls(
            base_dir=base_dir,
            target_kind=target_kind,
            existing_manifest=existing_manifest,
            prev_target=prev_target,
            owned_paths=owned_paths,
            other_owned=other_owned,
            created_paths=[],
            created_dirs=list(prev_target.created_dirs) if prev_target else [],
            modified_files=(
                dict(prev_target.modified_files) if preserve_modified and prev_target else {}
            ),
            stop_roots=_stop_roots_for(home, *extra_roots),
        )

    def stage_path(self, target: Path, tracker: InstallTracker) -> str:
        _ensure_safe_target(target, self.owned_paths, tracker, stop_roots=self.stop_roots)
        return self.record_created_path(target, tracker)

    def record_created_path(self, target: Path, tracker: InstallTracker) -> str:
        abs_target = os.path.abspath(str(target))
        if abs_target not in self.created_paths:
            self.created_paths.append(abs_target)
        tracker.record_installed(target)
        return abs_target

    def save(
        self,
        mode: InstallMode,
        *,
        dry_run: bool,
        cleanup_obsolete: bool = False,
        previous_hooks_path: str | None = None,
        hooks_dir: str | None = None,
    ) -> None:
        if cleanup_obsolete:
            _cleanup_obsolete_target_paths(
                self.prev_target,
                self.created_paths,
                self.other_owned,
                stop_roots=self.stop_roots,
                dry_run=dry_run,
            )
        targets = dict(self.existing_manifest.targets) if self.existing_manifest else {}
        targets[self.target_kind.value] = TargetManifest(
            target=self.target_kind.value,
            mode=mode.value,
            version=__version__,
            created_paths=tuple(self.created_paths),
            created_dirs=tuple(self.created_dirs),
            modified_files=self.modified_files,
            previous_hooks_path=previous_hooks_path,
            hooks_dir=hooks_dir,
        )
        default_mode = (
            self.existing_manifest.mode
            if (
                self.target_kind == TargetKind.GIT_HOOKS
                and self.existing_manifest
                and TargetKind.ANTIGRAVITY.value in targets
            )
            else mode.value
        )
        updated_manifest = BaseManifest.from_targets(targets, default_mode=default_mode)
        save_manifest(self.base_dir, updated_manifest, dry_run=dry_run)


def load_manifest(base_dir: Path) -> BaseManifest | None:
    manifest_path = base_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError(f"Failed to read install manifest {manifest_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise InstallError(f"Invalid install manifest {manifest_path}: expected JSON object")

    source_str = str(manifest_path)
    raw_targets = raw.get("targets")
    targets: dict[str, TargetManifest] = {}
    if raw_targets is not None:
        if not isinstance(raw_targets, dict):
            raise InstallError(f"Invalid install manifest {manifest_path}: 'targets' must be an object")
        for k, v in raw_targets.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                raise InstallError(
                    f"Invalid install manifest {manifest_path}: target entry {k!r} must be an object"
                )
            targets[k] = TargetManifest.from_dict(k, v, source=source_str)
    else:
        targets["default"] = TargetManifest.from_dict("default", raw, source=source_str)

    created_paths = _parse_manifest_str_list(raw, "created_paths", source_str)
    created_dirs = _parse_manifest_str_list(raw, "created_dirs", source_str)
    prev_hooks = raw.get("previous_hooks_path")
    hooks_dir = raw.get("hooks_dir")
    return BaseManifest(
        version=str(raw.get("version") or __version__),
        mode=str(raw.get("mode") or InstallMode.COPY.value),
        created_paths=created_paths,
        created_dirs=created_dirs,
        previous_hooks_path=prev_hooks if isinstance(prev_hooks, str) and prev_hooks else None,
        hooks_dir=hooks_dir if isinstance(hooks_dir, str) and hooks_dir else None,
        targets=targets,
    )


def save_manifest(base_dir: Path, manifest: BaseManifest, *, dry_run: bool) -> Path:
    manifest_path = base_dir / MANIFEST_FILENAME
    if not dry_run:
        write_text_atomic(
            manifest_path,
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
        )
    return manifest_path


def expand_tilde(raw: str, home: Path | None = None) -> str:
    resolved_home = str(user_home(home))
    if raw == "~":
        return resolved_home
    if raw.startswith("~/") or raw.startswith("~\\"):
        rest = raw[2:].lstrip("/\\")
        return os.path.join(resolved_home, rest) if rest else resolved_home
    return raw


def _resolve_safe(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(str(path)))


def git_config_get(key: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
        if proc.returncode == 0:
            val = proc.stdout.strip()
            return val if val else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _git_config_set(key: str, value: str) -> None:
    try:
        proc = subprocess.run(
            ["git", "config", "--global", key, value],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"Failed to set git config --global {key}: {exc}") from exc
    if proc.returncode != 0:
        raise InstallError(
            f"git config --global {key} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )


def _git_config_unset(key: str) -> None:
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--unset-all", key],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"Failed to unset git config --global {key}: {exc}") from exc
    if proc.returncode not in (0, 5):
        raise InstallError(
            f"git config --global --unset-all {key} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    section = key.partition(".")[0]
    if section:
        try:
            rem = subprocess.run(
                ["git", "config", "--global", "--get-regexp", rf"^{section}\."],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
            if rem.returncode != 0 or not rem.stdout.strip():
                subprocess.run(
                    ["git", "config", "--global", "--remove-section", section],
                    capture_output=True,
                    text=True,
                    timeout=GIT_TIMEOUT_S,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError):
            pass


def detect_antigravity(home: Path | None = None) -> bool:
    h = user_home(home)
    return (
        (h / ".gemini" / "antigravity").is_dir()
        or (h / ".gemini" / "config").is_dir()
        or bool(os.environ.get("AGY_MANAGED_ENV"))
        or bool(os.environ.get("ANTIGRAVITY_AGENT"))
    )


def detect_claude(home: Path | None = None) -> bool:
    h = user_home(home)
    return (h / ".claude").is_dir() or bool(os.environ.get("CLAUDECODE"))


def detect_vscode_copilot(home: Path | None = None) -> bool:
    h = user_home(home)
    if (
        (h / ".copilot").is_dir()
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or bool(os.environ.get("VSCODE_PID"))
        or bool(os.environ.get("VSCODE_IPC_HOOK_CLI"))
    ):
        return True
    xdg_cfg = env_var_for_home("XDG_CONFIG_HOME", home)
    xdg_root = Path(xdg_cfg) if xdg_cfg else (h / ".config")
    if (
        (h / "AppData" / "Roaming" / "Code" / "User").is_dir()
        or (h / "Library" / "Application Support" / "Code" / "User").is_dir()
        or (xdg_root / "Code" / "User").is_dir()
    ):
        return True
    appdata = env_var_for_home("APPDATA", home)
    if appdata:
        if (Path(appdata) / "Code" / "User").is_dir():
            return True
        win_base = resolve_windows_appdata(home)
        if win_base is not None and (win_base / "Code" / "User").is_dir():
            return True
    return shutil.which("code") is not None


def resolve_git_hooks_base_dir(
    home: Path | None = None, *, installing_antigravity: bool = False
) -> Path:
    h = user_home(home)
    config_dir = h / ".gemini" / "config"
    if installing_antigravity or config_dir.is_dir() or (h / ".gemini" / "antigravity").is_dir():
        return config_dir
    craftsman_home = env_var_for_home("CRAFTSMAN_HOME", home)
    return Path(craftsman_home) if craftsman_home else (h / ".craftsman")


def _record_missing_dirs(directory: Path, created_dirs: list[str], stop_roots: set[Path]) -> None:
    chain: list[Path] = []
    cur = Path(os.path.abspath(str(directory)))
    while cur not in stop_roots and cur.parent != cur:
        if cur.exists() or cur.is_symlink():
            break
        chain.append(cur)
        cur = cur.parent
    for p in reversed(chain):
        p_str = str(p)
        if p_str not in created_dirs:
            created_dirs.append(p_str)


def _stop_roots_for(home: Path, *extra: Path) -> set[Path]:
    abs_home = Path(os.path.abspath(str(home)))
    roots: set[Path] = {abs_home}
    for env_key in ("XDG_CONFIG_HOME", "APPDATA", "CRAFTSMAN_HOME"):
        val = env_var_for_home(env_key, home)
        if val:
            env_path = Path(os.path.abspath(val))
            if abs_home not in env_path.parents and env_path != abs_home:
                roots.add(env_path.parent)
    for item in extra:
        roots.add(Path(os.path.abspath(str(item))))
    return roots


def _allowed_roots_for_target(base_dir: Path, target_key: str, home: Path) -> tuple[Path, ...]:
    roots: list[Path] = [base_dir]
    if target_key in (TargetKind.COPILOT.value, "default"):
        roots.append(default_copilot_skills_dir(home))
        vsc_prompts = default_vscode_prompts_dir(home)
        roots.append(vsc_prompts.parent)
        roots.append(vsc_prompts)
    return tuple(roots)


def _is_path_within_roots(path: Path, allowed_roots: Sequence[Path]) -> bool:
    abs_path = Path(os.path.abspath(str(path)))
    for root in allowed_roots:
        abs_root = Path(os.path.abspath(str(root)))
        if abs_root not in abs_path.parents:
            continue
        cur = abs_path.parent
        has_sym = False
        while cur != abs_root and cur.parent != cur:
            if cur.is_symlink():
                has_sym = True
                break
            cur = cur.parent
        if has_sym:
            continue
        resolved_root = _resolve_safe(abs_root)
        resolved_parent = _resolve_safe(abs_path.parent)
        if resolved_parent == resolved_root or resolved_root in resolved_parent.parents:
            return True
    return False


def _is_owned(path: Path, owned_paths: set[str]) -> bool:
    abs_path = Path(os.path.abspath(str(path)))
    if str(abs_path) in owned_paths or str(path) in owned_paths:
        return True
    return any(str(parent) in owned_paths for parent in abs_path.parents)


def _timestamped_backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        counter += 1
        candidate = path.with_name(f"{path.name}.bak.{stamp}.{counter}")
    return candidate


def _ensure_safe_target(
    target: Path,
    owned_paths: set[str],
    tracker: InstallTracker,
    *,
    stop_roots: Collection[Path] = (),
) -> None:
    """Back up `target` if it exists and is not owned by a previous Craftsman install; remove if owned."""
    if _has_symlinked_parent(target, stop_roots):
        raise InstallError(f"Refusing to write {target}: an intermediate parent directory is a symlink.")
    if not (target.exists() or target.is_symlink()):
        return
    if _is_owned(target, owned_paths):
        if not _remove_path_if_exists(target, dry_run=tracker.dry_run, stop_roots=stop_roots):
            raise InstallError(f"Refusing to overwrite {target}: could not safely remove existing path.")
        return
    backup = _timestamped_backup_path(target)
    if not tracker.dry_run:
        os.replace(target, backup)
    tracker.record_backup(target, backup)
    print(f"  ! backed up existing {target} -> {backup}")


def _has_symlinked_parent(path: Path, stop_roots: Collection[Path] = ()) -> bool:
    effective_stops: set[Path] = {Path(os.path.abspath(str(r))) for r in stop_roots} | {
        _resolve_safe(r) for r in stop_roots
    }
    ambient_home = user_home(None)
    effective_stops.add(Path(os.path.abspath(str(ambient_home))))
    effective_stops.add(_resolve_safe(ambient_home))

    cur = path.parent
    while cur != cur.parent:
        abs_cur = Path(os.path.abspath(str(cur)))
        if abs_cur in effective_stops or _resolve_safe(cur) in effective_stops:
            break
        if cur.is_symlink():
            return True
        cur = cur.parent
    return False


def _remove_path_if_exists(path: Path, *, dry_run: bool, stop_roots: Collection[Path] = ()) -> bool:
    """Remove `path` without traversing through a symlinked intermediate directory below `stop_roots`."""
    if _has_symlinked_parent(path, stop_roots):
        return False
    if not (path.exists() or path.is_symlink()):
        return False
    if not dry_run:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    return True


def _copy_file_atomic(src: Path, dst: Path, *, executable: bool = False, dry_run: bool = False) -> None:
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    mode = 0o755 if executable else (src.stat().st_mode & 0o777 or 0o644)
    fd, tmp_str = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_str, mode)
        os.replace(tmp_str, dst)
    except BaseException:
        Path(tmp_str).unlink(missing_ok=True)
        raise


def _copy_directory_clean(
    src_dir: Path,
    dst_dir: Path,
    session: _TargetInstallSession,
    *,
    dry_run: bool,
) -> None:
    _record_missing_dirs(dst_dir, session.created_dirs, session.stop_roots)
    if not dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)
    abs_dst = os.path.abspath(str(dst_dir))
    if abs_dst not in session.created_paths:
        session.created_paths.append(abs_dst)
    for item in sorted(src_dir.rglob("*")):
        rel = item.relative_to(src_dir)
        if any(part in COPY_IGNORED_NAMES or part == "_assets" for part in rel.parts):
            continue
        target_item = dst_dir / rel
        if item.is_dir():
            if not dry_run:
                target_item.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            _copy_file_atomic(item, target_item, dry_run=dry_run)


def _install_scripts_to(
    repo_dir: Path,
    base_dir: Path,
    mode: InstallMode,
    session: _TargetInstallSession,
    tracker: InstallTracker,
) -> list[str]:
    """Install `craftsman/` package and `scripts/*.py` into `base_dir` without mutating `repo_dir`."""
    _record_missing_dirs(base_dir, session.created_dirs, session.stop_roots)
    if not tracker.dry_run:
        base_dir.mkdir(parents=True, exist_ok=True)

    if _resolve_safe(repo_dir) == _resolve_safe(base_dir):
        tracker.record_skipped(base_dir / "scripts")
        return []

    pkg_src_dir = resolve_craftsman_pkg_dir(repo_dir)
    target_scripts_dir = base_dir / "scripts"
    target_pkg_dir = base_dir / "craftsman"
    _record_missing_dirs(target_scripts_dir, session.created_dirs, session.stop_roots)
    if not tracker.dry_run:
        target_scripts_dir.mkdir(parents=True, exist_ok=True)

    _ensure_safe_target(target_pkg_dir, session.owned_paths, tracker, stop_roots=session.stop_roots)
    if mode in (InstallMode.SYMLINK, InstallMode.PLUGIN):
        if not tracker.dry_run:
            target_pkg_dir.symlink_to(pkg_src_dir, target_is_directory=True)
    else:
        _copy_directory_clean(
            pkg_src_dir,
            target_pkg_dir,
            session,
            dry_run=tracker.dry_run,
        )
    session.record_created_path(target_pkg_dir, tracker)

    script_names: list[str] = []
    for script_path in sorted((repo_dir / "scripts").glob("*.py")):
        if not script_path.is_file():
            continue
        script_name = script_path.name
        target = target_scripts_dir / script_name
        session.stage_path(target, tracker)
        if mode in (InstallMode.SYMLINK, InstallMode.PLUGIN):
            if not tracker.dry_run:
                target.symlink_to(script_path)
        else:
            _copy_file_atomic(script_path, target, executable=True, dry_run=tracker.dry_run)
        script_names.append(script_name)
    return script_names


def _validate_existing_hooks_json(target_hooks: Path, tracker: InstallTracker) -> None:
    """Validate that an existing regular `hooks.json` is valid UTF-8 JSON object; back up and raise if corrupt."""
    if target_hooks.is_symlink() or not target_hooks.is_file():
        return
    bak = target_hooks.with_name(target_hooks.name + ".bak")
    try:
        existing_text = target_hooks.read_text(encoding="utf-8")
        dst_data = json.loads(existing_text)
        if not isinstance(dst_data, dict):
            raise ValueError("top-level JSON value must be an object")
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        if not tracker.dry_run:
            shutil.copy2(target_hooks, bak)
        tracker.record_backup(target_hooks, bak)
        raise InstallError(
            f"Failed to parse existing {target_hooks} ({exc}); backed up to {bak} and aborted merge."
        ) from exc


def _merge_antigravity_hooks_json(
    repo_dir: Path,
    config_dir: Path,
    session: _TargetInstallSession,
    tracker: InstallTracker,
) -> None:
    src_hooks = repo_dir / "hooks.json"
    if not src_hooks.is_file():
        return
    target_hooks = config_dir / "hooks.json"
    abs_target = os.path.abspath(str(target_hooks))
    is_owned = _is_owned(target_hooks, session.owned_paths)

    if target_hooks.is_symlink():
        if not is_owned:
            backup = _timestamped_backup_path(target_hooks)
            if not tracker.dry_run:
                os.replace(target_hooks, backup)
            tracker.record_backup(target_hooks, backup)
            print(f"  ! backed up existing symlink {target_hooks} -> {backup}")
        elif not tracker.dry_run:
            target_hooks.unlink()

    src_data = json.loads(src_hooks.read_text(encoding="utf-8"))
    if not isinstance(src_data, dict):
        raise InstallError(f"Invalid source {src_hooks}: top-level JSON value must be an object")

    if target_hooks.is_file():
        _validate_existing_hooks_json(target_hooks, tracker)
        existing_text = target_hooks.read_text(encoding="utf-8")
        dst_data = json.loads(existing_text)
        bak = target_hooks.with_name(target_hooks.name + ".bak")

        merged_data = dict(dst_data)
        for key, val in src_data.items():
            existing_entry = merged_data.get(key)
            if (
                isinstance(existing_entry, dict)
                and isinstance(val, dict)
                and isinstance(existing_entry.get("enabled"), bool)
            ):
                merged_data[key] = {**val, "enabled": existing_entry["enabled"]}
            else:
                merged_data[key] = val

        rendered = json.dumps(merged_data, indent=2, ensure_ascii=False) + "\n"
        if (not is_owned or rendered != existing_text) and not bak.exists():
            if not tracker.dry_run:
                shutil.copy2(target_hooks, bak)
            tracker.record_backup(target_hooks, bak)
            print(f"  ! backed up {target_hooks} -> {bak}")
        if rendered != existing_text:
            if not tracker.dry_run:
                write_text_atomic(target_hooks, rendered)
            tracker.record_installed(target_hooks)
        else:
            tracker.record_skipped(target_hooks)

        if is_owned and abs_target not in session.created_paths:
            session.created_paths.append(abs_target)
        session.modified_files[abs_target] = "hooks_json"
    else:
        rendered = json.dumps(src_data, indent=2, ensure_ascii=False) + "\n"
        if not tracker.dry_run:
            write_text_atomic(target_hooks, rendered)
        session.record_created_path(target_hooks, tracker)
        session.modified_files[abs_target] = "hooks_json"

    print(f"  + hooks: {target_hooks}")


def _unmerge_antigravity_hooks_json(repo_dir: Path, target_hooks: Path, *, dry_run: bool) -> bool:
    if not target_hooks.is_file():
        return False
    src_hooks = repo_dir / "hooks.json"
    keys_to_remove: set[str] = {"craftsman-git-guard", "craftsman-stop-gate"}
    if src_hooks.is_file():
        try:
            src_data = json.loads(src_hooks.read_text(encoding="utf-8"))
            if isinstance(src_data, dict):
                keys_to_remove.update(src_data.keys())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        dst_data = json.loads(target_hooks.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(dst_data, dict):
        return False
    updated = {k: v for k, v in dst_data.items() if k not in keys_to_remove}
    if updated == dst_data:
        return False
    if not dry_run:
        write_text_atomic(target_hooks, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    return True


def _has_claude_craftsman_hooks(settings_path: Path) -> bool:
    if not settings_path.is_file():
        return False
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    hooks = loaded.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(entries, list) and any(
                isinstance(e, dict) and is_craftsman_hook_command(e.get("command")) for e in entries
            ):
                return True
    return False


def _has_managed_markdown_block(target_file: Path) -> bool:
    if not target_file.is_file():
        return False
    try:
        existing = target_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    begin_idx = existing.find(CRAFTSMAN_BEGIN_MARKER)
    end_idx = existing.find(CRAFTSMAN_END_MARKER)
    return begin_idx != -1 and end_idx != -1 and begin_idx < end_idx


def _cleanup_obsolete_target_paths(
    prev_target: TargetManifest | None,
    new_created_paths: Sequence[str],
    other_owned_paths: set[str],
    *,
    stop_roots: Collection[Path] = (),
    dry_run: bool,
) -> None:
    """Remove paths owned by the previous run of the same target that are no longer part of its install."""
    if prev_target is None:
        return
    new_set = set(new_created_paths)
    for old_p in sorted(prev_target.created_paths, key=lambda s: len(Path(s).parts), reverse=True):
        if old_p in new_set or old_p in other_owned_paths:
            continue
        if any(ncp.startswith(old_p + os.sep) or old_p.startswith(ncp + os.sep) for ncp in new_set):
            continue
        old_path = Path(old_p)
        if not old_path.is_symlink() and old_path.is_file():
            if old_path.name.endswith((".agent.md", ".prompt.md", ".instructions.md")):
                if not is_craftsman_generated_file(old_path):
                    continue
        elif not old_path.is_symlink() and old_path.is_dir():
            skill_md = old_path / "SKILL.md"
            if skill_md.is_file() and not is_craftsman_generated_file(skill_md):
                continue
        _remove_path_if_exists(old_path, dry_run=dry_run, stop_roots=stop_roots)


def install_antigravity_target(
    repo_dir: Path,
    home: Path,
    mode: InstallMode,
    tracker: InstallTracker,
) -> None:
    config_dir = home / ".gemini" / "config"
    skills_dir = config_dir / "skills"
    rules_dir = config_dir / "rules"
    scripts_dir = config_dir / "scripts"
    plugins_dir = config_dir / "plugins"

    session = _TargetInstallSession.begin(config_dir, TargetKind.ANTIGRAVITY, home)

    if mode != InstallMode.PLUGIN:
        _validate_existing_hooks_json(config_dir / "hooks.json", tracker)

    saved = False
    try:
        for d in (skills_dir, rules_dir, scripts_dir, plugins_dir):
            _record_missing_dirs(d, session.created_dirs, session.stop_roots)
            if not tracker.dry_run:
                d.mkdir(parents=True, exist_ok=True)

        if mode == InstallMode.PLUGIN:
            target_hooks = config_dir / "hooks.json"
            abs_hooks = os.path.abspath(str(target_hooks))
            if target_hooks.is_file() and (
                _is_owned(target_hooks, session.owned_paths)
                or (session.prev_target is not None and abs_hooks in session.prev_target.modified_files)
            ):
                _unmerge_antigravity_hooks_json(repo_dir, target_hooks, dry_run=tracker.dry_run)
                if (
                    not tracker.dry_run
                    and _is_owned(target_hooks, session.owned_paths)
                    and target_hooks.is_file()
                ):
                    try:
                        if json.loads(target_hooks.read_text(encoding="utf-8")) == {}:
                            target_hooks.unlink()
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        pass
                elif target_hooks.is_file():
                    session.modified_files[abs_hooks] = "hooks_json"

            _install_scripts_to(repo_dir, config_dir, mode, session, tracker)
            target_plugin = plugins_dir / "craftsman"
            session.stage_path(target_plugin, tracker)
            if not tracker.dry_run:
                target_plugin.symlink_to(repo_dir, target_is_directory=True)
            print(f"[OK] Installed craftsman as an Antigravity plugin -> {target_plugin}")
        else:
            print(f"Installing Craftsman rules into {rules_dir} ({mode.value} mode)...")
            rules_installed = 0
            for rule_path in sorted((repo_dir / "rules").glob("*.md")):
                if not rule_path.is_file():
                    continue
                target = rules_dir / rule_path.name
                session.stage_path(target, tracker)
                if mode == InstallMode.SYMLINK:
                    if not tracker.dry_run:
                        target.symlink_to(rule_path)
                else:
                    _copy_file_atomic(rule_path, target, dry_run=tracker.dry_run)
                rules_installed += 1
                print(f"  + rule: {rule_path.name}")

            print(f"Installing Craftsman skills into {skills_dir} ({mode.value} mode)...")
            skills_installed = 0
            for skill_dir in sorted((repo_dir / "skills").iterdir()):
                if not skill_dir.is_dir():
                    continue
                target = skills_dir / skill_dir.name
                _ensure_safe_target(target, session.owned_paths, tracker, stop_roots=session.stop_roots)
                if mode == InstallMode.SYMLINK:
                    if not tracker.dry_run:
                        target.symlink_to(skill_dir, target_is_directory=True)
                else:
                    _copy_directory_clean(skill_dir, target, session, dry_run=tracker.dry_run)
                session.record_created_path(target, tracker)
                skills_installed += 1
                print(f"  + skill: /{skill_dir.name}")

            print(f"Installing Craftsman Hard-Loop scripts into {scripts_dir} ({mode.value} mode)...")
            script_names = _install_scripts_to(repo_dir, config_dir, mode, session, tracker)
            for s_name in script_names:
                print(f"  + script: {s_name}")

            _merge_antigravity_hooks_json(repo_dir, config_dir, session, tracker)

            print("")
            print("[OK] Craftsman Antigravity suite installed successfully!")
            print(f"     Skills installed:  {skills_installed}")
            print(f"     Rules installed:   {rules_installed}")
            print(f"     Scripts installed: {len(script_names)}")

        session.save(mode, dry_run=tracker.dry_run, cleanup_obsolete=True)
        saved = True
    finally:
        if not saved and (session.created_paths or session.modified_files):
            session.save(mode, dry_run=tracker.dry_run, cleanup_obsolete=False)


def install_claude_target(
    repo_dir: Path,
    home: Path,
    *,
    with_hooks: bool,
    tracker: InstallTracker,
) -> None:
    claude_dir = default_claude_dir(home)
    session = _TargetInstallSession.begin(claude_dir, TargetKind.CLAUDE, home, preserve_modified=True)
    settings_path = claude_dir / "settings.json"
    abs_settings = os.path.abspath(str(settings_path))

    saved = False
    try:
        print("Exporting Craftsman suite to Claude Code (~/.claude)...")
        commands_dir = claude_dir / "commands"
        rules_dir = claude_dir / "rules"
        agents_dir = claude_dir / "agents"
        for d in (commands_dir, rules_dir, agents_dir):
            _record_missing_dirs(d, session.created_dirs, session.stop_roots)

        for skill_path in sorted((repo_dir / "skills").glob("*/SKILL.md")):
            raw_md = skill_path.read_text(encoding="utf-8")
            skill_name = extract_skill_name(raw_md, skill_path.parent.name)
            session.stage_path(commands_dir / f"{skill_name}.md", tracker)

        rules_file = rules_dir / "craftsman-rules.md"
        session.stage_path(rules_file, tracker)

        templates = [t for t in load_agent_templates(repo_dir) if t.role != "orchestrator"]
        for tmpl in templates:
            session.stage_path(agents_dir / f"{tmpl.name}.md", tracker)

        if not tracker.dry_run:
            count, written_rules = export_to_claude_user(repo_dir, claude_dir)
        else:
            count = len(list((repo_dir / "skills").glob("*/SKILL.md")))
            written_rules = rules_file
        print(f"[OK] Exported {count} Claude Code slash commands -> {commands_dir}")
        print(f"[OK] Exported Claude Code reviewer and verifier subagents -> {agents_dir}")
        print(f"[OK] Exported Claude Code global rules -> {written_rules}")

        if with_hooks:
            existed_unowned = settings_path.exists() and not _is_owned(
                settings_path, session.owned_paths
            )
            bak_path = settings_path.with_name(settings_path.name + ".bak")
            bak_existed = bak_path.exists()
            bash_prefix, _ = absolute_hook_prefixes()
            if not tracker.dry_run:
                changed = merge_claude_hooks(settings_path, bash_prefix)
                if existed_unowned and not bak_existed and bak_path.exists():
                    tracker.record_backup(settings_path, bak_path)
                if changed:
                    tracker.record_installed(settings_path)
                else:
                    tracker.record_skipped(settings_path)
            else:
                changed = True
                if existed_unowned and not bak_existed:
                    tracker.record_backup(settings_path, bak_path)
                tracker.record_installed(settings_path)
            if not existed_unowned and abs_settings not in session.created_paths:
                session.created_paths.append(abs_settings)
            session.modified_files[abs_settings] = "claude_settings"
            print(
                f"[OK] Claude Code hooks {'merged into' if changed else 'already up to date in'} {settings_path}"
            )
        elif (
            session.prev_target is not None
            and abs_settings in session.prev_target.created_paths
            and abs_settings not in session.created_paths
        ):
            session.created_paths.append(abs_settings)

        session.save(InstallMode.COPY, dry_run=tracker.dry_run, cleanup_obsolete=True)
        saved = True
    finally:
        if not saved and (session.created_paths or session.modified_files):
            session.save(InstallMode.COPY, dry_run=tracker.dry_run, cleanup_obsolete=False)


def _remove_generated_copilot_matches(
    paths: Iterable[Path],
    valid_names: frozenset[str],
    *,
    remove_parent: bool = False,
    stop_roots: Collection[Path],
    dry_run: bool,
    label: str = "legacy duplicate",
) -> list[Path]:
    removed: list[Path] = []
    for path in paths:
        if is_craftsman_generated_file(path, valid_names):
            target = path.parent if remove_parent else path
            if _remove_path_if_exists(target, dry_run=dry_run, stop_roots=stop_roots):
                removed.append(target)
                print(f"  - removed {label}: {target}")
    return removed


def _cleanup_legacy_copilot_user_files(
    repo_dir: Path,
    home: Path,
    *,
    prompt_files: bool,
    stop_roots: Collection[Path],
    dry_run: bool,
) -> list[Path]:
    """Remove Craftsman-generated Copilot files from non-canonical user/profile locations."""
    valid_names = craftsman_entity_names(repo_dir)
    removed: list[Path] = []
    for prompts_dir in vscode_prompt_dirs(default_vscode_prompts_dir(home)):
        if not prompts_dir.is_dir():
            continue
        patterns = ("*.agent.md",) if prompt_files else ("*.agent.md", "*.prompt.md")
        for pat in patterns:
            removed.extend(
                _remove_generated_copilot_matches(
                    sorted(prompts_dir.glob(pat)), valid_names, stop_roots=stop_roots, dry_run=dry_run
                )
            )
    if prompt_files:
        skills_dir = default_copilot_skills_dir(home)
        if skills_dir.is_dir():
            removed.extend(
                _remove_generated_copilot_matches(
                    sorted(skills_dir.glob("*/SKILL.md")),
                    valid_names,
                    remove_parent=True,
                    stop_roots=stop_roots,
                    dry_run=dry_run,
                )
            )
    return removed


def install_copilot_target(
    repo_dir: Path,
    home: Path,
    tracker: InstallTracker,
    *,
    prompt_files: bool = False,
) -> None:
    copilot_home = default_copilot_home(home)
    vscode_prompts = default_vscode_prompts_dir(home)
    copilot_skills = default_copilot_skills_dir(home)

    session = _TargetInstallSession.begin(copilot_home, TargetKind.COPILOT, home)

    print(
        "Exporting Craftsman suite to GitHub Copilot (VS Code User Profile, Agent Skills, agents and hooks)..."
    )

    templates = load_agent_templates(repo_dir)
    skill_manifests = sorted((repo_dir / "skills").glob("*/SKILL.md"))
    skill_names = [
        extract_skill_name(p.read_text(encoding="utf-8"), p.parent.name) for p in skill_manifests
    ]

    for prompts_dir in vscode_prompt_dirs(vscode_prompts):
        _record_missing_dirs(prompts_dir, session.created_dirs, session.stop_roots)
        if prompt_files:
            for s_name in skill_names:
                session.stage_path(prompts_dir / f"{s_name}.prompt.md", tracker)
        session.stage_path(prompts_dir / "craftsman-rules.instructions.md", tracker)

    if not prompt_files:
        _record_missing_dirs(copilot_skills, session.created_dirs, session.stop_roots)
        for s_name in skill_names:
            skill_dest = copilot_skills / s_name
            session.stage_path(skill_dest, tracker)

    _record_missing_dirs(copilot_home / "agents", session.created_dirs, session.stop_roots)
    for tmpl in templates:
        session.stage_path(copilot_home / "agents" / f"{tmpl.name}.agent.md", tracker)

    _record_missing_dirs(copilot_home / "hooks", session.created_dirs, session.stop_roots)
    hooks_file = copilot_home / "hooks" / HOOKS_FILENAME
    session.stage_path(hooks_file, tracker)

    if not tracker.dry_run:
        count, instr = export_to_vscode_user(repo_dir, vscode_prompts, prompt_files=prompt_files)
        skills_count = 0 if prompt_files else export_to_copilot_skills(repo_dir, copilot_skills)
        agents, written_hooks = export_copilot_home(repo_dir, copilot_home)
    else:
        count = len(skill_names) if prompt_files else 0
        instr = vscode_prompts / "craftsman-rules.instructions.md"
        skills_count = 0 if prompt_files else len(skill_names)
        agents = [copilot_home / "agents" / f"{t.name}.agent.md" for t in templates]
        written_hooks = hooks_file

    if prompt_files:
        print(f"[OK] Exported {count} Copilot prompt files -> {vscode_prompts}")
    print(f"[OK] Exported global Copilot instructions -> {instr}")
    if not prompt_files:
        print(f"[OK] Exported {skills_count} Copilot Agent Skills -> {copilot_skills}")
    print(f"[OK] Exported {len(agents)} Copilot custom agents -> {copilot_home / 'agents'}")
    print(f"[OK] Exported Copilot hooks -> {written_hooks}")

    _cleanup_legacy_copilot_user_files(
        repo_dir,
        home,
        prompt_files=prompt_files,
        stop_roots=session.stop_roots,
        dry_run=tracker.dry_run,
    )
    session.save(InstallMode.COPY, dry_run=tracker.dry_run, cleanup_obsolete=True)


def _stage_managed_markdown_file(
    target_file: Path,
    session: _TargetInstallSession,
    tracker: InstallTracker,
) -> tuple[bool, bytes | None]:
    """Prepare `target_file` for managed-block upsert; return `(existed_unowned, before_bytes)`."""
    if not _is_path_within_roots(target_file, (session.base_dir,)):
        raise InstallError(
            f"Refusing to modify {target_file}: path escapes workspace {session.base_dir}"
        )
    if target_file.is_symlink():
        _ensure_safe_target(target_file, session.owned_paths, tracker, stop_roots=session.stop_roots)
    abs_path = os.path.abspath(str(target_file))
    was_modified_by_us = (
        session.prev_target is not None
        and abs_path in session.prev_target.modified_files
        and abs_path not in session.owned_paths
    )
    existed_unowned = (
        target_file.is_file() and not _is_owned(target_file, session.owned_paths)
    ) or was_modified_by_us
    before_bytes = target_file.read_bytes() if target_file.is_file() else None
    bak_path = target_file.with_name(target_file.name + ".bak")
    if (
        target_file.is_file()
        and not _is_owned(target_file, session.owned_paths)
        and not bak_path.exists()
    ):
        if not tracker.dry_run:
            shutil.copy2(target_file, bak_path)
        tracker.record_backup(target_file, bak_path)
    return existed_unowned, before_bytes


def _record_managed_markdown_file(
    target_file: Path,
    existed_unowned: bool,
    before_bytes: bytes | None,
    session: _TargetInstallSession,
    tracker: InstallTracker,
) -> None:
    abs_path = os.path.abspath(str(target_file))
    session.modified_files[abs_path] = "managed_markdown"
    if not existed_unowned and abs_path not in session.created_paths:
        session.created_paths.append(abs_path)
    if tracker.dry_run:
        tracker.record_installed(target_file)
        return
    after_bytes = target_file.read_bytes() if target_file.is_file() else None
    if before_bytes is not None and after_bytes == before_bytes:
        tracker.record_skipped(target_file)
    else:
        tracker.record_installed(target_file)


def _cleanup_legacy_copilot_workspace_files(
    repo_dir: Path,
    workspace_dir: Path,
    *,
    prompt_files: bool,
    stop_roots: Collection[Path],
    dry_run: bool,
) -> list[Path]:
    """Remove Craftsman-generated Copilot files from non-canonical workspace locations."""
    valid_names = craftsman_entity_names(repo_dir)
    github_dir = workspace_dir / ".github"
    if not prompt_files:
        prompts_dir = github_dir / "prompts"
        if not prompts_dir.is_dir():
            return []
        return _remove_generated_copilot_matches(
            sorted(prompts_dir.glob("*.prompt.md")),
            valid_names,
            stop_roots=stop_roots,
            dry_run=dry_run,
        )
    skills_dir = github_dir / "skills"
    if not skills_dir.is_dir():
        return []
    return _remove_generated_copilot_matches(
        sorted(skills_dir.glob("*/SKILL.md")),
        valid_names,
        remove_parent=True,
        stop_roots=stop_roots,
        dry_run=dry_run,
    )


def install_copilot_workspace_target(
    repo_dir: Path,
    workspace_dir: Path,
    home: Path,
    tracker: InstallTracker,
    *,
    prompt_files: bool = False,
) -> None:
    ws = workspace_dir.resolve()
    session = _TargetInstallSession.begin(
        ws, TargetKind.COPILOT_WORKSPACE, home, ws, preserve_modified=True
    )

    print(f"Exporting Craftsman suite to GitHub Copilot workspace: {ws}...")
    github_dir = ws / ".github"
    templates = load_agent_templates(repo_dir)
    skill_manifests = sorted((repo_dir / "skills").glob("*/SKILL.md"))
    skill_names = [
        extract_skill_name(p.read_text(encoding="utf-8"), p.parent.name) for p in skill_manifests
    ]

    active_subs = ("prompts", "agents", "hooks") if prompt_files else ("skills", "agents", "hooks")
    for sub in active_subs:
        _record_missing_dirs(github_dir / sub, session.created_dirs, session.stop_roots)

    if prompt_files:
        for s_name in skill_names:
            session.stage_path(github_dir / "prompts" / f"{s_name}.prompt.md", tracker)
    else:
        for s_name in skill_names:
            s_dest = github_dir / "skills" / s_name
            session.stage_path(s_dest, tracker)

    for tmpl in templates:
        session.stage_path(github_dir / "agents" / f"{tmpl.name}.agent.md", tracker)

    session.stage_path(github_dir / "hooks" / HOOKS_FILENAME, tracker)

    copilot_instructions = github_dir / "copilot-instructions.md"
    existed_unowned, before_bytes = _stage_managed_markdown_file(copilot_instructions, session, tracker)

    if not tracker.dry_run:
        count, instr = export_to_workspace(repo_dir, ws, prompt_files=prompt_files)
    else:
        count = len(skill_names)
        instr = copilot_instructions
    _record_managed_markdown_file(copilot_instructions, existed_unowned, before_bytes, session, tracker)

    if prompt_files:
        print(f"[OK] Exported {count} workspace prompt files -> {github_dir / 'prompts'}")
    else:
        print(f"[OK] Exported {count} workspace Agent Skills -> {github_dir / 'skills'}")
    print(f"[OK] Exported workspace custom agents -> {github_dir / 'agents'}")
    print(
        f"[OK] Exported workspace hooks (runs `craftsman` from PATH) -> {github_dir / 'hooks' / HOOKS_FILENAME}"
    )
    print(f"[OK] Exported workspace Copilot instructions -> {instr}")

    _cleanup_legacy_copilot_workspace_files(
        repo_dir,
        ws,
        prompt_files=prompt_files,
        stop_roots=session.stop_roots,
        dry_run=tracker.dry_run,
    )
    session.save(InstallMode.COPY, dry_run=tracker.dry_run, cleanup_obsolete=True)


def install_agents_workspace_target(
    repo_dir: Path,
    workspace_dir: Path,
    home: Path,
    tracker: InstallTracker,
) -> None:
    ws = workspace_dir.resolve()
    session = _TargetInstallSession.begin(
        ws, TargetKind.AGENTS_WORKSPACE, home, ws, preserve_modified=True
    )

    print(f"Exporting universal AGENTS.md and Cursor rules to workspace: {ws}...")
    _record_missing_dirs(ws / ".cursor" / "rules", session.created_dirs, session.stop_roots)

    cursor_mdc = ws / ".cursor" / "rules" / "craftsman.mdc"
    session.stage_path(cursor_mdc, tracker)

    agents_md = ws / "AGENTS.md"
    existed_unowned, before_bytes = _stage_managed_markdown_file(agents_md, session, tracker)

    if not tracker.dry_run:
        written_agents, written_mdc = export_to_agents_workspace(repo_dir, ws)
    else:
        written_agents, written_mdc = agents_md, cursor_mdc
    _record_managed_markdown_file(agents_md, existed_unowned, before_bytes, session, tracker)

    print(f"[OK] Exported universal AGENTS.md (Codex/Cursor/Windsurf) -> {written_agents}")
    print(f"[OK] Exported Cursor rule -> {written_mdc}")

    session.save(InstallMode.COPY, dry_run=tracker.dry_run, cleanup_obsolete=True)


def install_git_hooks_target(
    repo_dir: Path,
    home: Path,
    mode: InstallMode,
    *,
    installing_antigravity: bool,
    tracker: InstallTracker,
) -> None:
    hooks_base_dir = resolve_git_hooks_base_dir(home, installing_antigravity=installing_antigravity)
    git_hooks_dir = hooks_base_dir / "git-hooks"

    session = _TargetInstallSession.begin(hooks_base_dir, TargetKind.GIT_HOOKS, home)

    if not installing_antigravity:
        _install_scripts_to(repo_dir, hooks_base_dir, mode, session, tracker)
    elif _resolve_safe(repo_dir) != _resolve_safe(hooks_base_dir):
        abs_pkg = os.path.abspath(str(hooks_base_dir / "craftsman"))
        if abs_pkg not in session.created_paths:
            session.created_paths.append(abs_pkg)
        for script_path in sorted((repo_dir / "scripts").glob("*.py")):
            if script_path.is_file():
                abs_s = os.path.abspath(str(hooks_base_dir / "scripts" / script_path.name))
                if abs_s not in session.created_paths:
                    session.created_paths.append(abs_s)

    _record_missing_dirs(git_hooks_dir, session.created_dirs, session.stop_roots)
    if not tracker.dry_run:
        git_hooks_dir.mkdir(parents=True, exist_ok=True)

    src_git_hooks_dir = repo_dir / "git-hooks"
    same_hooks_dir = _resolve_safe(src_git_hooks_dir) == _resolve_safe(git_hooks_dir)

    print(f"Installing global Git hooks into {git_hooks_dir}...")
    for hook_file in sorted(src_git_hooks_dir.iterdir()):
        if (
            not hook_file.is_file()
            or hook_file.name.startswith("_")
            or hook_file.name.startswith(".")
            or hook_file.name in STANDARD_PASSTHROUGH_GIT_HOOKS
            or hook_file.name.endswith(".bak")
            or ".bak." in hook_file.name
        ):
            continue
        target = git_hooks_dir / hook_file.name
        if same_hooks_dir:
            tracker.record_skipped(target)
            continue
        session.stage_path(target, tracker)
        if mode == InstallMode.SYMLINK:
            if not tracker.dry_run:
                target.symlink_to(hook_file)
        else:
            _copy_file_atomic(hook_file, target, executable=True, dry_run=tracker.dry_run)
        print(f"  + git-hook: {hook_file.name}")

    python_cmd_file = git_hooks_dir / ".python_cmd"
    if not tracker.dry_run:
        write_text_atomic(python_cmd_file, f"{sys.executable}\n")
    session.record_created_path(python_cmd_file, tracker)

    passthrough_src = src_git_hooks_dir / PASSTHROUGH_TEMPLATE_NAME
    if not passthrough_src.is_file():
        raise InstallError(f"Missing passthrough git hook template: {passthrough_src}")

    for passthrough_hook in STANDARD_PASSTHROUGH_GIT_HOOKS:
        target = git_hooks_dir / passthrough_hook
        session.stage_path(target, tracker)
        if mode == InstallMode.SYMLINK:
            if not tracker.dry_run:
                target.symlink_to(passthrough_src)
        else:
            _copy_file_atomic(passthrough_src, target, executable=True, dry_run=tracker.dry_run)
        print(f"  + git-hook (passthrough): {passthrough_hook}")

    prev_hooks_file = git_hooks_dir / ".previous_hooks_path"
    abs_prev_file = os.path.abspath(str(prev_hooks_file))
    raw_prev = git_config_get("core.hooksPath")
    expanded_prev = expand_tilde(raw_prev, home) if raw_prev else None
    git_hooks_resolved = _resolve_safe(git_hooks_dir)

    recorded_prev_hooks: str | None = None
    if expanded_prev:
        prev_resolved = _resolve_safe(Path(expanded_prev))
        migrated_from_other_craftsman = False
        if prev_resolved != git_hooks_resolved and prev_resolved.name == "git-hooks":
            old_base = prev_resolved.parent
            if (old_base / MANIFEST_FILENAME).is_file():
                try:
                    old_manifest = load_manifest(old_base)
                except InstallError:
                    old_manifest = None
                if old_manifest is not None and TargetKind.GIT_HOOKS.value in old_manifest.targets:
                    migrated_from_other_craftsman = True
                    old_gh_target = old_manifest.targets[TargetKind.GIT_HOOKS.value]
                    prior_from_old = old_gh_target.previous_hooks_path
                    if prior_from_old is None and (prev_resolved / ".previous_hooks_path").is_file():
                        cand = (
                            (prev_resolved / ".previous_hooks_path").read_text(encoding="utf-8").strip()
                        )
                        if cand and _resolve_safe(Path(expand_tilde(cand, home))) not in (
                            prev_resolved,
                            git_hooks_resolved,
                        ):
                            prior_from_old = cand
                    _uninstall_from_base(
                        repo_dir,
                        old_base,
                        [TargetKind.GIT_HOOKS.value],
                        home,
                        UninstallTracker(dry_run=tracker.dry_run),
                    )
                    if prior_from_old:
                        recorded_prev_hooks = prior_from_old
                        expanded_prior = expand_tilde(prior_from_old, home)
                        if not tracker.dry_run:
                            write_text_atomic(prev_hooks_file, f"{expanded_prior}\n")
                        if abs_prev_file not in session.created_paths:
                            session.created_paths.append(abs_prev_file)
                    else:
                        _remove_path_if_exists(
                            prev_hooks_file, dry_run=tracker.dry_run, stop_roots=session.stop_roots
                        )

        if not migrated_from_other_craftsman:
            if prev_resolved != git_hooks_resolved:
                recorded_prev_hooks = raw_prev
                if not tracker.dry_run:
                    write_text_atomic(prev_hooks_file, f"{expanded_prev}\n")
                if abs_prev_file not in session.created_paths:
                    session.created_paths.append(abs_prev_file)
                print(f"  ! backed up previous core.hooksPath ({raw_prev}) -> {prev_hooks_file}")
            else:
                prior = session.prev_target.previous_hooks_path if session.prev_target else None
                if prior is None and prev_hooks_file.is_file():
                    candidate_prior = prev_hooks_file.read_text(encoding="utf-8").strip()
                    if (
                        candidate_prior
                        and _resolve_safe(Path(expand_tilde(candidate_prior, home)))
                        != git_hooks_resolved
                    ):
                        prior = candidate_prior
                if prior:
                    recorded_prev_hooks = prior
                    expanded_prior = expand_tilde(prior, home)
                    if not tracker.dry_run:
                        write_text_atomic(prev_hooks_file, f"{expanded_prior}\n")
                    if abs_prev_file not in session.created_paths:
                        session.created_paths.append(abs_prev_file)
                else:
                    _remove_path_if_exists(
                        prev_hooks_file, dry_run=tracker.dry_run, stop_roots=session.stop_roots
                    )
    else:
        _remove_path_if_exists(prev_hooks_file, dry_run=tracker.dry_run, stop_roots=session.stop_roots)

    if not tracker.dry_run:
        _git_config_set("core.hooksPath", str(git_hooks_dir))

    print(f"[OK] Configured git config --global core.hooksPath -> {git_hooks_dir}")
    print(
        "     To revert global Git hooks, run 'craftsman uninstall --git-hooks' (or 'craftsman uninstall'); "
        "pass '--no-git-hooks' during install to skip setting them."
    )

    session.save(
        mode,
        dry_run=tracker.dry_run,
        cleanup_obsolete=True,
        previous_hooks_path=recorded_prev_hooks,
        hooks_dir=str(git_hooks_dir),
    )


def _build_cli_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=default_repo_dir(),
        help="Path to the Craftsman repository root.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-detect targets (default when no target flags are given).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Target all platforms: Antigravity + Claude Code + GitHub Copilot + Git Hooks.",
    )
    parser.add_argument(
        "--copilot",
        action="store_true",
        help="Target GitHub Copilot user-level files, skills, custom agents and hooks.",
    )
    parser.add_argument(
        "--copilot-prompt-files",
        action="store_true",
        help="With --copilot or --copilot-workspace: install legacy VS Code `.prompt.md` files instead of Agent Skills (`SKILL.md`).",
    )
    parser.add_argument(
        "--allow-duplicate-scopes",
        action="store_true",
        help="Allow installing Copilot files in both user (~/.copilot) and workspace (.github) scopes.",
    )
    parser.add_argument(
        "--claude",
        action="store_true",
        help="Target Claude Code slash commands, rules, reviewer/verifier subagents and gate hooks in ~/.claude/.",
    )
    parser.add_argument(
        "--claude-hooks",
        action="store_true",
        help="Same as --claude (the gate hooks in ~/.claude/settings.json are installed by default).",
    )
    parser.add_argument(
        "--no-claude-hooks",
        action="store_true",
        help="With the Claude Code target: skip the Craftsman gate hooks in ~/.claude/settings.json.",
    )
    parser.add_argument(
        "--antigravity",
        action="store_true",
        help="Target skills, rules, scripts, and hooks.json in ~/.gemini/config/.",
    )
    parser.add_argument(
        "--copilot-workspace",
        type=Path,
        metavar="<path>",
        help="Target .github/{skills,agents,hooks} and copilot-instructions.md in a workspace.",
    )
    parser.add_argument(
        "--agents-workspace",
        "--codex-workspace",
        "--cursor-workspace",
        dest="agents_workspace",
        type=Path,
        metavar="<path>",
        help="Target universal AGENTS.md and .cursor/rules/craftsman.mdc in a workspace.",
    )
    parser.add_argument(
        "--git-hooks",
        action="store_true",
        help="Target global Git hooks and git config --global core.hooksPath.",
    )
    parser.add_argument(
        "--no-git-hooks",
        action="store_true",
        help="Skip global Git hooks when running in default/auto mode.",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy skills, rules, scripts, and merge hooks.json into ~/.gemini/config/.",
    )
    parser.add_argument(
        "--symlink",
        action="store_true",
        help="Symlink skills, rules, and scripts (and merge hooks.json) into ~/.gemini/config/.",
    )
    parser.add_argument(
        "--plugin",
        action="store_true",
        help="Install as a toggleable Antigravity plugin in ~/.gemini/config/plugins/craftsman.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full plan without modifying any files or Git configuration.",
    )
    return parser


def _validate_cli_flags(parsed: argparse.Namespace) -> InstallMode:
    modes_requested: list[tuple[str, InstallMode]] = [
        (flag, mode)
        for flag, active, mode in (
            ("--copy", parsed.copy, InstallMode.COPY),
            ("--symlink", parsed.symlink, InstallMode.SYMLINK),
            ("--plugin", parsed.plugin, InstallMode.PLUGIN),
        )
        if active
    ]
    if len(modes_requested) > 1:
        names = ", ".join(flag for flag, _ in modes_requested)
        raise InstallError(
            f"Conflicting mode flags: {names} (--copy, --symlink, and --plugin are mutually exclusive)."
        )
    if parsed.no_git_hooks and parsed.git_hooks:
        raise InstallError("Conflicting flags: --no-git-hooks cannot be combined with --git-hooks.")
    if parsed.no_git_hooks and parsed.all:
        raise InstallError("Conflicting flags: --no-git-hooks cannot be combined with --all.")
    if parsed.no_claude_hooks and parsed.claude_hooks:
        raise InstallError(
            "Conflicting flags: --no-claude-hooks cannot be combined with --claude-hooks."
        )
    return modes_requested[0][1] if modes_requested else InstallMode.COPY


def parse_install_args(argv: Sequence[str]) -> InstallConfig:
    parser = _build_cli_parser(
        "craftsman install",
        "Install Craftsman across Antigravity, Claude Code, GitHub Copilot, and global Git hooks.",
    )
    parsed = parser.parse_args(list(argv))
    mode = _validate_cli_flags(parsed)

    install_antigravity = bool(
        parsed.all
        or parsed.antigravity
        or ((parsed.copy or parsed.symlink or parsed.plugin) and not parsed.auto)
    )
    install_claude = bool(parsed.all or parsed.claude or parsed.claude_hooks)
    copilot_ws = parsed.copilot_workspace
    install_copilot = bool(
        parsed.all or parsed.copilot or (parsed.copilot_prompt_files and copilot_ws is None)
    )
    install_git_hooks = bool(parsed.all or parsed.git_hooks)
    agents_ws = parsed.agents_workspace

    explicit_target = bool(
        parsed.all
        or install_antigravity
        or install_claude
        or install_copilot
        or install_git_hooks
        or copilot_ws is not None
        or agents_ws is not None
    )

    return InstallConfig(
        repo_dir=parsed.repo_dir.resolve(),
        mode=mode,
        auto_detect=bool(parsed.auto) or not explicit_target,
        install_antigravity=install_antigravity,
        install_claude=install_claude,
        disable_claude_hooks=bool(parsed.no_claude_hooks),
        install_copilot=install_copilot,
        copilot_prompt_files=bool(parsed.copilot_prompt_files),
        allow_duplicate_scopes=bool(parsed.allow_duplicate_scopes),
        copilot_workspace=copilot_ws,
        agents_workspace=agents_ws,
        install_git_hooks=install_git_hooks,
        disable_git_hooks=bool(parsed.no_git_hooks),
        dry_run=bool(parsed.dry_run),
    )


def resolve_install_config(config: InstallConfig, home: Path | None = None) -> InstallConfig:
    if not config.auto_detect:
        return config
    h = user_home(home)
    print("Auto-detecting installed AI coding platforms...")
    ag = detect_antigravity(home) or config.install_antigravity
    if ag:
        print("  * Detected: Google Antigravity")
    cl = detect_claude(home) or config.install_claude
    if cl:
        print("  * Detected: Claude Code")
    cp = detect_vscode_copilot(home) or config.install_copilot
    if cp:
        print("  * Detected: GitHub Copilot (VS Code)")
    gh = (not config.disable_git_hooks) or config.install_git_hooks
    if not ag and not cl and not cp:
        print("  ! No existing IDE directory detected; defaulting to GitHub Copilot user export.")
        cp = True
    _ = h
    return InstallConfig(
        repo_dir=config.repo_dir,
        mode=config.mode,
        auto_detect=False,
        install_antigravity=ag,
        install_claude=cl,
        disable_claude_hooks=config.disable_claude_hooks,
        install_copilot=cp,
        copilot_prompt_files=config.copilot_prompt_files,
        allow_duplicate_scopes=config.allow_duplicate_scopes,
        copilot_workspace=config.copilot_workspace,
        agents_workspace=config.agents_workspace,
        install_git_hooks=gh,
        disable_git_hooks=config.disable_git_hooks,
        dry_run=config.dry_run,
    )


def run_install(config: InstallConfig, home: Path | None = None) -> InstallTracker:
    h = user_home(home)
    repo_dir = config.repo_dir
    if not (repo_dir / "skills").is_dir() or not (repo_dir / "rules").is_dir():
        raise InstallError(f"Repository assets (skills/, rules/) not found under {repo_dir}")

    resolved = resolve_install_config(config, home)
    if not resolved.allow_duplicate_scopes:
        if resolved.copilot_workspace is not None:
            if resolved.install_copilot or has_user_copilot_install(h, repo_dir):
                raise InstallError(DUPLICATE_SCOPE_USER_TO_WS_MSG)
        if resolved.install_copilot:
            ws_check = (
                resolved.copilot_workspace.resolve()
                if resolved.copilot_workspace is not None
                else Path.cwd().resolve()
            )
            if has_workspace_copilot_install(ws_check, repo_dir):
                raise InstallError(DUPLICATE_SCOPE_WS_TO_USER_MSG)

    tracker = InstallTracker(dry_run=resolved.dry_run)
    if resolved.dry_run:
        print("[DRY-RUN] Planning installation (no files or Git config will be modified)...")

    should_install_git_hooks = resolved.install_git_hooks
    if resolved.install_antigravity and not should_install_git_hooks and not resolved.disable_git_hooks:
        ag_cfg = h / ".gemini" / "config"
        if (ag_cfg / "git-hooks").is_dir() and (ag_cfg / MANIFEST_FILENAME).is_file():
            try:
                existing_ag = load_manifest(ag_cfg)
            except InstallError:
                existing_ag = None
            if existing_ag is not None and TargetKind.GIT_HOOKS.value in existing_ag.targets:
                should_install_git_hooks = True

    if resolved.install_antigravity:
        install_antigravity_target(repo_dir, h, resolved.mode, tracker)

    if resolved.install_claude:
        install_claude_target(repo_dir, h, with_hooks=not resolved.disable_claude_hooks, tracker=tracker)

    if resolved.install_copilot:
        install_copilot_target(repo_dir, h, tracker, prompt_files=resolved.copilot_prompt_files)

    if resolved.copilot_workspace is not None:
        install_copilot_workspace_target(
            repo_dir,
            resolved.copilot_workspace,
            h,
            tracker,
            prompt_files=resolved.copilot_prompt_files,
        )

    if resolved.agents_workspace is not None:
        install_agents_workspace_target(repo_dir, resolved.agents_workspace, h, tracker)

    if should_install_git_hooks:
        install_git_hooks_target(
            repo_dir,
            h,
            resolved.mode,
            installing_antigravity=resolved.install_antigravity,
            tracker=tracker,
        )

    prefix = "[DRY-RUN] " if resolved.dry_run else ""
    print(
        f"{prefix}Summary: {len(tracker.installed)} installed, "
        f"{len(tracker.backed_up)} backed up, {len(tracker.skipped)} skipped."
    )
    return tracker


def parse_uninstall_args(argv: Sequence[str]) -> UninstallConfig:
    parser = _build_cli_parser(
        "craftsman uninstall",
        "Uninstall Craftsman targets recorded in install manifests and restore global Git hooks.",
    )
    parsed = parser.parse_args(list(argv))
    _validate_cli_flags(parsed)

    uninstall_antigravity = bool(
        parsed.all or parsed.antigravity or parsed.copy or parsed.symlink or parsed.plugin
    )
    uninstall_claude = bool(parsed.all or parsed.claude or parsed.claude_hooks)
    uninstall_copilot = bool(parsed.all or parsed.copilot)
    uninstall_git_hooks = bool(parsed.all or parsed.git_hooks)
    copilot_ws = parsed.copilot_workspace
    agents_ws = parsed.agents_workspace

    explicit_target = bool(
        parsed.all
        or uninstall_antigravity
        or uninstall_claude
        or uninstall_copilot
        or uninstall_git_hooks
        or copilot_ws is not None
        or agents_ws is not None
    )

    if not explicit_target:
        return UninstallConfig(
            repo_dir=parsed.repo_dir.resolve(),
            uninstall_antigravity=True,
            uninstall_claude=True,
            uninstall_copilot=True,
            copilot_workspace=None,
            agents_workspace=None,
            uninstall_git_hooks=not parsed.no_git_hooks,
            dry_run=bool(parsed.dry_run),
        )

    return UninstallConfig(
        repo_dir=parsed.repo_dir.resolve(),
        uninstall_antigravity=uninstall_antigravity,
        uninstall_claude=uninstall_claude,
        uninstall_copilot=uninstall_copilot,
        copilot_workspace=copilot_ws,
        agents_workspace=agents_ws,
        uninstall_git_hooks=uninstall_git_hooks,
        dry_run=bool(parsed.dry_run),
    )


def _restore_git_hooks_config(
    base_dir: Path,
    t_man: TargetManifest,
    home: Path,
    tracker: UninstallTracker,
) -> None:
    hooks_dir = Path(t_man.hooks_dir) if t_man.hooks_dir else (base_dir / "git-hooks")
    prev_hooks_file = hooks_dir / ".previous_hooks_path"

    prev_hooks = t_man.previous_hooks_path
    if prev_hooks is None and prev_hooks_file.is_file():
        candidate = prev_hooks_file.read_text(encoding="utf-8").strip()
        if candidate:
            prev_hooks = candidate

    if _remove_path_if_exists(prev_hooks_file, dry_run=tracker.dry_run, stop_roots=(base_dir, home)):
        tracker.record_removed(prev_hooks_file)

    current_hooks = git_config_get("core.hooksPath")
    if current_hooks is None:
        return

    current_resolved = _resolve_safe(Path(expand_tilde(current_hooks, home)))
    expected_resolved = _resolve_safe(hooks_dir)

    if current_resolved == expected_resolved:
        if prev_hooks:
            if not tracker.dry_run:
                _git_config_set("core.hooksPath", prev_hooks)
            tracker.restored.append(f"core.hooksPath -> {prev_hooks}")
            print(f"[OK] Restored git config --global core.hooksPath -> {prev_hooks}")
        else:
            if not tracker.dry_run:
                _git_config_unset("core.hooksPath")
            tracker.restored.append("core.hooksPath (unset)")
            print("[OK] Unset git config --global core.hooksPath")
    else:
        if prev_hooks and current_resolved == _resolve_safe(Path(expand_tilde(prev_hooks, home))):
            return
        warn_msg = (
            f"Leaving git config --global core.hooksPath ({current_hooks}) untouched "
            f"because it was changed and no longer points to {hooks_dir}."
        )
        tracker.warnings.append(warn_msg)
        sys.stderr.write(f"[WARN] {warn_msg}\n")


def _uninstall_from_base(
    repo_dir: Path,
    base_dir: Path,
    target_keys: Sequence[str],
    home: Path,
    tracker: UninstallTracker,
    *,
    is_workspace: bool = False,
) -> None:
    manifest = load_manifest(base_dir)
    if manifest is None:
        return

    matching_keys = [k for k in target_keys if k in manifest.targets]
    if not matching_keys and "default" in manifest.targets:
        matching_keys = ["default"]
    if not matching_keys:
        return

    remaining_targets = {k: v for k, v in manifest.targets.items() if k not in matching_keys}
    remaining_owned_paths: set[str] = {p for t in remaining_targets.values() for p in t.created_paths}
    remaining_created_dirs: set[str] = {d for t in remaining_targets.values() for d in t.created_dirs}

    dirs_to_prune: list[str] = []

    for key in matching_keys:
        t_man = manifest.targets[key]
        allowed_roots = (base_dir,) if is_workspace else _allowed_roots_for_target(base_dir, key, home)
        stop_roots: set[Path] = set(allowed_roots) | {home}

        if not is_workspace and (
            key == TargetKind.GIT_HOOKS.value or t_man.target == TargetKind.GIT_HOOKS.value
        ):
            _restore_git_hooks_config(base_dir, t_man, home, tracker)

        for mod_path_str, mod_kind in t_man.modified_files.items():
            mod_path = Path(mod_path_str)
            if not _is_path_within_roots(mod_path, allowed_roots):
                warn_msg = f"Skipping out-of-bounds modified_files path {mod_path} in {base_dir}"
                tracker.warnings.append(warn_msg)
                continue
            created_by_us = mod_path_str in t_man.created_paths
            if mod_kind == "claude_settings":
                if not tracker.dry_run:
                    changed = remove_claude_hooks(mod_path, create_backup=not created_by_us)
                else:
                    changed = _has_claude_craftsman_hooks(mod_path)
                if created_by_us and mod_path.is_file():
                    if not tracker.dry_run:
                        try:
                            loaded = json.loads(mod_path.read_text(encoding="utf-8"))
                            if loaded == {}:
                                mod_path.unlink()
                                tracker.record_removed(mod_path)
                                print(f"  - removed: {mod_path}")
                                continue
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    else:
                        tracker.record_removed(mod_path)
                        print(f"  - removed: {mod_path}")
                        continue
                if changed:
                    tracker.restored.append(str(mod_path))
                    print(f"  - removed Craftsman hooks from {mod_path}")
            elif mod_kind == "hooks_json":
                changed = _unmerge_antigravity_hooks_json(repo_dir, mod_path, dry_run=tracker.dry_run)
                if created_by_us and mod_path.is_file():
                    if not tracker.dry_run:
                        try:
                            loaded = json.loads(mod_path.read_text(encoding="utf-8"))
                            if loaded == {}:
                                mod_path.unlink()
                                tracker.record_removed(mod_path)
                                print(f"  - removed: {mod_path}")
                                continue
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    else:
                        tracker.record_removed(mod_path)
                        print(f"  - removed: {mod_path}")
                        continue
                if changed:
                    tracker.restored.append(str(mod_path))
                    print(f"  - removed Craftsman hooks from {mod_path}")
            elif mod_kind == "managed_markdown":
                if not tracker.dry_run:
                    changed = remove_managed_markdown(mod_path)
                else:
                    changed = _has_managed_markdown_block(mod_path)
                if created_by_us and mod_path.is_file():
                    if not tracker.dry_run:
                        try:
                            if mod_path.read_text(encoding="utf-8").strip() == "":
                                mod_path.unlink()
                                tracker.record_removed(mod_path)
                                print(f"  - removed: {mod_path}")
                                continue
                        except (OSError, UnicodeDecodeError):
                            pass
                    else:
                        tracker.record_removed(mod_path)
                        print(f"  - removed: {mod_path}")
                        continue
                if changed:
                    tracker.restored.append(str(mod_path))
                    print(f"  - removed Craftsman block from {mod_path}")

        sorted_paths = sorted(t_man.created_paths, key=lambda s: len(Path(s).parts), reverse=True)
        for p_str in sorted_paths:
            if p_str in remaining_owned_paths or p_str in t_man.modified_files:
                continue
            p = Path(p_str)
            if not _is_path_within_roots(p, allowed_roots):
                warn_msg = f"Skipping out-of-bounds created_paths entry {p} in {base_dir}"
                tracker.warnings.append(warn_msg)
                continue
            if _remove_path_if_exists(p, dry_run=tracker.dry_run, stop_roots=stop_roots):
                tracker.record_removed(p)
                print(f"  - removed: {p}")

        for d_str in t_man.created_dirs:
            if d_str not in remaining_created_dirs and d_str not in dirs_to_prune:
                dirs_to_prune.append(d_str)

    manifest_path = base_dir / MANIFEST_FILENAME
    if not remaining_targets:
        if _remove_path_if_exists(manifest_path, dry_run=tracker.dry_run, stop_roots=(base_dir, home)):
            tracker.record_removed(manifest_path)

    surviving_dirs: list[str] = []
    for d_str in sorted(dirs_to_prune, key=lambda s: len(Path(s).parts), reverse=True):
        d = Path(d_str)
        if not tracker.dry_run and d.is_dir() and not d.is_symlink():
            try:
                d.rmdir()
            except OSError:
                surviving_dirs.append(d_str)
        elif d.exists():
            surviving_dirs.append(d_str)

    if remaining_targets:
        if surviving_dirs:
            first_key = sorted(remaining_targets.keys())[0]
            first_t = remaining_targets[first_key]
            merged_dirs = list(first_t.created_dirs)
            for sd in reversed(surviving_dirs):
                if sd not in merged_dirs:
                    merged_dirs.append(sd)
            remaining_targets[first_key] = TargetManifest(
                target=first_t.target,
                mode=first_t.mode,
                version=first_t.version,
                created_paths=first_t.created_paths,
                created_dirs=tuple(merged_dirs),
                modified_files=first_t.modified_files,
                previous_hooks_path=first_t.previous_hooks_path,
                hooks_dir=first_t.hooks_dir,
            )
        updated = BaseManifest.from_targets(remaining_targets, default_mode=manifest.mode)
        save_manifest(base_dir, updated, dry_run=tracker.dry_run)


def _prune_empty_dirs(seen_dirs: Iterable[Path], stop_roots: Collection[Path]) -> None:
    for directory in sorted(seen_dirs, key=lambda p: len(p.parts), reverse=True):
        cur = directory
        while cur not in stop_roots and cur.parent != cur:
            if cur.is_dir() and not cur.is_symlink():
                try:
                    cur.rmdir()
                except OSError:
                    break
            cur = cur.parent


def _uninstall_legacy_copilot_files(
    repo_dir: Path,
    home: Path,
    tracker: UninstallTracker,
    *,
    workspace: Path | None = None,
) -> None:
    """Remove any remaining Craftsman-generated Copilot files discovered via `COPILOT_DISCOVERY`."""
    valid_names = craftsman_entity_names(repo_dir)
    stop_roots = _stop_roots_for(home, *([workspace] if workspace is not None else []))
    target_scopes = ("workspace",) if workspace is not None else ("user", "profile")
    seen_dirs: set[Path] = set()
    locs = [
        loc
        for kind in ("agent", "skill", "prompt")
        for loc in COPILOT_DISCOVERY[("vscode", kind)]
        if loc.scope in target_scopes
    ]
    for loc in locs:
        for directory in resolve_discovery_dirs(
            loc, home=home, workspace=workspace, include_all_profiles=True
        ):
            if not directory.is_dir():
                continue
            seen_dirs.add(directory)
            matches = [p for n, p in iter_discovered_files(directory, loc.pattern) if n in valid_names]
            for rem in _remove_generated_copilot_matches(
                matches,
                valid_names,
                remove_parent=(loc.pattern == "*/SKILL.md"),
                stop_roots=stop_roots,
                dry_run=tracker.dry_run,
                label="removed",
            ):
                tracker.record_removed(rem)
    if workspace is None:
        instrs = [
            d / "craftsman-rules.instructions.md"
            for d in vscode_prompt_dirs(default_vscode_prompts_dir(home))
            if (d / "craftsman-rules.instructions.md").is_file()
        ]
        seen_dirs.update(p.parent for p in instrs)
        for rem in _remove_generated_copilot_matches(
            instrs, valid_names, stop_roots=stop_roots, dry_run=tracker.dry_run, label="removed"
        ):
            tracker.record_removed(rem)
    if not tracker.dry_run:
        _prune_empty_dirs(seen_dirs, stop_roots)


def run_uninstall(config: UninstallConfig, home: Path | None = None) -> UninstallTracker:
    h = user_home(home)
    tracker = UninstallTracker(dry_run=config.dry_run)
    if config.dry_run:
        print("[DRY-RUN] Planning uninstallation (no files or Git config will be modified)...")

    config_dir = h / ".gemini" / "config"
    craftsman_home_env = env_var_for_home("CRAFTSMAN_HOME", home)
    craftsman_home = Path(craftsman_home_env) if craftsman_home_env else (h / ".craftsman")
    claude_dir = default_claude_dir(home)
    copilot_home = default_copilot_home(home)

    if config.uninstall_antigravity or config.uninstall_git_hooks:
        ag_keys: list[str] = []
        if config.uninstall_antigravity:
            ag_keys.append(TargetKind.ANTIGRAVITY.value)
        if config.uninstall_git_hooks:
            ag_keys.append(TargetKind.GIT_HOOKS.value)
        _uninstall_from_base(config.repo_dir, config_dir, ag_keys, h, tracker)

    if config.uninstall_git_hooks and _resolve_safe(craftsman_home) != _resolve_safe(config_dir):
        _uninstall_from_base(config.repo_dir, craftsman_home, [TargetKind.GIT_HOOKS.value], h, tracker)

    if config.uninstall_claude:
        claude_manifest = load_manifest(claude_dir)
        if claude_manifest is not None:
            _uninstall_from_base(config.repo_dir, claude_dir, [TargetKind.CLAUDE.value], h, tracker)
        else:
            settings_path = claude_dir / "settings.json"
            if settings_path.is_file():
                if not config.dry_run:
                    changed = remove_claude_hooks(settings_path)
                else:
                    changed = _has_claude_craftsman_hooks(settings_path)
                if changed:
                    tracker.restored.append(str(settings_path))
                    print(f"  - removed Craftsman hooks from {settings_path}")

    if config.uninstall_copilot:
        _uninstall_from_base(config.repo_dir, copilot_home, [TargetKind.COPILOT.value], h, tracker)
        _uninstall_legacy_copilot_files(config.repo_dir, h, tracker)

    if config.copilot_workspace is not None:
        ws_resolved = config.copilot_workspace.resolve()
        _uninstall_from_base(
            config.repo_dir,
            ws_resolved,
            [TargetKind.COPILOT_WORKSPACE.value],
            h,
            tracker,
            is_workspace=True,
        )
        _uninstall_legacy_copilot_files(config.repo_dir, h, tracker, workspace=ws_resolved)

    if config.agents_workspace is not None:
        _uninstall_from_base(
            config.repo_dir,
            config.agents_workspace.resolve(),
            [TargetKind.AGENTS_WORKSPACE.value],
            h,
            tracker,
            is_workspace=True,
        )

    prefix = "[DRY-RUN] " if config.dry_run else ""
    if not tracker.removed and not tracker.restored:
        print(f"{prefix}Nothing to uninstall (no Craftsman install manifests found).")
    else:
        print(
            f"{prefix}Uninstall summary: {len(tracker.removed)} path(s) removed, "
            f"{len(tracker.restored)} configuration(s) restored."
        )
    return tracker


def install_main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        config = parse_install_args(args_list)
        run_install(config)
    except (InstallError, ExportError, OSError) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    return 0


def uninstall_main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        config = parse_uninstall_args(args_list)
        run_uninstall(config)
    except (InstallError, ExportError, OSError) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 1
    return 0
