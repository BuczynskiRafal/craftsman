#!/usr/bin/env python3
"""Export Craftsman skills and rules to GitHub Copilot (VS Code User Profile or Workspace).

Supports:
1. User-level VS Code installation (`--user-dir <Code/User/prompts>`):
   - Exports all 20 skills as `<skill>.prompt.md` (invokable via `/<skill>` in Copilot Chat)
   - Exports global rules (`git_workflow.md` + `ultra_mode.md`) as `craftsman.instructions.md`
     with `applyTo: "**"` so Copilot applies them across all repositories.
2. Workspace-level installation (`--workspace <repo_path>`):
   - Exports `.github/copilot-instructions.md`
   - Exports `.github/prompts/<skill>.prompt.md`
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
    # Escape single quotes for YAML single-quoted scalar
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


def render_copilot_prompt_file(skill_name: str, skill_md: str) -> str:
    description, body = extract_description_and_body(skill_md, skill_name)
    safe_desc = description.replace("'", "''")
    adapted = adapt_body_for_copilot(body)
    return f"---\nmode: 'agent'\ndescription: '{safe_desc}'\n---\n\n{adapted}"


def render_combined_rules(rules_dir: Path, with_frontmatter: bool) -> str:
    parts: list[str] = []
    if with_frontmatter:
        parts.append("---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering & Git Rules'\n---\n")

    for rule_file in sorted(rules_dir.glob("*.md")):
        parts.append(rule_file.read_text(encoding="utf-8").strip())
        parts.append("\n\n---\n")

    return "\n".join(parts).strip() + "\n"


def export_to_vscode_user(repo_dir: Path, target_dir: Path) -> tuple[int, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    skills_dir = repo_dir / "skills"
    rules_dir = repo_dir / "rules"

    count = 0
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_path.parent.name
        prompt_content = render_copilot_prompt_file(
            skill_name, skill_path.read_text(encoding="utf-8")
        )
        out_file = target_dir / f"{skill_name}.prompt.md"
        out_file.write_text(prompt_content, encoding="utf-8")
        count += 1

    instructions_file = target_dir / "craftsman-rules.instructions.md"
    instructions_file.write_text(
        render_combined_rules(rules_dir, with_frontmatter=True), encoding="utf-8"
    )
    return count, instructions_file


def export_to_workspace(repo_dir: Path, workspace_dir: Path) -> tuple[int, Path]:
    github_dir = workspace_dir / ".github"
    prompts_dir = github_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    skills_dir = repo_dir / "skills"
    rules_dir = repo_dir / "rules"

    count = 0
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        skill_name = skill_path.parent.name
        prompt_content = render_copilot_prompt_file(
            skill_name, skill_path.read_text(encoding="utf-8")
        )
        out_file = prompts_dir / f"{skill_name}.prompt.md"
        out_file.write_text(prompt_content, encoding="utf-8")
        count += 1

    copilot_instructions = github_dir / "copilot-instructions.md"
    copilot_instructions.write_text(
        render_combined_rules(rules_dir, with_frontmatter=False), encoding="utf-8"
    )
    return count, copilot_instructions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Craftsman suite to GitHub Copilot.")
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
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    if args.user_dir is not None:
        target = Path(args.user_dir) if args.user_dir else default_vscode_prompts_dir()
        count, instr = export_to_vscode_user(repo_dir, target)
        print(f"[OK] Exported {count} Copilot prompt files (*.prompt.md) -> {target}")
        print(f"[OK] Exported global Copilot instructions -> {instr}")

    if args.workspace:
        ws = Path(args.workspace).resolve()
        count, instr = export_to_workspace(repo_dir, ws)
        print(f"[OK] Exported {count} workspace prompt files -> {ws / '.github' / 'prompts'}")
        print(f"[OK] Exported workspace Copilot instructions -> {instr}")

    if args.user_dir is None and not args.workspace:
        parser.error("Specify --user-dir and/or --workspace <path>")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
