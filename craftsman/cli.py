#!/usr/bin/env python3
"""Unified CLI entrypoint for the Craftsman Agentic Engineering Framework."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from craftsman import __version__

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def default_repo_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _git_config_get(key: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0:
            val = proc.stdout.strip()
            return val if val else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


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
            errors.append(f"{folder_name}: SKILL.md body is unexpectedly short ({len(body)} chars)")

    return len(skill_files), errors


def run_doctor(repo_dir: Path, as_json: bool = False) -> int:
    """Inspect repository integrity, Git configuration, and installed agent platforms."""
    skill_count, skill_errors = validate_skill_manifests(repo_dir)
    rule_files = sorted((repo_dir / "rules").glob("*.md")) if (repo_dir / "rules").is_dir() else []

    required_scripts = ("git-guard.py", "craftsman-stop-gate.py", "copilot-export.py")
    missing_scripts = [
        name for name in required_scripts if not (repo_dir / "scripts" / name).is_file()
    ]

    git_name = _git_config_get("user.name")
    git_email = _git_config_get("user.email")
    hooks_path = _git_config_get("core.hooksPath")

    home = Path.home()
    installed_targets = {
        "antigravity": (home / ".gemini" / "config" / "skills" / "craftsman" / "SKILL.md").is_file(),
        "claude_code": (home / ".claude" / "commands" / "craftsman.md").is_file(),
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
            home / ".config" / "Code" / "User" / "prompts" / "craftsman.prompt.md"
        ).is_file(),
        "global_git_hooks": bool(hooks_path and Path(hooks_path).expanduser().is_dir()),
    }

    healthy = (
        sys.version_info >= (3, 10)
        and skill_count >= 17
        and not skill_errors
        and len(rule_files) >= 2
        and not missing_scripts
    )

    report: dict[str, Any] = {
        "version": __version__,
        "healthy": healthy,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "repo_dir": str(repo_dir),
        "skills_count": skill_count,
        "skill_errors": skill_errors,
        "rules_count": len(rule_files),
        "missing_scripts": missing_scripts,
        "git_config": {
            "user.name": git_name,
            "user.email": git_email,
            "core.hooksPath": hooks_path,
        },
        "installed_targets": installed_targets,
    }

    if as_json:
        sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        return 0 if healthy else 1

    status_label = "HEALTHY" if healthy else "ISSUES DETECTED"
    print(f"Craftsman Doctor v{__version__} [{status_label}]")
    print(f"  Python runtime : {report['python_version']}")
    print(f"  Repository     : {repo_dir}")
    print(f"  Skills audited : {skill_count} ({len(skill_errors)} errors)")
    print(f"  Rules audited  : {len(rule_files)}")
    print(f"  Core scripts   : {len(required_scripts) - len(missing_scripts)}/{len(required_scripts)} present")
    print(f"  Git identity   : {git_name or 'unset'} <{git_email or 'unset'}>")
    print(f"  Git hooksPath  : {hooks_path or 'unset'}")
    for err in skill_errors:
        print(f"  [ERROR] {err}", file=sys.stderr)
    for ms in missing_scripts:
        print(f"  [ERROR] Missing script: scripts/{ms}", file=sys.stderr)
    return 0 if healthy else 1


def _empty_diff_summary() -> dict[str, int]:
    return {"changed_files": 0, "added_lines": 0, "deleted_lines": 0, "total_changed_lines": 0}


def _compute_diff_summary(cwd: str | None, base_sha: str | None) -> dict[str, int]:
    if not cwd or not os.path.isdir(cwd):
        return _empty_diff_summary()
    ref = base_sha if base_sha else "HEAD"
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", ref],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if proc.returncode != 0:
            return _empty_diff_summary()
        files_count = 0
        added_total = 0
        deleted_total = 0
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files_count += 1
            added_total += int(parts[0]) if parts[0].isdigit() else 0
            deleted_total += int(parts[1]) if parts[1].isdigit() else 0

        untracked_proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if untracked_proc.returncode == 0:
            cwd_path = Path(cwd)
            for rel_path in untracked_proc.stdout.splitlines():
                rel_path = rel_path.strip()
                if not rel_path:
                    continue
                files_count += 1
                fpath = cwd_path / rel_path
                if fpath.is_file():
                    try:
                        with fpath.open("rb") as f:
                            added_total += sum(1 for _ in f)
                    except OSError:
                        pass

        return {
            "changed_files": files_count,
            "added_lines": added_total,
            "deleted_lines": deleted_total,
            "total_changed_lines": added_total + deleted_total,
        }
    except (OSError, subprocess.SubprocessError):
        return _empty_diff_summary()


def run_status(state_file: Path | None, cwd: str | None, as_json: bool = False) -> int:
    """Display the current 3-Gear state machine and cumulative diff metrics."""
    state: dict[str, Any] = {}
    if state_file and state_file.is_file():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"[craftsman] Warning: failed to read state file {state_file}: {exc}\n")
            state = {}

    effective_cwd = cwd or (state.get("cwd") if isinstance(state.get("cwd"), str) else os.getcwd())
    base_sha = state.get("base_sha") if isinstance(state.get("base_sha"), str) else None
    diff_summary = _compute_diff_summary(effective_cwd, base_sha)

    tier = str(state.get("tier") or "auto").strip().lower()
    mode = str(state.get("mode") or "craftsman").strip().lower()
    if mode == "ultracode" or tier == "deep":
        effective_gear = "deep (Gear 3: Full-Audit Hard-Loop)"
    elif tier == "quick":
        effective_gear = "quick (Gear 1: Soft-Loop)"
    elif tier == "auto":
        files_n = diff_summary["changed_files"]
        lines_n = diff_summary["total_changed_lines"]
        effective_gear = (
            "quick (Gear 1: Soft-Loop)"
            if (1 <= files_n <= 2 and lines_n <= 40)
            else "standard (Gear 2: Hybrid Hard-Loop)"
        )
    else:
        effective_gear = "standard (Gear 2: Hybrid Hard-Loop)"

    raw_gates = state.get("gates")
    gates: dict[str, Any] = (
        raw_gates
        if isinstance(raw_gates, dict)
        else {"simplified": False, "reviewed": False, "verified": False}
    )

    payload: dict[str, Any] = {
        "state_file": str(state_file) if state_file else None,
        "active": bool(state.get("active", False)),
        "configured_tier": tier,
        "effective_gear": effective_gear,
        "stage": state.get("stage", "idle"),
        "base_sha": base_sha,
        "cwd": effective_cwd,
        "diff": diff_summary,
        "gates": gates,
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
    print(
        f"  Quality Gates  : simplified={gates.get('simplified')} | "
        f"reviewed={gates.get('reviewed')} | verified={gates.get('verified')} | "
        f"subagent_verified={payload['subagent_verified']}"
    )
    return 0


def _delegate_script(script_name: str, extra_args: list[str]) -> int:
    script_path = default_repo_dir() / "scripts" / script_name
    proc = subprocess.run([sys.executable, str(script_path), *extra_args], check=False)
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="craftsman",
        description="Craftsman: Universal Agentic Engineering & Quality Gate CLI.",
    )
    parser.add_argument("--version", action="version", version=f"craftsman {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    doc_parser = subparsers.add_parser(
        "doctor", help="Audit skill manifests, core scripts, Git config, and installed agent targets."
    )
    doc_parser.add_argument(
        "--repo-dir",
        type=Path,
        default=default_repo_dir(),
        help="Path to the Craftsman repository root.",
    )
    doc_parser.add_argument("--json", action="store_true", help="Output diagnostic report as JSON.")

    stat_parser = subparsers.add_parser(
        "status", help="Inspect the active 3-Gear state machine and cumulative git diff."
    )
    stat_parser.add_argument("--state-file", type=Path, help="Path to craftsman_state.json.")
    stat_parser.add_argument("--cwd", type=str, help="Target repository directory for diff calculation.")
    stat_parser.add_argument("--json", action="store_true", help="Output status report as JSON.")

    subparsers.add_parser(
        "export", help="Export skills and rules to Claude Code, GitHub Copilot, Codex, and Cursor."
    )
    subparsers.add_parser("gate", help="Invoke the 3-Gear Stop Gate state manager or lifecycle hook.")
    subparsers.add_parser("guard", help="Invoke the Git Guard PreToolUse or native Git hook engine.")

    if not args_list:
        parser.print_help()
        return 0

    cmd = args_list[0]
    if cmd == "export":
        return _delegate_script("copilot-export.py", args_list[1:])
    if cmd == "gate":
        return _delegate_script("craftsman-stop-gate.py", args_list[1:])
    if cmd == "guard":
        return _delegate_script("git-guard.py", args_list[1:])

    parsed = parser.parse_args(args_list)
    if parsed.command == "doctor":
        return run_doctor(parsed.repo_dir.resolve(), as_json=bool(parsed.json))
    if parsed.command == "status":
        return run_status(parsed.state_file, parsed.cwd, as_json=bool(parsed.json))

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
