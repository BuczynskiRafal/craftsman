"""Universal AGENTS.md and Cursor `.cursor/rules/craftsman.mdc` exporter and installer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from craftsman.assets import iter_skills, render_combined_rules
from craftsman.generated import (
    CRAFTSMAN_GENERATED_MARKER,
    ExportError,
    FileChange,
    is_craftsman_generated_file,
    upsert_managed_markdown,
    write_generated,
    write_marked,
)
from craftsman.lenses import render_reviewer_groups_markdown
from craftsman.manifest import TargetKind
from craftsman.staging import TargetSession, Tracker, upsert_markdown

REVIEWER_GROUPS_PLACEHOLDER = "{{reviewer_groups}}"
AGENTS_MD_TEMPLATE = Path("templates") / "AGENTS.md"
CURSOR_RULE_HEADER = (
    "---\n"
    "description: Craftsman Agentic Engineering Rules\n"
    'globs: "*"\n'
    "alwaysApply: true\n"
    "---\n\n"
    f"{CRAFTSMAN_GENERATED_MARKER}\n\n"
)


def render_agents_md(repo_dir: Path) -> str:
    """The universal AGENTS.md block for OpenAI Codex, Cursor, Windsurf and GitHub Copilot."""
    template_path = repo_dir / AGENTS_MD_TEMPLATE
    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExportError(f"cannot read the AGENTS.md template {template_path}: {exc}") from exc
    if REVIEWER_GROUPS_PLACEHOLDER not in template:
        raise ExportError(f"{template_path} has no {REVIEWER_GROUPS_PLACEHOLDER} placeholder")
    groups = "\n".join(f"  {line}" for line in render_reviewer_groups_markdown().splitlines())
    skills = "\n".join(f"- **`/{skill.name}`**: {skill.description}" for skill in iter_skills(repo_dir))
    rules = render_combined_rules(repo_dir / "rules", with_frontmatter=False)
    loop = template.replace(REVIEWER_GROUPS_PLACEHOLDER, groups).strip()
    return f"{loop}\n{skills}\n\n---\n\n{rules}"


@dataclass(frozen=True)
class AgentsWorkspaceExport:
    agents_md: FileChange
    cursor_rule: FileChange


def export_to_agents_workspace(repo_dir: Path, workspace_dir: Path) -> AgentsWorkspaceExport:
    agents_content = render_agents_md(repo_dir)
    return AgentsWorkspaceExport(
        agents_md=upsert_managed_markdown(
            workspace_dir / "AGENTS.md", agents_content, within=workspace_dir
        ),
        cursor_rule=write_marked(
            workspace_dir / ".cursor" / "rules" / "craftsman.mdc", CURSOR_RULE_HEADER + agents_content
        ),
    )


def install_agents_workspace_target(
    repo_dir: Path, workspace: Path, home: Path, tracker: Tracker
) -> None:
    session = TargetSession.begin(workspace, TargetKind.AGENTS_WORKSPACE, home, tracker, workspace)
    cursor_rule = workspace / ".cursor" / "rules" / "craftsman.mdc"
    session.make_dirs(cursor_rule.parent)
    session.stage(cursor_rule)
    content = render_agents_md(repo_dir)
    if not tracker.dry_run:
        write_generated(cursor_rule, CURSOR_RULE_HEADER + content, owned=is_craftsman_generated_file)
    upsert_markdown(session, workspace / "AGENTS.md", content)
    session.finish()
