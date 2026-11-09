"""Google Antigravity detection and installer (`~/.gemini/config`)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from craftsman.assets import iter_skills
from craftsman.files import copy_once_backup, exists
from craftsman.generated import ExportError, read_json_object, write_json_object
from craftsman.manifest import InstallError, TargetKind, load_manifest
from craftsman.paths import resolve_craftsman_pkg_dir
from craftsman.staging import (
    EventKind,
    InstallMode,
    TargetSession,
    Tracker,
    created_before,
    install_runtime,
    install_tree,
    path_key,
    revert_json_file,
    strip_hooks_json,
)


def detect_antigravity(home: Path) -> bool:
    return (
        (home / ".gemini" / "antigravity").is_dir()
        or (home / ".gemini" / "config").is_dir()
        or bool(os.environ.get("AGY_MANAGED_ENV"))
        or bool(os.environ.get("ANTIGRAVITY_AGENT"))
    )


PLUGIN_NAME = "craftsman"


def is_own_plugin(config_dir: Path) -> bool:
    """Whether `craftsman install --antigravity --plugin` created `config_dir/plugins/craftsman`."""
    try:
        manifest = load_manifest(config_dir)
    except InstallError:
        return False  # who installed the plugin is unknown; the install reports the manifest
    previous = manifest.targets.get(TargetKind.ANTIGRAVITY) if manifest else None
    plugin_dir = config_dir / "plugins" / PLUGIN_NAME
    return previous is not None and path_key(plugin_dir) in previous.created_paths


def is_agy_plugin_active(home: Path) -> bool:
    """Whether `agy plugin install` put an enabled Craftsman plugin in `~/.gemini/config/plugins`.

    The plugin ships the skills, rules and gate hooks a copy or symlink install adds once more.
    Craftsman's own plugin (`is_own_plugin`) is no such plugin: the install modes replace each
    other. agy keeps a plugin's on/off switch in `config.json`
    (`{"plugins": {"<name>": {"enabled": false}}}`); a plugin without one is enabled.
    """
    config_dir = home / ".gemini" / "config"
    if not (config_dir / "plugins" / PLUGIN_NAME / "plugin.json").is_file() or is_own_plugin(config_dir):
        return False
    settings_file = config_dir / "config.json"
    try:
        settings = read_json_object(settings_file) if exists(settings_file) else {}
    except ExportError:
        settings = {}  # agy cannot read it either, so nothing switches the plugin off
    plugins = settings.get("plugins")
    entry = plugins.get(PLUGIN_NAME) if isinstance(plugins, dict) else None
    return not (isinstance(entry, dict) and entry.get("enabled") is False)


def install_antigravity_target(repo_dir: Path, home: Path, mode: InstallMode, tracker: Tracker) -> None:
    config_dir = home / ".gemini" / "config"
    hooks_json = config_dir / "hooks.json"
    # Read before anything is written: a hooks.json that is no JSON object stops the install.
    existing_hooks = read_json_object(hooks_json) if exists(hooks_json) else None
    session = TargetSession.begin(config_dir, TargetKind.ANTIGRAVITY, home, tracker)
    for sub in ("skills", "rules", "scripts", "plugins"):
        session.make_dirs(config_dir / sub)
    install_runtime(session, repo_dir, mode)
    if mode is InstallMode.PLUGIN:
        _revert_hooks_json(session, hooks_json)
        _install_plugin(session, repo_dir, config_dir / "plugins" / PLUGIN_NAME)
    else:
        for rule in sorted((repo_dir / "rules").glob("*.md")):
            install_tree(session, rule, config_dir / "rules" / rule.name, mode)
        for skill in iter_skills(repo_dir):
            install_tree(session, skill.directory, config_dir / "skills" / skill.name, mode)
        _merge_hooks_json(session, read_json_object(repo_dir / "hooks.json"), hooks_json, existing_hooks)
    session.finish()


# What the plugin loader reads besides `scripts/` and the package, which the hooks run.
# The agent templates stay out; agy would load them unrendered.
PLUGIN_ENTRIES = ("plugin.json", "hooks.json", "skills", "rules")


def _install_plugin(session: TargetSession, repo_dir: Path, plugin_dir: Path) -> None:
    """Copy the plugin into `plugin_dir`: agy does not load a plugin folder that is a symlink."""
    session.stage(plugin_dir)
    session.make_dirs(plugin_dir)
    for name in PLUGIN_ENTRIES:
        install_tree(session, repo_dir / name, plugin_dir / name, InstallMode.COPY)
    session.make_dirs(plugin_dir / "scripts")
    for script in sorted(path for path in (repo_dir / "scripts").glob("*.py") if path.is_file()):
        target = plugin_dir / "scripts" / script.name
        install_tree(session, script, target, InstallMode.COPY, executable=True)
    install_tree(session, resolve_craftsman_pkg_dir(repo_dir), plugin_dir / "craftsman", InstallMode.COPY)


def _revert_hooks_json(session: TargetSession, hooks_json: Path) -> None:
    """Plugin mode ships its hooks in the plugin: take the ones a copy install merged back out."""
    previous = session.previous
    if previous is not None and previous.hooks_json_keys:
        revert_json_file(
            hooks_json, previous, session.tracker, strip_hooks_json(previous.hooks_json_keys)
        )


LEGACY_ANTIGRAVITY_HOOK_KEYS = ("craftsman-git-guard",)


def _merge_hooks_json(
    session: TargetSession, ours: dict[str, Any], target: Path, existing: dict[str, Any] | None
) -> None:
    """Add Craftsman's entries to the user's hooks.json; the user's `enabled` flags are kept."""
    previous_keys = session.previous.hooks_json_keys if session.previous else ()
    stale = (set(previous_keys) | set(LEGACY_ANTIGRAVITY_HOOK_KEYS)) - set(ours)
    merged = {key: value for key, value in (existing or {}).items() if key not in stale}
    for key, entry in ours.items():
        merged[key] = _with_users_enabled_flag(entry, merged.get(key))
    session.merged_hooks_json(target, ours)
    if existing is None or created_before(session, target):
        session.created(target)
    elif merged == existing:
        session.tracker.add(EventKind.UNCHANGED, target)
        return
    else:
        if (backup := copy_once_backup(target, dry_run=session.dry_run)) is not None:
            session.tracker.backed_up(target, backup)
        session.tracker.add(EventKind.INSTALLED, target)
    if not session.dry_run:
        write_json_object(target, merged)


def _with_users_enabled_flag(ours: Any, theirs: Any) -> Any:
    if isinstance(ours, dict) and isinstance(theirs, dict) and isinstance(theirs.get("enabled"), bool):
        return {**ours, "enabled": theirs["enabled"]}
    return ours
