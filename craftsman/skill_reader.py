"""`craftsman skill`: read the bundled skills and their companion files."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from craftsman.assets import Skill, iter_skills
from craftsman.generated import ExportError
from craftsman.paths import default_repo_dir
from craftsman.playbooks import (
    CRAFT_REVIEW_SKILL,
    LENSES_FILENAME,
    expand_playbook_lenses,
    lens_playbooks,
)


def discover_skill_dirs(repo_dir: Path) -> dict[str, Skill]:
    """Bundled skills by name, each inside the skills folder (a symlink out of it is skipped)."""
    skills_root = (repo_dir / "skills").resolve()
    return {
        skill.name: skill
        for skill in iter_skills(repo_dir)
        if skill.directory.resolve().is_relative_to(skills_root)
    }


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
        raise ValueError(f"--file {rel_file!r} must resolve inside skill directory {skill_name!r}")
    if not resolved.is_file():
        raise FileNotFoundError(f"file {rel_file!r} not found in skill {skill_name!r}")
    return resolved


@dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    path: str


def _first_sentence(text: str) -> str:
    return text.split(". ", 1)[0].rstrip(".") + "."


def _list_skills(known: dict[str, Skill], *, as_json: bool) -> int:
    entries = [
        SkillEntry(name, _first_sentence(skill.description), str(skill.directory.resolve()))
        for name, skill in known.items()
    ]
    if as_json:
        sys.stdout.write(
            json.dumps([asdict(entry) for entry in entries], indent=2, ensure_ascii=False) + "\n"
        )
        return 0
    width = max((len(entry.name) for entry in entries), default=12)
    for entry in entries:
        print(f"{entry.name:<{width}}  {entry.description}")
    return 0


def _read_skill_show_content(
    skill_dir: Path, skill_name: str, rel_file: str | None, raw_lenses: str | None
) -> str:
    if raw_lenses is None:
        target = (
            _validate_skill_relative_file(skill_dir, rel_file, skill_name)
            if rel_file is not None
            else (skill_dir / "SKILL.md").resolve()
        )
        return target.read_text(encoding="utf-8")
    if skill_name != CRAFT_REVIEW_SKILL:
        raise ValueError(
            f"--lenses is only supported for skill {CRAFT_REVIEW_SKILL!r} (got {skill_name!r})"
        )
    if rel_file is not None:
        raise ValueError("--lenses cannot be combined with --file")
    expanded = expand_playbook_lenses(raw_lenses)
    target = _validate_skill_relative_file(skill_dir, LENSES_FILENAME, skill_name)
    return lens_playbooks(target.read_text(encoding="utf-8"), expanded)


def run_skill(args: argparse.Namespace) -> int:
    """`craftsman skill list|path|show` for the parsed command line `args`."""
    try:
        known = discover_skill_dirs((args.repo_dir or default_repo_dir()).resolve())
    except ExportError as exc:
        sys.stderr.write(f"craftsman skill: error: {exc}\n")
        return 1

    if args.subcommand == "list":
        return _list_skills(known, as_json=bool(args.json))

    skill_name: str = args.name
    if skill_name not in known:
        available = ", ".join(sorted(known)) or "none"
        sys.stderr.write(
            f"craftsman skill: error: unknown skill {skill_name!r} (available: {available})\n"
        )
        return 2

    skill_dir = known[skill_name].directory.resolve()
    if args.subcommand == "path":
        print(str(skill_dir))
        return 0

    try:
        content = _read_skill_show_content(skill_dir, skill_name, args.rel_file, args.lenses)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"craftsman skill: error: {exc}\n")
        return 2

    sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return 0
