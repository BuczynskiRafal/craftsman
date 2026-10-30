"""Bundled skill manifests, agent templates (`{{include ...}}`), and combined rules."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, get_args

from craftsman.generated import CRAFTSMAN_GENERATED_MARKER, ExportError, FileChange, write_marked

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
INCLUDE_RE = re.compile(r"^\{\{include\s+(\S+)\s*\}\}[ \t]*$", re.MULTILINE)
MALFORMED_INCLUDE_RE = re.compile(r"^[ \t]*\{\{\s*include\b.*$", re.MULTILINE)
SIBLING_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([\w.-]+\.md)\)")
COPY_IGNORED_NAMES = frozenset({"__pycache__", ".DS_Store", "Thumbs.db"})

TemplateRole = Literal["orchestrator", "reviewer", "verifier"]
TEMPLATE_ROLES: tuple[TemplateRole, ...] = get_args(TemplateRole)


def parse_frontmatter(text: str, source: Path) -> tuple[dict[str, str], str]:
    """Split simple YAML frontmatter (scalars, folded `>-` blocks, `[a, b]` lists) from the body."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise ExportError(f"{source}: missing frontmatter block")
    fields: dict[str, str] = {}
    key: str | None = None
    for line in match.group(1).splitlines():
        if key is not None and line[:1] in (" ", "\t"):
            fields[key] = f"{fields[key]} {line.strip()}".strip()
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise ExportError(f"{source}: cannot parse frontmatter line {line!r}")
        key = name.strip()
        value = value.strip()
        fields[key] = "" if value in (">-", ">", "|", "|-") else _unquote(value)
    return fields, match.group(2)


def _unquote(value: str) -> str:
    """A YAML scalar without its enclosing quote pair; quotes inside the text are kept."""
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"')
    return value


def _required_fields(fields: dict[str, str], source: Path, *names: str) -> None:
    missing = [name for name in names if not fields.get(name)]
    if missing:
        raise ExportError(f"{source}: frontmatter needs {', '.join(f'`{n}`' for n in missing)}")


@dataclass(frozen=True)
class Skill:
    """A bundled skill: its folder, frontmatter name and description, and the full SKILL.md."""

    name: str
    description: str
    body: str
    text: str
    directory: Path


def read_skill(skill_md: Path) -> Skill:
    text = skill_md.read_text(encoding="utf-8")
    fields, body = parse_frontmatter(text, skill_md)
    _required_fields(fields, skill_md, "name", "description")
    return Skill(fields["name"], fields["description"], body.strip(), text, skill_md.parent)


def iter_skills(repo_dir: Path) -> list[Skill]:
    """Every `skills/*/SKILL.md` of the repository, by folder."""
    return [read_skill(path) for path in sorted((repo_dir / "skills").glob("*/SKILL.md"))]


def single_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def yaml_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(single_quoted(item) for item in items) + "]"


def with_inlined_siblings(skill: Skill) -> str:
    """The skill body followed by the sibling Markdown files it links to, for one-file formats."""
    appendices = [
        f"## Appendix: {name}\n\n{_strip_frontmatter((skill.directory / name).read_text(encoding='utf-8'))}"
        for name in dict.fromkeys(SIBLING_LINK_RE.findall(skill.body))
        if name != "SKILL.md" and (skill.directory / name).is_file()
    ]
    if not appendices:
        return skill.body
    return skill.body.rstrip() + "\n\n---\n\n" + "\n\n".join(appendices)


def _strip_frontmatter(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return (match.group(2) if match else text).strip()


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    description: str
    role: TemplateRole
    subagents: tuple[str, ...]
    body: str  # with includes expanded


def _parse_list(raw: str) -> tuple[str, ...]:
    inner = raw.strip().removeprefix("[").removesuffix("]")
    return tuple(item.strip().strip("\"'") for item in inner.split(",") if item.strip())


def expand_includes(body: str, repo_dir: Path, source: Path) -> str:
    """Replace `{{include <repo-relative path>}}` lines with that file's body."""

    def include(match: re.Match[str]) -> str:
        relative = PurePosixPath(match.group(1))
        # Checked lexically: an installed copy may symlink skills/ to a checkout elsewhere.
        if relative.is_absolute() or ".." in relative.parts:
            raise ExportError(f"{source}: include {match.group(1)} must be a path inside {repo_dir}")
        target = repo_dir / relative
        if not target.is_file():
            raise ExportError(f"{source}: included file not found: {target}")
        return _strip_frontmatter(target.read_text(encoding="utf-8"))

    expanded = INCLUDE_RE.sub(include, body)
    if leftover := MALFORMED_INCLUDE_RE.search(expanded):
        raise ExportError(f"{source}: malformed include directive {leftover.group(0).strip()!r}")
    return expanded


def load_agent_templates(repo_dir: Path) -> list[AgentTemplate]:
    paths = sorted((repo_dir / "agents").glob("*.md"))
    if not paths:
        raise ExportError(f"no agent templates (agents/*.md) found under {repo_dir}")
    return [_load_agent_template(path, repo_dir) for path in paths]


def _load_agent_template(path: Path, repo_dir: Path) -> AgentTemplate:
    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"), path)
    _required_fields(fields, path, "name", "description")
    raw_role = fields.get("role", "")
    role = next((item for item in TEMPLATE_ROLES if item == raw_role), None)
    if role is None:
        raise ExportError(
            f"{path}: role must be one of {', '.join(TEMPLATE_ROLES)} (got {raw_role!r})"
        )
    return AgentTemplate(
        name=fields["name"],
        description=fields["description"],
        role=role,
        subagents=_parse_list(fields.get("subagents", "")),
        body=expand_includes(body, repo_dir, path).strip(),
    )


def craftsman_agent_names(repo_dir: Path) -> tuple[str, ...]:
    return tuple(template.name for template in load_agent_templates(repo_dir))


def craftsman_skill_names(repo_dir: Path) -> tuple[str, ...]:
    return tuple(skill.name for skill in iter_skills(repo_dir))


def craftsman_entity_names(repo_dir: Path) -> frozenset[str]:
    return frozenset(
        (*craftsman_agent_names(repo_dir), *craftsman_skill_names(repo_dir), "craftsman-rules")
    )


def export_agents(
    templates: Sequence[AgentTemplate],
    out_dir: Path,
    suffix: str,
    render: Callable[[AgentTemplate], str],
) -> list[FileChange]:
    return [write_marked(out_dir / f"{t.name}{suffix}", render(t)) for t in templates]


def render_combined_rules(rules_dir: Path, with_frontmatter: bool) -> str:
    bodies = [
        _strip_frontmatter(rule_file.read_text(encoding="utf-8"))
        for rule_file in sorted(rules_dir.glob("*.md"))
    ]
    combined = "\n\n---\n\n".join(bodies) + "\n"
    if not with_frontmatter:
        return combined
    return (
        "---\napplyTo: '**'\ndescription: 'Craftsman Global Engineering Rules'\n---\n"
        f"{CRAFTSMAN_GENERATED_MARKER}\n\n{combined}"
    )
