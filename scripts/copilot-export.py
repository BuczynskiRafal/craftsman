#!/usr/bin/env python3
"""Universal Multi-Platform Exporter for Craftsman skills and rules.

Supports exporting the 17 core Craftsman skills and global engineering rules to:
1. GitHub Copilot (VS Code User Profile `--user-dir` or Workspace `--workspace` -> `.github/prompts/*.prompt.md` + `.github/copilot-instructions.md`)
2. Claude Code (`--claude-dir` -> `~/.claude/commands/<skill>.md` + `~/.claude/rules/craftsman-rules.md`)
3. OpenAI Codex / Cursor / Universal Agents (`--agents-workspace <repo_path>` -> `AGENTS.md` + `.cursor/rules/craftsman.mdc`)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def default_vscode_prompts_dir() -> Path:
    home = Path.home()
    mac_dir = home / "Library" / "Application Support" / "Code" / "User" / "prompts"
    if sys.platform == "darwin" or mac_dir.parent.is_dir():
        return mac_dir
    return home / ".config" / "Code" / "User" / "prompts"


def default_claude_dir() -> Path:
    return Path.home() / ".claude"


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


def adapt_body_for_copilot(body: str) -> str:
    """Append a concise Copilot Agent Mode tool-mapping note while keeping full instructions intact."""
    header_note = (
        "> **GitHub Copilot Agent Mode Note:** When running inside GitHub Copilot, map "
        "`run_command` to terminal execution (`run_in_terminal`), `view_file`/`replace_file_content` "
        "to workspace file read/edit tools, `ask_question` to asking the user directly in chat before "
        "continuing, and `invoke_subagent` to sequential or sub-task execution.\n\n"
    )
    return header_note + body + "\n"


def adapt_body_for_claude(body: str) -> str:
    """Append a concise Claude Code tool-mapping note while keeping full instructions intact."""
    header_note = (
        "> **Claude Code Note:** When running inside Claude Code, map `run_command` to `Bash`, "
        "`view_file`/`replace_file_content` to `Read`/`Edit`, `ask_question` to `AskUserQuestion` "
        "(or asking the user directly before pushing to `main`), and `invoke_subagent` to the "
        "`Task` subagent tool.\n\n"
    )
    return header_note + body + "\n"


def render_copilot_prompt_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    safe_desc = description.replace("'", "''")
    adapted = adapt_body_for_copilot(body)
    return f"---\nmode: 'agent'\ndescription: '{safe_desc}'\n---\n\n{adapted}"


def render_claude_command_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    safe_desc = description.replace('"', '\\"')
    adapted = adapt_body_for_claude(body)
    return f'---\ndescription: "{safe_desc}"\n---\n\n{adapted}'


def render_combined_rules(rules_dir: Path, with_frontmatter: bool) -> str:
    parts: list[str] = []
    if with_frontmatter:
        parts.append("---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering & Git Rules'\n---\n")

    for rule_file in sorted(rules_dir.glob("*.md")):
        parts.append(rule_file.read_text(encoding="utf-8").strip())
        parts.append("\n\n---\n")

    return "\n".join(parts).strip() + "\n"


def render_agents_md(repo_dir: Path) -> str:
    """Render a universal AGENTS.md file for OpenAI Codex, Cursor, Windsurf, and GitHub Copilot."""
    rules_dir = repo_dir / "rules"
    skills_dir = repo_dir / "skills"

    lines: list[str] = [
        "# AGENTS.md — Craftsman Agentic Engineering Standard",
        "",
        "This repository follows the **Craftsman** 3-Gear Hybrid Quality Loop (`Twórca ≠ Recenzent`).",
        "",
        "## 3-Gear Quality Loop (`Quick` vs `Standard` vs `Deep`)",
        "- **Gear 1 (`quick` / Soft-Loop):** Changes `<= 40` lines across `<= 2` files -> inline `/simplify` + `/verify`.",
        "- **Gear 2 (`standard` / Hybrid Hard-Loop):** Changes `> 40` lines or `> 2` files -> spawn an independent reviewer subagent to run `/simplify` -> `/code-review` -> `/verify` on the full cumulative diff.",
        "- **Gear 3 (`deep` / Full-Audit Hard-Loop):** Triggered via `/craftsman --deep` or `ultracode` -> deep architectural reasoning + independent reviewer subagent running `/audit all`.",
        "",
        "## Available Skills",
    ]
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_path.parent.name
        desc, _ = extract_description_and_body(skill_path.read_text(encoding="utf-8"), skill_name)
        lines.append(f"- **`/{skill_name}`**: {desc}")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(render_combined_rules(rules_dir, with_frontmatter=False).strip())
    lines.append("")
    return "\n".join(lines)


def _export_skill_files(
    skills_dir: Path,
    out_dir: Path,
    suffix: str,
    renderer: callable,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_path.parent.name
        rendered = renderer(skill_name, skill_path.read_text(encoding="utf-8"))
        (out_dir / f"{skill_name}{suffix}").write_text(rendered, encoding="utf-8")
        count += 1
    return count


def export_to_vscode_user(repo_dir: Path, target_dir: Path) -> tuple[int, Path]:
    count = _export_skill_files(
        repo_dir / "skills", target_dir, ".prompt.md", render_copilot_prompt_file
    )
    instructions_file = target_dir / "craftsman-rules.instructions.md"
    instructions_file.write_text(
        render_combined_rules(repo_dir / "rules", with_frontmatter=True), encoding="utf-8"
    )
    return count, instructions_file


def export_to_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[int, Path]:
    github_dir = workspace_dir / ".github"
    count = _export_skill_files(
        repo_dir / "skills", github_dir / "prompts", ".prompt.md", render_copilot_prompt_file
    )
    copilot_instructions = github_dir / "copilot-instructions.md"
    copilot_instructions.write_text(
        render_combined_rules(repo_dir / "rules", with_frontmatter=False), encoding="utf-8"
    )
    return count, copilot_instructions


def export_to_claude_user(repo_dir: Path, claude_dir: Path) -> tuple[int, Path]:
    rules_out_dir = claude_dir / "rules"
    rules_out_dir.mkdir(parents=True, exist_ok=True)
    count = _export_skill_files(
        repo_dir / "skills", claude_dir / "commands", ".md", render_claude_command_file
    )
    rules_file = rules_out_dir / "craftsman-rules.md"
    rules_file.write_text(
        render_combined_rules(repo_dir / "rules", with_frontmatter=False), encoding="utf-8"
    )
    return count, rules_file


def export_to_agents_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[Path, Path]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    agents_content = render_agents_md(repo_dir)
    agents_md_path = workspace_dir / "AGENTS.md"
    agents_md_path.write_text(agents_content, encoding="utf-8")

    cursor_rules_dir = workspace_dir / ".cursor" / "rules"
    cursor_rules_dir.mkdir(parents=True, exist_ok=True)
    cursor_mdc = cursor_rules_dir / "craftsman.mdc"
    cursor_header = (
        "---\n"
        "description: Craftsman Agentic Engineering & Git Workflow Rules\n"
        "globs: *\n"
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
        default=str(Path(__file__).resolve().parent.parent),
        help="Path to the craftsman repository root",
    )
    parser.add_argument(
        "--user-dir",
        nargs="?",
        const="",
        help="Export to VS Code User prompts directory (defaults to OS-specific Code/User/prompts)",
    )
    parser.add_argument(
        "--workspace",
        help="Export to a target project workspace (.github/prompts and .github/copilot-instructions.md)",
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
    any_target = False

    if args.user_dir is not None:
        any_target = True
        target = Path(args.user_dir) if args.user_dir else default_vscode_prompts_dir()
        count, instr = export_to_vscode_user(repo_dir, target)
        print(f"[OK] Exported {count} Copilot prompt files (*.prompt.md) -> {target}")
        print(f"[OK] Exported global Copilot instructions -> {instr}")

    if args.workspace:
        any_target = True
        ws = Path(args.workspace).resolve()
        count, instr = export_to_workspace(repo_dir, ws)
        print(f"[OK] Exported {count} workspace prompt files -> {ws / '.github' / 'prompts'}")
        print(f"[OK] Exported workspace Copilot instructions -> {instr}")

    if args.claude_dir is not None:
        any_target = True
        target_claude = Path(args.claude_dir) if args.claude_dir else default_claude_dir()
        count, rules_file = export_to_claude_user(repo_dir, target_claude)
        print(f"[OK] Exported {count} Claude Code slash commands -> {target_claude / 'commands'}")
        print(f"[OK] Exported Claude Code global rules -> {rules_file}")

    if args.agents_workspace:
        any_target = True
        ws = Path(args.agents_workspace).resolve()
        agents_md, cursor_mdc = export_to_agents_workspace(repo_dir, ws)
        print(f"[OK] Exported universal AGENTS.md (Codex/Cursor/Windsurf) -> {agents_md}")
        print(f"[OK] Exported Cursor rule -> {cursor_mdc}")

    if not any_target:
        parser.error(
            "Specify at least one target: --user-dir, --workspace <path>, --claude-dir, or --agents-workspace <path>"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
