"""Global git hooks: the hook scripts, `git config --global core.hooksPath`, and restoring it.

Craftsman's hooks run the git guard and then the hooks Craftsman found before it: the
`core.hooksPath` it replaced (recorded in the manifest and written to `.previous_hooks_path` for the
scripts) and the repository's own `.git/hooks`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from craftsman.export import env_var_for_home, user_home
from craftsman.files import write_text_atomic
from craftsman.manifest import InstallError, TargetKind, TargetManifest, load_manifest
from craftsman.staging import (
    EventKind,
    InstallMode,
    TargetSession,
    Tracker,
    install_runtime,
    install_tree,
)

GIT_TIMEOUT_S = 5
HOOKS_DIR_NAME = "git-hooks"
PASSTHROUGH_TEMPLATE_NAME = "_passthrough.sh"
PYTHON_CMD_FILE = ".python_cmd"
PREVIOUS_HOOKS_FILE = ".previous_hooks_path"
# The hooks that run the git guard; every other standard hook only passes through.
GUARDED_GIT_HOOKS = ("commit-msg", "pre-push")
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


# --- install --------------------------------------------------------------------------------


def craftsman_home_dir(home: Path) -> Path:
    craftsman_home = env_var_for_home("CRAFTSMAN_HOME", home)
    return Path(craftsman_home) if craftsman_home else home / ".craftsman"


def git_hooks_base_dir(home: Path, *, with_antigravity: bool) -> Path:
    """Where the git hooks live: where a manifest records them, else next to the Antigravity
    install that has the same scripts, else in `$CRAFTSMAN_HOME` (default `~/.craftsman`)."""
    candidates = (home / ".gemini" / "config", craftsman_home_dir(home))
    recorded = []
    for base in dict.fromkeys(candidates):
        manifest = load_manifest(base)
        if manifest is not None and TargetKind.GIT_HOOKS in manifest.targets:
            recorded.append(base)
    if len(recorded) > 1:
        raise InstallError(
            f"git hooks are recorded in both {recorded[0]} and {recorded[1]}; "
            "run `craftsman uninstall --git-hooks` first"
        )
    if recorded:
        return recorded[0]
    return candidates[0] if with_antigravity else candidates[1]


def install_git_hooks_target(
    repo_dir: Path, base_dir: Path, home: Path, mode: InstallMode, tracker: Tracker
) -> None:
    session = TargetSession.begin(base_dir, TargetKind.GIT_HOOKS, home, tracker)
    hooks_dir = base_dir / HOOKS_DIR_NAME
    install_runtime(session, repo_dir, mode)
    session.make_dirs(hooks_dir)
    _install_hook_scripts(session, repo_dir / HOOKS_DIR_NAME, hooks_dir, mode)
    _write_runtime_file(session, hooks_dir / PYTHON_CMD_FILE, sys.executable)
    previous = _previous_hooks_path(session, hooks_dir, home)
    if previous is None:
        session.clear(hooks_dir / PREVIOUS_HOOKS_FILE)
    else:
        _write_runtime_file(session, hooks_dir / PREVIOUS_HOOKS_FILE, expand_tilde(previous, home))
    # Recorded first: a hooksPath set without a manifest would lose the value to restore.
    session.finish(previous_hooks_path=previous, hooks_dir=str(hooks_dir))
    if not tracker.dry_run:
        git_config_set("core.hooksPath", str(hooks_dir), home)
    tracker.add(EventKind.NOTE, f"git config --global core.hooksPath -> {hooks_dir}")


def _install_hook_scripts(
    session: TargetSession, source_dir: Path, hooks_dir: Path, mode: InstallMode
) -> None:
    """The guarded hooks, `_passthrough.sh` they source, and every other standard hook as a copy
    (or symlink) of `_passthrough.sh`."""
    template = source_dir / PASSTHROUGH_TEMPLATE_NAME
    sources = [template, *(source_dir / name for name in GUARDED_GIT_HOOKS)]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise InstallError(f"missing git hook sources: {', '.join(missing)}")
    if source_dir.resolve() == hooks_dir.resolve():
        return  # the repository's own hooks folder is the installed one
    link_mode = InstallMode.COPY if mode is InstallMode.COPY else InstallMode.SYMLINK
    for source in sources:
        install_tree(session, source, hooks_dir / source.name, link_mode, executable=True)
    for name in STANDARD_PASSTHROUGH_GIT_HOOKS:
        install_tree(session, template, hooks_dir / name, link_mode, executable=True)


def _write_runtime_file(session: TargetSession, path: Path, line: str) -> None:
    session.stage(path)
    if not session.dry_run:
        write_text_atomic(path, f"{line}\n")


def _previous_hooks_path(session: TargetSession, hooks_dir: Path, home: Path) -> str | None:
    """The `core.hooksPath` to chain to and to restore on uninstall."""
    current = git_config_get("core.hooksPath", home)
    if current is None:
        return None
    if _same_dir(current, hooks_dir, home):  # a reinstall keeps what the first install found
        if session.previous is None:
            raise InstallError(
                f"core.hooksPath already points to {hooks_dir}, but no install manifest records "
                "the value it replaced; set core.hooksPath back to it (or unset it) and install again"
            )
        return session.previous.previous_hooks_path
    current_dir = Path(expand_tilde(current, home))
    if not current_dir.is_absolute():  # each repository's own folder: no Craftsman install
        return current
    other_base = current_dir.parent
    other = load_manifest(other_base)
    if other is not None and TargetKind.GIT_HOOKS in other.targets:
        raise InstallError(
            f"core.hooksPath points to the Craftsman git hooks installed in {other_base}; "
            "run `craftsman uninstall --git-hooks` before installing them here"
        )
    return current


# --- uninstall ------------------------------------------------------------------------------


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
