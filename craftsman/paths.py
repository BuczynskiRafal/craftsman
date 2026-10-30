"""Filesystem locations for user homes, platform directories, and bundled Craftsman assets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from craftsman.generated import ExportError


def user_home(home: Path | None = None) -> Path:
    if home is not None:
        return home
    env_home = os.environ.get("HOME", "").strip() or os.environ.get("USERPROFILE", "").strip()
    if env_home:
        return Path(env_home)
    try:
        return Path.home()
    except RuntimeError as exc:
        raise ExportError(f"cannot determine the home directory: {exc}") from exc


def env_var_for_home(env_key: str, home: Path | None = None) -> str:
    """`env_key` from the environment, or "" when it points outside an explicitly given `home`."""
    val = os.environ.get(env_key, "").strip()
    if not val:
        return ""
    abs_val = Path(os.path.abspath(val))
    abs_home = Path(os.path.abspath(user_home(home)))
    if abs_val == abs_home or abs_home in abs_val.parents:
        return val
    if abs_home == Path(os.path.abspath(user_home(None))):
        return val
    return ""


def _has_assets(directory: Path) -> bool:
    return (directory / "skills").is_dir() and (directory / "agents").is_dir()


def resolve_craftsman_pkg_dir(repo_dir: Path) -> Path:
    """The `craftsman` package that ships with the assets in `repo_dir`."""
    candidate = repo_dir / "craftsman"
    if (candidate / "__init__.py").is_file():
        return candidate
    if repo_dir.name == "_assets" and (repo_dir.parent / "__init__.py").is_file():
        return repo_dir.parent
    raise ExportError(f"no craftsman package next to the assets in {repo_dir}")


def default_repo_dir() -> Path:
    """This checkout, or the assets bundled into the installed package."""
    pkg_dir = Path(__file__).resolve().parent
    for candidate in (pkg_dir.parent, pkg_dir / "_assets"):
        if _has_assets(candidate):
            return candidate
    raise ExportError(f"no Craftsman assets (skills/, agents/) next to {pkg_dir}; pass --repo-dir")


def default_vscode_user_dir(home: Path | None = None) -> Path:
    """VS Code's `Code/User` folder in this operating system's layout."""
    if sys.platform == "win32":
        appdata = env_var_for_home("APPDATA", home)
        if not appdata:
            raise ExportError("APPDATA is not set; cannot locate the VS Code user folder")
        return Path(appdata) / "Code" / "User"
    if sys.platform == "darwin":
        return user_home(home) / "Library" / "Application Support" / "Code" / "User"
    xdg_config = env_var_for_home("XDG_CONFIG_HOME", home)
    return (Path(xdg_config) if xdg_config else user_home(home) / ".config") / "Code" / "User"


def default_vscode_prompts_dir(home: Path | None = None) -> Path:
    return default_vscode_user_dir(home) / "prompts"


def default_copilot_home(home: Path | None = None) -> Path:
    return user_home(home) / ".copilot"


def default_copilot_skills_dir(home: Path | None = None) -> Path:
    return default_copilot_home(home) / "skills"


def default_claude_dir(home: Path | None = None) -> Path:
    return user_home(home) / ".claude"


def vscode_prompt_dirs(prompts_dir: Path) -> list[Path]:
    """The prompts folder plus, for a VS Code `User/prompts` folder, those of its profiles."""
    dirs = [prompts_dir]
    profiles_dir = prompts_dir.parent / "profiles"
    if prompts_dir.name == "prompts" and profiles_dir.is_dir():
        dirs.extend(
            profile / "prompts" for profile in sorted(profiles_dir.iterdir()) if profile.is_dir()
        )
    return dirs
