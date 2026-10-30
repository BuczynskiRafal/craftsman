"""Removal of the global git hooks installed by Craftsman < 4.0; delete in 5.0."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from craftsman.export import (
    env_var_for_home,
    user_home,
)
from craftsman.manifest import InstallError, TargetManifest
from craftsman.staging import EventKind, Tracker

GIT_TIMEOUT_S = 5
HOOKS_DIR_NAME = "git-hooks"


def expand_tilde(raw: str, home: Path | None = None) -> str:
    """`raw` with a leading `~` replaced by the home folder, the way git reads `core.hooksPath`."""
    resolved_home = str(user_home(home))
    if raw == "~":
        return resolved_home
    if raw.startswith(("~/", "~\\")):
        rest = raw[2:].lstrip("/\\")
        return os.path.join(resolved_home, rest) if rest else resolved_home
    return raw


def _same_dir(raw: str, directory: Path, home: Path) -> bool:
    return Path(expand_tilde(raw, home)).resolve() == directory.resolve()


def craftsman_home_dir(home: Path) -> Path:
    craftsman_home = env_var_for_home("CRAFTSMAN_HOME", home)
    return Path(craftsman_home) if craftsman_home else home / ".craftsman"


def legacy_git_hooks_bases(home: Path) -> tuple[Path, ...]:
    """Candidate base folders where Craftsman < 4.0 could have recorded `TargetKind.GIT_HOOKS`."""
    candidates = (home / ".gemini" / "config", craftsman_home_dir(home))
    unique: dict[Path, Path] = {}
    for candidate in candidates:
        unique.setdefault(candidate.resolve(), candidate)
    return tuple(unique.values())


def is_craftsman_hooks_path(hooks_path: str | None, home: Path) -> bool:
    """Whether `core.hooksPath` points to one of Craftsman 3.x's global `git-hooks` folders."""
    if not hooks_path:
        return False
    return any(_same_dir(hooks_path, base / HOOKS_DIR_NAME, home) for base in legacy_git_hooks_bases(home))


# --- git config --global --------------------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    """The environment for `git config --global` with `home` as the user's home folder."""
    env = {**os.environ, "HOME": str(home)}
    for key in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME"):
        value = env_var_for_home(key, home)
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return env


def _git_config(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "config", "--global", *args],
            env=_git_env(home),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(f"cannot run git config --global {' '.join(args)}: {exc}") from exc


def _failed(proc: subprocess.CompletedProcess[str], args: str) -> InstallError:
    return InstallError(
        f"git config --global {args} failed (exit {proc.returncode}): {proc.stderr.strip()}"
    )


def git_config_get(key: str, home: Path) -> str | None:
    """The global value of `key`, or None when it is not set."""
    proc = _git_config(home, "--get", key)
    if proc.returncode == 1:  # git's exit code for a key that is not set
        return None
    if proc.returncode != 0:
        raise _failed(proc, f"--get {key}")
    return proc.stdout.strip() or None


def git_config_set(key: str, value: str, home: Path) -> None:
    proc = _git_config(home, key, value)
    if proc.returncode != 0:
        raise _failed(proc, key)


def git_config_unset(key: str, home: Path) -> None:
    """Unset `key`, and drop its section once nothing else is left in it."""
    proc = _git_config(home, "--unset-all", key)
    if proc.returncode not in (0, 5):  # 5: the key was not set
        raise _failed(proc, f"--unset-all {key}")
    section = key.partition(".")[0]
    rest = _git_config(home, "--get-regexp", rf"^{section}\.")
    if rest.returncode == 0:
        return
    if rest.returncode != 1:
        raise _failed(rest, f"--get-regexp ^{section}.")
    removed = _git_config(home, "--remove-section", section)
    if removed.returncode not in (0, 128):  # 128: git wrote no section header to remove
        raise _failed(removed, f"--remove-section {section}")


# --- restore --------------------------------------------------------------------------------


def restore_hooks_path(target: TargetManifest, home: Path, tracker: Tracker) -> None:
    """Point `core.hooksPath` back to what it was, unless the user changed it since."""
    if target.hooks_dir is None:
        raise InstallError("the git hooks manifest entry has no hooks_dir")
    hooks_dir = Path(target.hooks_dir)
    current = git_config_get("core.hooksPath", home)
    previous = target.previous_hooks_path
    if current is None or (
        previous is not None and _same_dir(current, Path(expand_tilde(previous, home)), home)
    ):
        return
    if not _same_dir(current, hooks_dir, home):
        tracker.add(
            EventKind.WARNING,
            f"leaving git config --global core.hooksPath ({current}) untouched: it no longer "
            f"points to {hooks_dir}",
        )
        return
    if not tracker.dry_run:
        if previous is None:
            git_config_unset("core.hooksPath", home)
        else:
            git_config_set("core.hooksPath", previous, home)
    tracker.add(EventKind.RESTORED, f"git config --global core.hooksPath -> {previous or '(unset)'}")
