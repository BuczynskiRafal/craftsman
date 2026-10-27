#!/usr/bin/env python3
"""Unified CLI entrypoint for the Craftsman Agentic Engineering Framework."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from craftsman import __version__
from craftsman.diffscan import DiffScanError
from craftsman.evidence import ledger_overview
from craftsman.export import (
    CLAUDE_HOOK_EVENTS,
    FRONTMATTER_RE,
    HOOKS_FILENAME,
    _resolve_windows_appdata,
    _rewrite_antigravity_paths,
    default_claude_dir,
    default_copilot_home,
    default_copilot_skills_dir,
    default_repo_dir,
    default_vscode_prompts_dir,
    extract_description_and_body,
    extract_skill_name,
    is_craftsman_hook_command,
    resolve_craftsman_pkg_dir,
    user_home,
)
from craftsman.export import main as export_main
from craftsman.gate import (
    compute_cumulative_diff_summary,
    resolve_cli_state_file,
    resolve_effective_gear_label,
)
from craftsman.gate import main as gate_main
from craftsman.guard import main as guard_main
from craftsman.hooks import main as hook_main
from craftsman.install import (
    MANIFEST_FILENAME,
    STANDARD_PASSTHROUGH_GIT_HOOKS,
    _expand_tilde,
    git_config_get,
    install_main,
    uninstall_main,
)
from craftsman.ledger import LedgerError, ledger_path_for, load
from craftsman.review import main as review_main
from craftsman.runner import main as run_main


def _expected_skill_count(repo_dir: Path) -> int:
    plugin_file = repo_dir / "plugin.json"
    if plugin_file.is_file():
        try:
            data = json.loads(plugin_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("skills"), list):
                return len(data["skills"])
        except (OSError, json.JSONDecodeError):
            pass
    return 17


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def detect_antigravity_drift(repo_dir: Path, config_dir: Path | None = None) -> list[str]:
    """Compare SHA-256 hashes of repo skills/rules/scripts/hooks against installed ~/.gemini/config."""
    cfg = config_dir or (user_home() / ".gemini" / "config")
    has_skills = (cfg / "skills" / "craftsman" / "SKILL.md").is_file()
    has_plugin = (cfg / "plugins" / "craftsman" / "plugin.json").is_file()
    if not has_skills and not has_plugin:
        return []

    drifted: list[str] = []
    subdirs: tuple[tuple[str, str], ...] = (
        (
            ("skills", "*/SKILL.md"),
            ("rules", "*.md"),
            ("scripts", "*.py"),
            ("craftsman", "*.py"),
            ("git-hooks", "*"),
        )
        if has_skills
        else (
            ("scripts", "*.py"),
            ("craftsman", "*.py"),
            ("git-hooks", "*"),
        )
    )
    for subdir, pattern in subdirs:
        src_base = (
            resolve_craftsman_pkg_dir(repo_dir)
            if subdir == "craftsman"
            else (repo_dir / subdir)
        )
        dst_base = cfg / subdir
        if not src_base.is_dir() or not dst_base.is_dir():
            continue
        for src_file in sorted(src_base.glob(pattern)):
            if not src_file.is_file():
                continue
            if subdir == "git-hooks" and (
                src_file.name.startswith("_") or src_file.name.startswith(".")
            ):
                continue
            rel = src_file.relative_to(src_base)
            dst_file = dst_base / rel
            if not dst_file.is_file():
                drifted.append(f"{subdir}/{rel} (missing in {cfg})")
            elif _sha256_file(src_file) != _sha256_file(dst_file):
                drifted.append(f"{subdir}/{rel}")
        if subdir == "git-hooks":
            passthrough_src = src_base / "_passthrough.sh"
            passthrough_sha = _sha256_file(passthrough_src) if passthrough_src.is_file() else None
            if passthrough_sha is not None:
                for hook_name in STANDARD_PASSTHROUGH_GIT_HOOKS:
                    dst_hook = dst_base / hook_name
                    if not dst_hook.is_file():
                        drifted.append(f"git-hooks/{hook_name} (missing in {cfg})")
                    elif _sha256_file(dst_hook) != passthrough_sha:
                        drifted.append(f"git-hooks/{hook_name}")

    src_hooks_json = repo_dir / "hooks.json"
    dst_hooks_json = cfg / "hooks.json"
    if has_skills and src_hooks_json.is_file():
        if not dst_hooks_json.is_file():
            drifted.append(f"hooks.json (missing in {cfg})")
        else:
            try:
                src_data = json.loads(src_hooks_json.read_text(encoding="utf-8"))
                dst_data = json.loads(dst_hooks_json.read_text(encoding="utf-8"))
                if not isinstance(src_data, dict) or not isinstance(dst_data, dict):
                    drifted.append("hooks.json")
                else:
                    for key, val in src_data.items():
                        dst_entry = dst_data.get(key)
                        if not isinstance(dst_entry, dict) or not isinstance(val, dict):
                            drifted.append("hooks.json")
                            break
                        if not isinstance(dst_entry.get("enabled"), bool) or {
                            k: v for k, v in dst_entry.items() if k != "enabled"
                        } != {k: v for k, v in val.items() if k != "enabled"}:
                            drifted.append("hooks.json")
                            break
            except (OSError, json.JSONDecodeError):
                drifted.append("hooks.json")

    return drifted


def validate_skill_manifests(repo_dir: Path) -> tuple[int, list[str]]:
    """Validate all SKILL.md files under repo_dir/skills for frontmatter integrity."""
    skills_dir = repo_dir / "skills"
    if not skills_dir.is_dir():
        return 0, [f"Missing skills directory: {skills_dir}"]

    errors: list[str] = []
    skill_files = sorted(skills_dir.glob("*/SKILL.md"))
    for skill_path in skill_files:
        folder_name = skill_path.parent.name
        try:
            content = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{folder_name}: unreadable SKILL.md ({exc})")
            continue

        match = FRONTMATTER_RE.match(content)
        if not match:
            errors.append(f"{folder_name}: missing YAML frontmatter block")
            continue

        fm_raw, body = match.group(1), match.group(2).strip()
        fm_lines = fm_raw.splitlines()
        has_name = any(line.startswith("name:") for line in fm_lines)
        has_desc = any(line.startswith("description:") for line in fm_lines)
        if not has_name:
            errors.append(f"{folder_name}: frontmatter missing 'name:' field")
        if not has_desc:
            errors.append(f"{folder_name}: frontmatter missing 'description:' field")
        if len(body) < 100:
            errors.append(
                f"{folder_name}: SKILL.md body is unexpectedly short ({len(body)} chars)"
            )

    return len(skill_files), errors


AGENT_NAMES = ("craftsman", "craftsman-reviewer", "craftsman-verifier")
CLAUDE_AGENT_NAMES = ("craftsman-reviewer", "craftsman-verifier")
SELF_CHECK_MODULES = ("ledger", "diffscan")


def _command_executables(command: str, powershell: bool = False) -> list[str]:
    """The interpreter and script a hook command runs, i.e. the words before `hook`."""
    try:
        if powershell:  # PowerShell quoting: backslashes are path separators, not escapes
            words = [word.strip("\"'") for word in shlex.split(command.removeprefix("& "), posix=False)]
        else:
            words = shlex.split(command)
    except ValueError:
        return [command]
    head = words[: words.index("hook")] if "hook" in words else words[:1]
    return [word for word in head if not word.startswith("-") and word != "craftsman.cli"]


def _is_runnable(executable: str) -> bool:
    if os.path.isabs(executable):
        return Path(executable).is_file()
    return shutil.which(executable) is not None


def inspect_copilot_hooks(hooks_file: Path) -> dict[str, Any]:
    """Whether a Copilot hooks file exists and every program its commands run is present."""
    report: dict[str, Any] = {"path": str(hooks_file), "exists": hooks_file.is_file(), "missing": []}
    if not report["exists"]:
        report["runnable"] = None
        return report
    try:
        data = json.loads(hooks_file.read_text(encoding="utf-8"))
        groups = data["hooks"].values()
        # Check the command this OS will run: Copilot picks `powershell` on Windows, `bash` elsewhere.
        key = "powershell" if os.name == "nt" else "bash"
        commands = [
            str(entry.get(key) or entry.get("command") or "") for group in groups for entry in group
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report["runnable"] = False
        report["missing"] = [f"unreadable hooks file: {exc}"]
        return report
    commands = [command for command in commands if command]
    missing = sorted(
        {
            exe
            for command in commands
            for exe in _command_executables(command, powershell=key == "powershell")
            if not _is_runnable(exe)
        }
    )
    report["runnable"] = bool(commands) and not missing
    report["missing"] = missing or ([] if commands else [f"no `{key}` hook commands"])
    return report


def _agents_installed(directory: Path, names: tuple[str, ...], suffix: str) -> bool:
    return all((directory / f"{name}{suffix}").is_file() for name in names)


def _skills_report(skills_dir: Path) -> dict[str, Any]:
    review = skills_dir / "code-review"
    lenses: bool | None = None
    if (review / "SKILL.md").is_file():
        lenses = (review / "lenses.md").is_file()
    return {
        "path": str(skills_dir),
        "installed": (skills_dir / "craftsman" / "SKILL.md").is_file(),
        "code_review_lenses": lenses,
    }


def _claude_hooks_merged(settings_file: Path) -> bool:
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        return all(
            any(
                is_craftsman_hook_command(entry.get("command"))
                for group in hooks.get(event, [])
                for entry in group.get("hooks", [])
            )
            for event in CLAUDE_HOOK_EVENTS
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def _has_passthrough_helpers(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "run_craftsman_guard" in text and "chain_git_hook" in text


def _hook_can_load_helpers(hooks_dir: Path, hook_file: Path) -> bool:
    candidates: list[Path] = [hooks_dir / "_passthrough.sh"]
    if hook_file.is_symlink():
        try:
            raw_target = os.readlink(hook_file)
        except OSError:
            raw_target = ""
        if raw_target:
            target_path = (
                Path(raw_target)
                if os.path.isabs(raw_target)
                else (hook_file.parent / raw_target)
            )
            candidates.append(target_path.parent / "_passthrough.sh")
    candidates.append(hooks_dir / "applypatch-msg")
    return any(_has_passthrough_helpers(cand) for cand in candidates)


def inspect_git_hooks(
    hooks_path: str | None, repo_dir: Path | None = None, home: Path | None = None
) -> list[str]:
    """Check whether a Craftsman `core.hooksPath` directory has working `_passthrough.sh` helpers."""
    if not hooks_path:
        return []
    hooks_dir = Path(_expand_tilde(hooks_path, home)).expanduser()
    if not hooks_dir.is_dir():
        return []

    is_craftsman_dir = (
        (hooks_dir / ".python_cmd").is_file()
        or (hooks_dir.parent / MANIFEST_FILENAME).is_file()
        or (hooks_dir / ".previous_hooks_path").is_file()
    )
    broken_hooks: list[str] = []
    for hook_name in ("commit-msg", "pre-push"):
        hook_file = hooks_dir / hook_name
        if hook_file.is_file():
            try:
                hook_text = hook_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                hook_text = ""
            if "run_craftsman_guard" in hook_text and not _hook_can_load_helpers(
                hooks_dir, hook_file
            ):
                broken_hooks.append(hook_name)
        elif is_craftsman_dir and hook_file.is_symlink():
            broken_hooks.append(hook_name)

    if not broken_hooks:
        return []
    _ = repo_dir
    return [
        f"git hooks in {hooks_dir} ({', '.join(broken_hooks)}) cannot find _passthrough.sh helpers "
        "(missing _passthrough.sh next to symlink target and no run_craftsman_guard in applypatch-msg); "
        "run './install.sh --git-hooks' (or 'craftsman install --git-hooks') to repair"
    ]


def _self_check() -> dict[str, bool]:
    results: dict[str, bool] = {}
    for module in SELF_CHECK_MODULES:
        try:
            importlib.import_module(f"craftsman.{module}")
            results[module] = True
        except ImportError:
            results[module] = False
    return results


def inspect_platforms(workspace: Path | None) -> dict[str, Any]:
    """Copilot and Claude Code integration status; `problems` lists what breaks the gate."""
    copilot_home = default_copilot_home()
    claude_dir = default_claude_dir()
    skills = [_skills_report(default_copilot_skills_dir())]
    if workspace is not None:
        skills.append(_skills_report(workspace / ".github" / "skills"))
    user_hooks = inspect_copilot_hooks(copilot_home / "hooks" / HOOKS_FILENAME)
    report: dict[str, Any] = {
        "copilot": {
            "user_hooks": user_hooks,
            "agents": {
                "user": _agents_installed(copilot_home / "agents", AGENT_NAMES, ".agent.md"),
                "workspace": (
                    _agents_installed(workspace / ".github" / "agents", AGENT_NAMES, ".agent.md")
                    if workspace is not None
                    else None
                ),
            },
            "skills": skills,
        },
        "claude": {
            "agents": _agents_installed(claude_dir / "agents", CLAUDE_AGENT_NAMES, ".md"),
            "hooks_merged": _claude_hooks_merged(claude_dir / "settings.json"),
        },
        "cli_self_check": _self_check(),
    }
    problems = [
        f"{entry['path']}/code-review has no lenses.md next to SKILL.md; re-run `craftsman export`"
        for entry in skills
        if entry["code_review_lenses"] is False
    ]
    if user_hooks["runnable"] is False:
        problems.append(
            f"{user_hooks['path']} runs missing programs ({', '.join(user_hooks['missing'])}); "
            "re-run `craftsman export --user-dir`"
        )
    problems.extend(
        f"craftsman.{module} cannot be imported"
        for module, ok in report["cli_self_check"].items()
        if not ok
    )
    report["problems"] = problems
    return report


def run_doctor(repo_dir: Path, as_json: bool = False, workspace: Path | None = None) -> int:
    """Inspect repository integrity, Git configuration, drift, and installed agent platforms."""
    skill_count, skill_errors = validate_skill_manifests(repo_dir)
    expected_skills = _expected_skill_count(repo_dir)
    rule_files = sorted((repo_dir / "rules").glob("*.md")) if (repo_dir / "rules").is_dir() else []

    required_scripts = (
        "git-guard.py",
        "craftsman-stop-gate.py",
        "copilot-export.py",
        "craftsman-cli.py",
    )
    scripts_dir = repo_dir / "scripts"
    missing_scripts = [
        name for name in required_scripts if not (scripts_dir / name).is_file()
    ]

    git_name = git_config_get("user.name")
    git_email = git_config_get("user.email")
    hooks_path = git_config_get("core.hooksPath")

    home = user_home()
    vscode_prompts = default_vscode_prompts_dir()
    copilot_skills = default_copilot_skills_dir()
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    linux_cfg_root = Path(xdg_cfg) if xdg_cfg else (home / ".config")
    win_appdata_root = _resolve_windows_appdata(home) or (home / "AppData" / "Roaming")
    installed_targets = {
        "antigravity": (
            (home / ".gemini" / "config" / "skills" / "craftsman" / "SKILL.md").is_file()
            or (home / ".gemini" / "config" / "plugins" / "craftsman" / "plugin.json").is_file()
        ),
        "claude_code": (home / ".claude" / "commands" / "craftsman.md").is_file(),
        "github_copilot": (
            (vscode_prompts / "craftsman.prompt.md").is_file()
            or (copilot_skills / "craftsman" / "SKILL.md").is_file()
        ),
        "github_copilot_macos": (
            home
            / "Library"
            / "Application Support"
            / "Code"
            / "User"
            / "prompts"
            / "craftsman.prompt.md"
        ).is_file(),
        "github_copilot_linux": (
            linux_cfg_root / "Code" / "User" / "prompts" / "craftsman.prompt.md"
        ).is_file(),
        "github_copilot_windows": (
            win_appdata_root / "Code" / "User" / "prompts" / "craftsman.prompt.md"
        ).is_file(),
        "global_git_hooks": bool(hooks_path and Path(hooks_path).expanduser().is_dir()),
    }

    config_drift = detect_antigravity_drift(repo_dir)
    platforms = inspect_platforms(workspace)
    platforms["problems"].extend(inspect_git_hooks(hooks_path, repo_dir, home))

    healthy = (
        sys.version_info >= (3, 10)
        and skill_count >= expected_skills
        and not skill_errors
        and len(rule_files) >= 2
        and not missing_scripts
        and not config_drift
        and not platforms["problems"]
    )

    report: dict[str, Any] = {
        "version": __version__,
        "healthy": healthy,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "repo_dir": str(repo_dir),
        "skills_count": skill_count,
        "expected_skills_count": expected_skills,
        "skill_errors": skill_errors,
        "rules_count": len(rule_files),
        "missing_scripts": missing_scripts,
        "config_drift": config_drift,
        "git_config": {
            "user.name": git_name,
            "user.email": git_email,
            "core.hooksPath": hooks_path,
        },
        "installed_targets": installed_targets,
        **platforms,
    }

    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return 0 if healthy else 1

    status_label = "HEALTHY" if healthy else "ISSUES DETECTED"
    print(f"Craftsman Doctor v{__version__} [{status_label}]")
    print(f"  Python runtime : {report['python_version']}")
    print(f"  Repository     : {repo_dir}")
    print(f"  Skills audited : {skill_count}/{expected_skills} ({len(skill_errors)} errors)")
    print(f"  Rules audited  : {len(rule_files)}")
    print(
        f"  Core scripts   : {len(required_scripts) - len(missing_scripts)}/{len(required_scripts)} present"
    )
    print(f"  Git identity   : {git_name or 'unset'} <{git_email or 'unset'}>")
    print(f"  Git hooksPath  : {hooks_path or 'unset'}")
    if config_drift:
        sync_hint = (
            "run './install.sh --symlink --git-hooks' to sync"
            if (repo_dir / "install.sh").is_file()
            else "reinstall or re-run 'craftsman export' to sync"
        )
        print(
            f"  Config drift   : {len(config_drift)} file(s) differ between repo and ~/.gemini/config ({sync_hint})"
        )
    copilot = platforms["copilot"]
    hooks_state = {None: "not installed", True: "ok", False: "BROKEN"}[copilot["user_hooks"]["runnable"]]
    print(f"  Copilot hooks  : {hooks_state} ({copilot['user_hooks']['path']})")
    print(
        f"  Copilot agents : user {_yes_no(copilot['agents']['user'])}"
        + (f", workspace {_yes_no(copilot['agents']['workspace'])}" if workspace else "")
    )
    claude = platforms["claude"]
    claude_hooks_label = (
        "installed"
        if claude["hooks_merged"]
        else "not installed (run ./install.sh --claude)"
    )
    print(
        f"  Claude Code    : agents {_yes_no(claude['agents'])}, "
        f"hooks {claude_hooks_label}"
    )
    for problem in platforms["problems"]:
        print(f"  [ERROR] {problem}", file=sys.stderr)
    for err in skill_errors:
        print(f"  [ERROR] {err}", file=sys.stderr)
    for ms in missing_scripts:
        print(f"  [ERROR] Missing script: scripts/{ms}", file=sys.stderr)
    return 0 if healthy else 1


def _yes_no(value: bool) -> str:
    return "installed" if value else "missing"


def _empty_diff_summary() -> dict[str, int]:
    return {"changed_files": 0, "added_lines": 0, "deleted_lines": 0, "total_changed_lines": 0}


def run_status(state_file: Path | None, cwd: str | None, as_json: bool = False) -> int:
    """Display the current 3-Gear state machine and cumulative diff metrics."""
    if state_file is None or "$(git rev-parse" in str(state_file):
        candidate = resolve_cli_state_file(None, cwd)
        state_file = candidate if candidate.is_file() else None

    state: dict[str, Any] = {}
    if state_file and state_file.is_file():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"[craftsman] Warning: failed to read state file {state_file}: {exc}\n"
            )

    effective_cwd = cwd or (
        state.get("cwd") if isinstance(state.get("cwd"), str) else os.getcwd()
    )
    base_sha = state.get("base_sha") if isinstance(state.get("base_sha"), str) else None
    diff_summary = compute_cumulative_diff_summary(effective_cwd, base_sha) or _empty_diff_summary()

    tier = str(state.get("tier") or "auto").strip().lower()
    effective_state = dict(state, cwd=effective_cwd)
    effective_gear = resolve_effective_gear_label(effective_state, effective_cwd)

    ledger_info: dict[str, Any] | None = None
    ledger_error: str | None = None
    try:
        ledger_info = ledger_overview(load(ledger_path_for(str(effective_cwd))))
    except (DiffScanError, LedgerError) as exc:
        ledger_error = str(exc)

    payload: dict[str, Any] = {
        "state_file": str(state_file) if state_file else None,
        "active": bool(state.get("active", False)),
        "configured_tier": tier,
        "effective_gear": effective_gear,
        "stage": state.get("stage", "idle"),
        "base_sha": base_sha,
        "cwd": effective_cwd,
        "diff": diff_summary,
        "ledger": ledger_info,
        "ledger_error": ledger_error,
        "subagent_verified": bool(state.get("subagent_verified", False)),
    }

    if as_json:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        return 0

    print(f"Craftsman Status (v{__version__})")
    print(f"  Active         : {payload['active']}")
    print(f"  Effective Gear : {effective_gear}")
    print(f"  Stage          : {payload['stage']}")
    print(f"  Base SHA       : {base_sha or 'HEAD'}")
    print(
        f"  Cumulative Diff: {diff_summary['changed_files']} files, "
        f"+{diff_summary['added_lines']}/-{diff_summary['deleted_lines']} lines"
    )
    print(f"  Evidence       : {_describe_ledger(ledger_info, ledger_error)}")
    return 0


def _describe_ledger(info: dict[str, Any] | None, error: str | None) -> str:
    if info is None:
        return f"unavailable ({error})"
    if not info["rounds"]:
        return "no review round yet (`craftsman review prepare`)"
    coverage = info["coverage"]
    verdicts = info["verdicts"]
    runs = ", ".join(
        f"{run['id']} {run['kind']} exit {run['exit_code']}" for run in info["latest_runs"]
    )
    return (
        f"{info['rounds']} round(s), tier {info['tier']}, "
        f"coverage {coverage['covered']}/{coverage['total']}; findings {info['findings']} "
        f"(proven {verdicts['proven']}, probable {verdicts['probable']}, "
        f"question {verdicts['question']}, dismissed {verdicts['dismissed']}, "
        f"no verdict {verdicts['none']}, "
        f"resolved {info['resolved']}); latest runs: {runs or 'none'}"
    )


def _discover_skill_dirs(repo_dir: Path) -> dict[str, Path]:
    skills_root = (repo_dir / "skills").resolve()
    if not skills_root.is_dir():
        return {}
    result: dict[str, Path] = {}
    for skill_md in sorted(skills_root.glob("*/SKILL.md")):
        skill_dir = skill_md.parent.resolve()
        if skill_dir.is_relative_to(skills_root):
            result[skill_md.parent.name] = skill_dir
    return result


def _validate_skill_relative_file(skill_dir: Path, rel_file: str, skill_name: str) -> Path:
    if not rel_file or not rel_file.strip():
        raise ValueError(f"--file for skill {skill_name!r} must not be empty")
    rel_path = Path(rel_file)
    if (
        rel_path.is_absolute()
        or rel_file.startswith(("/", "\\"))
        or ".." in rel_path.parts
        or (len(rel_path.parts) > 0 and ":" in rel_path.parts[0])
    ):
        raise ValueError(
            f"--file {rel_file!r} must be a relative path inside skill {skill_name!r} (no '..' or absolute paths)"
        )
    cur = skill_dir
    for part in rel_path.parts:
        cur = cur / part
        if cur.is_symlink():
            resolved_link = cur.resolve()
            if not resolved_link.is_relative_to(skill_dir):
                raise ValueError(
                    f"--file {rel_file!r} escapes skill directory {skill_name!r} via symlink"
                )
    resolved = (skill_dir / rel_path).resolve()
    if resolved == skill_dir or not resolved.is_relative_to(skill_dir):
        raise ValueError(
            f"--file {rel_file!r} must resolve inside skill directory {skill_name!r}"
        )
    if not resolved.is_file():
        raise FileNotFoundError(f"file {rel_file!r} not found in skill {skill_name!r}")
    return resolved


def skill_main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo-dir",
        type=Path,
        default=default_repo_dir(),
        help="Path to the Craftsman repository or bundled assets root.",
    )
    parser = argparse.ArgumentParser(
        prog="craftsman skill",
        description="Read bundled Craftsman skills and companion files without searching the disk.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    show_p = sub.add_parser(
        "show",
        parents=[common],
        help="Print a bundled SKILL.md or companion file (such as lenses.md) to stdout.",
    )
    show_p.add_argument("name", help="Skill name (e.g. craftsman, code-review, simplify)")
    show_p.add_argument(
        "--file",
        dest="rel_file",
        help="Relative file inside the skill directory (e.g. lenses.md)",
    )

    list_p = sub.add_parser(
        "list",
        parents=[common],
        help="List bundled skill names and their one-sentence descriptions.",
    )
    list_p.add_argument("--json", action="store_true", help="Output skill list as JSON.")

    path_p = sub.add_parser(
        "path",
        parents=[common],
        help="Print the resolved directory path of a bundled skill.",
    )
    path_p.add_argument("name", help="Skill name (e.g. craftsman, code-review)")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    repo_dir = Path(args.repo_dir).resolve()
    known = _discover_skill_dirs(repo_dir)

    if args.subcommand == "list":
        entries: list[dict[str, str]] = []
        for folder_name, skill_dir in known.items():
            raw = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            name = extract_skill_name(raw, folder_name)
            desc, _ = extract_description_and_body(raw, name)
            first_sentence = desc.split(". ", 1)[0].rstrip(".") + "." if desc else ""
            entries.append(
                {
                    "name": folder_name,
                    "description": first_sentence,
                    "path": str(skill_dir),
                }
            )
        if args.json:
            sys.stdout.write(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")
            return 0
        width = max((len(e["name"]) for e in entries), default=12)
        for entry in entries:
            print(f"{entry['name']:<{width}}  {entry['description']}")
        return 0

    skill_name: str = args.name
    if skill_name not in known:
        available = ", ".join(sorted(known)) or "none"
        sys.stderr.write(
            f"craftsman skill: error: unknown skill {skill_name!r} (available: {available})\n"
        )
        return 2

    skill_dir = known[skill_name]
    if args.subcommand == "path":
        print(str(skill_dir))
        return 0

    rel_file: str | None = args.rel_file
    try:
        target = (
            _validate_skill_relative_file(skill_dir, rel_file, skill_name)
            if rel_file is not None
            else (skill_dir / "SKILL.md").resolve()
        )
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"craftsman skill: error: {exc}\n")
        return 2

    content = _rewrite_antigravity_paths(target.read_text(encoding="utf-8"))
    sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return 0


def _tolerate_unencodable_output() -> None:
    """Print `?` for characters the console cannot encode (legacy Windows code pages) instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _tolerate_unencodable_output()
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="craftsman",
        description="Craftsman: Universal Agentic Engineering & Quality Gate CLI.",
    )
    parser.add_argument("--version", action="version", version=f"craftsman {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doc_parser = subparsers.add_parser(
        "doctor",
        help="Audit skill manifests, core scripts, Git config, drift, and installed agent targets.",
    )
    doc_parser.add_argument(
        "--repo-dir",
        type=Path,
        default=default_repo_dir(),
        help="Path to the Craftsman repository root.",
    )
    doc_parser.add_argument(
        "--json", action="store_true", help="Output diagnostic report as JSON."
    )
    doc_parser.add_argument(
        "--workspace", type=Path, help="Also check the Copilot agents and skills of this project."
    )

    stat_parser = subparsers.add_parser(
        "status", help="Inspect the active 3-Gear state machine and cumulative git diff."
    )
    stat_parser.add_argument("--state-file", type=Path, help="Path to craftsman_state.json.")
    stat_parser.add_argument(
        "--cwd", type=str, help="Target repository directory for diff calculation."
    )
    stat_parser.add_argument("--json", action="store_true", help="Output status report as JSON.")

    subparsers.add_parser(
        "install",
        help="Install Craftsman skills, rules, agents, hooks, and git guards across platforms.",
    )
    subparsers.add_parser(
        "uninstall",
        help="Uninstall Craftsman files recorded in install manifests and restore global hooksPath.",
    )
    subparsers.add_parser(
        "export", help="Export skills and rules to Claude Code, GitHub Copilot, Codex, and Cursor."
    )
    subparsers.add_parser(
        "gate", help="Invoke the 3-Gear Stop Gate state manager or lifecycle hook."
    )
    subparsers.add_parser(
        "guard", help="Invoke the Git Guard PreToolUse or native Git hook engine."
    )
    subparsers.add_parser(
        "hook", help="Lifecycle hook for VS Code Copilot, the Copilot CLI and Claude Code."
    )
    subparsers.add_parser(
        "review", help="Prepare review rounds and record findings, coverage and verdicts."
    )
    subparsers.add_parser(
        "run", help="Run a command and record its exit code in the evidence ledger."
    )
    subparsers.add_parser(
        "skill",
        help="Read bundled Craftsman skills and companion files without searching the disk.",
    )

    if not args_list:
        parser.print_help()
        return 0

    cmd = args_list[0]
    if cmd == "install":
        return install_main(args_list[1:])
    if cmd == "uninstall":
        return uninstall_main(args_list[1:])
    if cmd == "export":
        return export_main(args_list[1:])
    if cmd == "gate":
        return gate_main(args_list[1:])
    if cmd == "guard":
        return guard_main(args_list[1:])
    if cmd == "hook":
        return hook_main(args_list[1:])
    if cmd == "review":
        return review_main(args_list[1:])
    if cmd == "run":
        return run_main(args_list[1:])
    if cmd == "skill":
        return skill_main(args_list[1:])

    parsed = parser.parse_args(args_list)
    if parsed.command == "doctor":
        workspace = parsed.workspace.resolve() if parsed.workspace else None
        return run_doctor(parsed.repo_dir.resolve(), as_json=bool(parsed.json), workspace=workspace)
    if parsed.command == "status":
        return run_status(parsed.state_file, parsed.cwd, as_json=bool(parsed.json))

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
