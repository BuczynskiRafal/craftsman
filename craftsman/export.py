#!/usr/bin/env python3
"""Universal Multi-Platform Exporter for Craftsman skills and rules.

Supports exporting Craftsman skills and global engineering rules to:
1. GitHub Copilot (VS Code User Profile `--user-dir`, Agent Skills `--copilot-skills-dir` -> `~/.copilot/skills/*/SKILL.md`, or Workspace `--workspace` -> `.github/prompts/*.prompt.md` + `.github/skills/*/SKILL.md` + `.github/copilot-instructions.md`)
2. Claude Code (`--claude-dir` -> `~/.claude/commands/<skill>.md` + `~/.claude/rules/craftsman-rules.md`)
3. OpenAI Codex / Cursor / Universal Agents (`--agents-workspace <repo_path>` -> `AGENTS.md` + `.cursor/rules/craftsman.mdc`)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
CRAFTSMAN_BEGIN_MARKER = "<!-- craftsman:begin -->"
CRAFTSMAN_END_MARKER = "<!-- craftsman:end -->"


def default_repo_dir() -> Path:
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parent
    if (repo_root / "skills").is_dir():
        return repo_root
    bundled = pkg_dir / "_assets"
    if (bundled / "skills").is_dir():
        return bundled
    cfg_dir = Path.home() / ".gemini" / "config"
    if (cfg_dir / "skills").is_dir():
        return cfg_dir
    return repo_root


def _resolve_windows_appdata(home: Path | None = None) -> Path | None:
    resolved_home = home or Path.home()
    appdata = os.environ.get("APPDATA", "").strip()
    win_roaming = resolved_home / "AppData" / "Roaming"
    if appdata:
        if os.name != "nt":
            drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", appdata)
            if drive_match:
                drive = drive_match.group(1).lower()
                rest = drive_match.group(2).replace("\\", "/")
                for candidate in (
                    win_roaming,
                    Path(f"/{drive}/{rest}"),
                    Path(f"/mnt/{drive}/{rest}"),
                ):
                    if candidate.is_dir():
                        return candidate
                return win_roaming
        return Path(appdata)
    if sys.platform == "win32" or os.name == "nt" or win_roaming.is_dir():
        return win_roaming
    return None


def default_vscode_prompts_dir() -> Path:
    home = Path.home()
    win_base = _resolve_windows_appdata(home)
    if win_base is not None:
        return win_base / "Code" / "User" / "prompts"
    mac_dir = home / "Library" / "Application Support" / "Code" / "User" / "prompts"
    if sys.platform == "darwin" or mac_dir.parent.is_dir():
        return mac_dir
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    cfg_root = Path(xdg_cfg) if xdg_cfg else (home / ".config")
    return cfg_root / "Code" / "User" / "prompts"


def default_copilot_skills_dir() -> Path:
    return Path.home() / ".copilot" / "skills"


def default_claude_dir() -> Path:
    return Path.home() / ".claude"


def extract_skill_name(skill_md: str, fallback_name: str) -> str:
    match = FRONTMATTER_RE.match(skill_md)
    if not match:
        return fallback_name
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            val = line.split(":", 1)[1].strip().strip("\"'")
            if val:
                return val
    return fallback_name


def extract_description_and_body(skill_md: str, fallback_name: str) -> tuple[str, str]:
    match = FRONTMATTER_RE.match(skill_md)
    if not match:
        return f"Craftsman skill /{fallback_name}", skill_md.strip()

    fm_raw, body = match.group(1), match.group(2)
    desc_lines: list[str] = []
    in_desc = False
    for line in fm_raw.splitlines():
        if line.startswith("description:"):
            val = line.split(":", 1)[1].strip()
            if val not in (">-", "|", ">", ""):
                desc_lines.append(val.strip("\"'"))
            in_desc = True
            continue
        if in_desc:
            if line.startswith("  ") or line.startswith("\t"):
                desc_lines.append(line.strip())
            else:
                break

    description = " ".join(desc_lines).strip() or f"Craftsman skill /{fallback_name}"
    return description, body.strip()


GATE_SCRIPT_INVOCATION_RE = re.compile(
    r"(?:python3?\s+)?~/.gemini/config/scripts/craftsman-stop-gate\.py"
)


def _rewrite_antigravity_paths(body: str) -> str:
    """Rewrite Antigravity-specific script and artifact paths to portable per-worktree CLI equivalents."""
    state_expr = "$(git rev-parse --git-dir 2>/dev/null || echo .craftsman)/craftsman/craftsman_state.json"
    dir_expr = "$(git rev-parse --git-dir 2>/dev/null || echo .craftsman)/craftsman"
    rewritten = GATE_SCRIPT_INVOCATION_RE.sub("craftsman gate", body)
    rewritten = rewritten.replace(
        '"<artifactDirectoryPath>/craftsman_state.json"',
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace(
        "<artifactDirectoryPath>/craftsman_state.json",
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace(
        '"<appDataDir>/brain/<conversation-id>/craftsman_state.json"',
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace(
        "<appDataDir>/brain/<conversation-id>/craftsman_state.json",
        f'"{state_expr}"',
    )
    rewritten = rewritten.replace("<artifactDirectoryPath>", dir_expr)
    rewritten = rewritten.replace("<appDataDir>/brain/<conversation-id>", dir_expr)
    return rewritten


def adapt_body_for_copilot(body: str) -> str:
    """Append a concise Copilot Agent Mode tool-mapping note and normalize paths."""
    header_note = (
        "> **GitHub Copilot Agent Mode Note:** When running inside GitHub Copilot, map "
        "`run_command` to terminal execution (`run_in_terminal`), `view_file`/`replace_file_content` "
        "to workspace file read/edit tools, `ask_question` to asking the user directly in chat before "
        "continuing, and `invoke_subagent` to sequential or sub-task execution. On Windows PowerShell/CMD "
        "(or any shell), you may omit `--state-file` and run `craftsman gate --init --tier auto --cwd .` "
        "and `craftsman gate --complete --cwd .` directly.\n\n"
    )
    return header_note + _rewrite_antigravity_paths(body) + "\n"


def adapt_body_for_claude(body: str) -> str:
    """Append a concise Claude Code tool-mapping note and normalize paths."""
    header_note = (
        "> **Claude Code Note:** When running inside Claude Code, map `run_command` to `Bash`, "
        "`view_file`/`replace_file_content` to `Read`/`Edit`, `ask_question` to `AskUserQuestion` "
        "(or asking the user directly before pushing to `main`), and `invoke_subagent` to the "
        "`Task` subagent tool.\n\n"
    )
    return header_note + _rewrite_antigravity_paths(body) + "\n"


def render_copilot_prompt_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    safe_desc = description.replace("'", "''")
    adapted = adapt_body_for_copilot(body)
    return f"---\nagent: 'agent'\nmode: 'agent'\ndescription: '{safe_desc}'\n---\n\n{adapted}"


def render_copilot_skill_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    safe_desc = description.replace("'", "''")
    adapted = adapt_body_for_copilot(body)
    return f"---\nname: {skill_name}\ndescription: '{safe_desc}'\n---\n\n{adapted}"


def render_claude_command_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    safe_desc = description.replace('"', '\\"')
    adapted = adapt_body_for_claude(body)
    return f'---\ndescription: "{safe_desc}"\n---\n\n{adapted}'


def render_combined_rules(rules_dir: Path, with_frontmatter: bool) -> str:
    parts: list[str] = []
    if with_frontmatter:
        parts.append(
            "---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering & Git Rules'\n---\n"
        )

    for rule_file in sorted(rules_dir.glob("*.md")):
        raw_rule = rule_file.read_text(encoding="utf-8").strip()
        fm_match = FRONTMATTER_RE.match(raw_rule + "\n")
        body = fm_match.group(2).strip() if fm_match else raw_rule
        parts.append(body)
        parts.append("\n\n---\n")

    return "\n".join(parts).strip() + "\n"


def render_agents_md(repo_dir: Path) -> str:
    """Render a universal AGENTS.md block for OpenAI Codex, Cursor, Windsurf, and GitHub Copilot."""
    rules_dir = repo_dir / "rules"
    skills_dir = repo_dir / "skills"

    lines: list[str] = [
        "# AGENTS.md — Craftsman Adaptive Closed-Loop Engineering Standard",
        "",
        "This repository uses **Craftsman**, an invocation-steerable 3-Gear Hybrid Hard-Loop engineering harness.",
        "",
        "## 3-Gear Quality Loop (`Quick` vs `Standard` vs `Deep`)",
        "- **Gear 1 (`quick` / Soft-Loop):** Changes `<= 40` lines across `<= 2` files -> inline `/simplify` + `/code-review` + `/verify`.",
        "- **Gear 2 (`standard` / Hybrid Hard-Loop):** Changes `> 40` lines or `> 2` files -> spawn an isolated auditor subagent to run `/simplify` -> `/code-review` -> `/verify` on the full cumulative diff.",
        "- **Gear 3 (`deep` / Full-Audit Hard-Loop):** Triggered via `/craftsman --deep` or `ultracode` -> deep architectural reasoning + isolated auditor subagent running `/audit all`.",
        "",
        "## Available Skills",
    ]
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        raw_md = skill_path.read_text(encoding="utf-8")
        skill_name = extract_skill_name(raw_md, skill_path.parent.name)
        desc, _ = extract_description_and_body(raw_md, skill_name)
        lines.append(f"- **`/{skill_name}`**: {desc}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        _rewrite_antigravity_paths(render_combined_rules(rules_dir, with_frontmatter=False).strip())
    )
    lines.append("")
    return "\n".join(lines)


def upsert_managed_markdown(target_file: Path, inner_content: str) -> None:
    """Insert or update a `<!-- craftsman:begin -->...<!-- craftsman:end -->` block without destroying user content."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    managed_block = f"{CRAFTSMAN_BEGIN_MARKER}\n{inner_content.strip()}\n{CRAFTSMAN_END_MARKER}\n"

    if not target_file.is_file():
        target_file.write_text(managed_block, encoding="utf-8")
        return

    existing = target_file.read_text(encoding="utf-8")
    begin_idx = existing.find(CRAFTSMAN_BEGIN_MARKER)
    end_idx = existing.find(CRAFTSMAN_END_MARKER)
    if begin_idx != -1 and end_idx != -1 and begin_idx < end_idx:
        end_pos = end_idx + len(CRAFTSMAN_END_MARKER)
        if end_pos < len(existing) and existing[end_pos : end_pos + 1] == "\n":
            end_pos += 1
        updated = existing[:begin_idx] + managed_block + existing[end_pos:]
        target_file.write_text(updated, encoding="utf-8")
        return

    stripped = existing.lstrip()
    if stripped.startswith("# AGENTS.md — Craftsman") or stripped.startswith(
        "# Craftsman Global Engineering"
    ):
        target_file.write_text(managed_block, encoding="utf-8")
        return

    # Preserve existing custom project instructions and append the Craftsman block
    base = existing.rstrip()
    prefix = f"{base}\n\n" if base else ""
    target_file.write_text(prefix + managed_block, encoding="utf-8")


def _render_skill_items(
    skills_dir: Path,
    renderer: Callable[[str, str], str],
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        raw_md = skill_path.read_text(encoding="utf-8")
        skill_name = extract_skill_name(raw_md, skill_path.parent.name)
        items.append((skill_name, renderer(skill_name, raw_md)))
    return items


def _export_skill_files(
    skills_dir: Path,
    out_dir: Path,
    suffix: str,
    renderer: Callable[[str, str], str],
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered_items = _render_skill_items(skills_dir, renderer)
    for skill_name, rendered in rendered_items:
        (out_dir / f"{skill_name}{suffix}").write_text(rendered, encoding="utf-8")
    return len(rendered_items)


def export_to_copilot_skills(repo_dir: Path, skills_out_dir: Path) -> int:
    """Export native GitHub Copilot Agent Skills (<skills_out_dir>/<skill>/SKILL.md)."""
    skills_out_dir.mkdir(parents=True, exist_ok=True)
    rendered_items = _render_skill_items(repo_dir / "skills", render_copilot_skill_file)
    for skill_name, rendered in rendered_items:
        skill_dest_dir = skills_out_dir / skill_name
        skill_dest_dir.mkdir(parents=True, exist_ok=True)
        (skill_dest_dir / "SKILL.md").write_text(rendered, encoding="utf-8")
    return len(rendered_items)


def export_to_vscode_user(repo_dir: Path, target_dir: Path) -> tuple[int, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    rendered_prompts = _render_skill_items(repo_dir / "skills", render_copilot_prompt_file)
    for skill_name, rendered in rendered_prompts:
        (target_dir / f"{skill_name}.prompt.md").write_text(rendered, encoding="utf-8")

    rendered_rules = _rewrite_antigravity_paths(
        render_combined_rules(repo_dir / "rules", with_frontmatter=True)
    )
    instructions_file = target_dir / "craftsman-rules.instructions.md"
    instructions_file.write_text(rendered_rules, encoding="utf-8")

    # If target_dir is a VS Code User/prompts directory and custom profiles exist under User/profiles/<id>, populate them too
    profiles_dir = target_dir.parent / "profiles"
    if target_dir.name == "prompts" and profiles_dir.is_dir():
        for profile_dir in sorted(profiles_dir.iterdir()):
            if profile_dir.is_dir():
                prof_prompts = profile_dir / "prompts"
                prof_prompts.mkdir(parents=True, exist_ok=True)
                for skill_name, rendered in rendered_prompts:
                    (prof_prompts / f"{skill_name}.prompt.md").write_text(
                        rendered, encoding="utf-8"
                    )
                (prof_prompts / "craftsman-rules.instructions.md").write_text(
                    rendered_rules, encoding="utf-8"
                )

    return len(rendered_prompts), instructions_file


def export_to_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[int, Path]:
    github_dir = workspace_dir / ".github"
    count = _export_skill_files(
        repo_dir / "skills", github_dir / "prompts", ".prompt.md", render_copilot_prompt_file
    )
    export_to_copilot_skills(repo_dir, github_dir / "skills")
    copilot_instructions = github_dir / "copilot-instructions.md"
    rules_content = _rewrite_antigravity_paths(
        render_combined_rules(repo_dir / "rules", with_frontmatter=False)
    )
    upsert_managed_markdown(copilot_instructions, rules_content)
    return count, copilot_instructions


def export_to_claude_user(repo_dir: Path, claude_dir: Path) -> tuple[int, Path]:
    rules_out_dir = claude_dir / "rules"
    rules_out_dir.mkdir(parents=True, exist_ok=True)
    count = _export_skill_files(
        repo_dir / "skills", claude_dir / "commands", ".md", render_claude_command_file
    )
    rules_file = rules_out_dir / "craftsman-rules.md"
    rules_file.write_text(
        _rewrite_antigravity_paths(
            render_combined_rules(repo_dir / "rules", with_frontmatter=False)
        ),
        encoding="utf-8",
    )
    return count, rules_file


def export_to_agents_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[Path, Path]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agents_content = render_agents_md(repo_dir)
    agents_md_path = workspace_dir / "AGENTS.md"
    upsert_managed_markdown(agents_md_path, agents_content)

    cursor_rules_dir = workspace_dir / ".cursor" / "rules"
    cursor_rules_dir.mkdir(parents=True, exist_ok=True)
    cursor_mdc = cursor_rules_dir / "craftsman.mdc"
    cursor_header = (
        "---\n"
        "description: Craftsman Agentic Engineering & Git Workflow Rules\n"
        'globs: "*"\n'
        "alwaysApply: true\n"
        "---\n\n"
    )
    cursor_mdc.write_text(cursor_header + agents_content, encoding="utf-8")
    return agents_md_path, cursor_mdc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Craftsman suite to GitHub Copilot, Claude Code, OpenAI Codex, and Cursor."
    )
    parser.add_argument(
        "--repo-dir",
        default=str(default_repo_dir()),
        help="Path to the craftsman repository root",
    )
    parser.add_argument(
        "--user-dir",
        nargs="?",
        const="",
        help="Export to VS Code User prompts directory (defaults to OS-specific Code/User/prompts)",
    )
    parser.add_argument(
        "--copilot-skills-dir",
        nargs="?",
        const="",
        help="Export native Copilot Agent Skills (defaults to ~/.copilot/skills)",
    )
    parser.add_argument(
        "--workspace",
        help="Export to a target project workspace (.github/prompts, .github/skills, and .github/copilot-instructions.md)",
    )
    parser.add_argument(
        "--claude-dir",
        nargs="?",
        const="",
        help="Export to Claude Code directory (defaults to ~/.claude/commands and ~/.claude/rules)",
    )
    parser.add_argument(
        "--agents-workspace",
        help="Export universal AGENTS.md and .cursor/rules/craftsman.mdc into a target project workspace",
    )
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()

    if not (
        args.user_dir is not None
        or args.copilot_skills_dir is not None
        or args.workspace
        or args.claude_dir is not None
        or args.agents_workspace
    ):
        parser.error(
            "Specify at least one target: --user-dir, --copilot-skills-dir, --workspace <path>, --claude-dir, or --agents-workspace <path>"
        )

    skill_manifests = sorted((repo_dir / "skills").glob("*/SKILL.md"))
    if not skill_manifests:
        sys.stderr.write(
            f"[ERROR] No skill manifests (skills/*/SKILL.md) found under {repo_dir}\n"
        )
        return 1

    if args.user_dir is not None:
        target = Path(args.user_dir) if args.user_dir else default_vscode_prompts_dir()
        count, instr = export_to_vscode_user(repo_dir, target)
        print(f"[OK] Exported {count} Copilot prompt files (*.prompt.md) -> {target}")
        print(f"[OK] Exported global Copilot instructions -> {instr}")
        if not args.user_dir and args.copilot_skills_dir is None:
            skills_target = default_copilot_skills_dir()
            skills_count = export_to_copilot_skills(repo_dir, skills_target)
            print(f"[OK] Exported {skills_count} Copilot Agent Skills (SKILL.md) -> {skills_target}")

    if args.copilot_skills_dir is not None:
        skills_target = (
            Path(args.copilot_skills_dir)
            if args.copilot_skills_dir
            else default_copilot_skills_dir()
        )
        skills_count = export_to_copilot_skills(repo_dir, skills_target)
        print(f"[OK] Exported {skills_count} Copilot Agent Skills (SKILL.md) -> {skills_target}")

    if args.workspace:
        ws = Path(args.workspace).resolve()
        count, instr = export_to_workspace(repo_dir, ws)
        print(f"[OK] Exported {count} workspace prompt files -> {ws / '.github' / 'prompts'}")
        print(f"[OK] Exported {count} workspace Agent Skills -> {ws / '.github' / 'skills'}")
        print(f"[OK] Exported workspace Copilot instructions -> {instr}")

    if args.claude_dir is not None:
        target_claude = Path(args.claude_dir) if args.claude_dir else default_claude_dir()
        count, rules_file = export_to_claude_user(repo_dir, target_claude)
        print(f"[OK] Exported {count} Claude Code slash commands -> {target_claude / 'commands'}")
        print(f"[OK] Exported Claude Code global rules -> {rules_file}")

    if args.agents_workspace:
        ws = Path(args.agents_workspace).resolve()
        agents_md, cursor_mdc = export_to_agents_workspace(repo_dir, ws)
        print(f"[OK] Exported universal AGENTS.md (Codex/Cursor/Windsurf) -> {agents_md}")
        print(f"[OK] Exported Cursor rule -> {cursor_mdc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
