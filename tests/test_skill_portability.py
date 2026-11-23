"""Tests for Agent Skills portability, frontmatter compliance, and synced lens references."""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from pathlib import Path

from craftsman.assets import SIBLING_LINK_RE, iter_skills
from craftsman.playbooks import LENSES_FILENAME, lens_playbooks

from tests.support import REPO_ROOT, SKILL_COUNT

EXPECTED_SKILL_COUNT = 13
MAX_SKILL_NAME_LENGTH = 64
MAX_SKILL_DESCRIPTION = 200
MAX_COMPATIBILITY_LENGTH = 500
MAX_BODY_LINES = 500

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)

ALLOWED_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
EXPECTED_METADATA: dict[str, str] = {
    "author": "BuczynskiRafal",
    "repository": "https://github.com/BuczynskiRafal/craftsman",
}
EXPECTED_LENS_REFERENCES: dict[tuple[str, str], str] = {
    ("audit-security", "references/trust-lens.md"): "trust",
    ("audit-errors", "references/failure-lens.md"): "failure",
    ("audit-tests", "references/tests-lens.md"): "tests",
    ("audit-types", "references/model-lens.md"): "model",
    ("audit-comments", "references/craft-lens.md"): "craft",
    ("simplify", "references/craft-lens.md"): "craft",
}
RUN_FALLBACK_SKILLS = (
    "audit",
    "audit-tests",
    "feature-dev",
    "modernize",
    "simplify",
    "verify",
)


@dataclass(frozen=True)
class ParsedFrontmatter:
    fields: dict[str, str | dict[str, str]]
    body: str


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"')
    return value


def _consume_block_scalar(lines: list[str], start: int, style: str) -> tuple[str, int]:
    collected: list[str] = []
    idx = start
    while idx < len(lines):
        line = lines[idx]
        if line == "":
            collected.append("")
            idx += 1
            continue
        if not line.startswith((" ", "\t")):
            break
        collected.append(line.strip())
        idx += 1
    while collected and collected[-1] == "":
        collected.pop()
    joined = " ".join(part for part in collected if part) if style.startswith(">") else "\n".join(collected)
    return joined, idx


def _consume_mapping(lines: list[str], start: int, source: Path, parent_key: str) -> tuple[dict[str, str], int]:
    mapping: dict[str, str] = {}
    idx = start
    while idx < len(lines):
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            idx += 1
            continue
        if not line.startswith((" ", "\t")):
            break
        subkey, sep, subval = line.strip().partition(":")
        if not sep or not subkey.strip():
            raise ValueError(f"{source}: malformed nested line under {parent_key!r}: {line!r}")
        cleaned_key = subkey.strip()
        cleaned_val = _unquote(subval.strip())
        if not cleaned_val:
            raise ValueError(f"{source}: nested key {parent_key}.{cleaned_key} must have a string value")
        if cleaned_key in mapping:
            raise ValueError(f"{source}: duplicate key {parent_key}.{cleaned_key}")
        mapping[cleaned_key] = cleaned_val
        idx += 1
    if not mapping:
        raise ValueError(f"{source}: mapping {parent_key!r} has no entries")
    return mapping, idx


def parse_spec_frontmatter(text: str, source: Path) -> ParsedFrontmatter:
    """Parse strict Agent Skills YAML frontmatter into string fields and flat string maps."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    match = FRONTMATTER_RE.match(normalized)
    if match is None:
        raise ValueError(f"{source}: missing or malformed YAML frontmatter block")
    lines = match.group(1).splitlines()
    fields: dict[str, str | dict[str, str]] = {}
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            idx += 1
            continue
        if line.startswith((" ", "\t")):
            raise ValueError(f"{source}: unexpected indentation at {line!r}")
        raw_key, sep, raw_value = line.partition(":")
        if not sep or not raw_key.strip():
            raise ValueError(f"{source}: invalid frontmatter line {line!r}")
        key = raw_key.strip()
        if key in fields:
            raise ValueError(f"{source}: duplicate frontmatter key {key!r}")
        value = raw_value.strip()
        if value in (">-", ">", "|", "|-"):
            fields[key], idx = _consume_block_scalar(lines, idx + 1, value)
        elif value == "":
            fields[key], idx = _consume_mapping(lines, idx + 1, source, key)
        else:
            fields[key] = _unquote(value)
            idx += 1
    return ParsedFrontmatter(fields=fields, body=match.group(2))


class SkillPortabilityTests(unittest.TestCase):
    def test_all_skills_satisfy_agent_skills_spec(self) -> None:
        skill_paths = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(skill_paths), EXPECTED_SKILL_COUNT)
        self.assertEqual(len(skill_paths), SKILL_COUNT)

        for skill_path in skill_paths:
            folder = skill_path.parent.name
            with self.subTest(skill=folder):
                raw = skill_path.read_bytes()
                self.assertNotIn(b"\r\n", raw, f"{skill_path} must use LF line endings")
                parsed = parse_spec_frontmatter(raw.decode("utf-8"), skill_path)
                self.assertLessEqual(set(parsed.fields), ALLOWED_FRONTMATTER_KEYS)

                name = parsed.fields.get("name")
                self.assertIsInstance(name, str)
                assert isinstance(name, str)
                self.assertEqual(name, folder)
                self.assertLessEqual(len(name), MAX_SKILL_NAME_LENGTH)
                self.assertRegex(name, SKILL_NAME_RE)

                description = parsed.fields.get("description")
                self.assertIsInstance(description, str)
                assert isinstance(description, str)
                self.assertTrue(description.strip())
                self.assertLessEqual(len(description), MAX_SKILL_DESCRIPTION)

                self.assertEqual(parsed.fields.get("license"), "MIT")

                compatibility = parsed.fields.get("compatibility")
                self.assertIsInstance(compatibility, str)
                assert isinstance(compatibility, str)
                self.assertTrue(compatibility.strip())
                self.assertLessEqual(len(compatibility), MAX_COMPATIBILITY_LENGTH)

                metadata = parsed.fields.get("metadata")
                self.assertEqual(metadata, EXPECTED_METADATA)

                body_lines = parsed.body.strip().splitlines()
                self.assertGreater(len(body_lines), 0)
                self.assertLessEqual(len(body_lines), MAX_BODY_LINES)
                self.assertEqual(
                    SIBLING_LINK_RE.findall(parsed.body),
                    [],
                    f"{folder} must reference companion files in backticks, not sibling links",
                )

    def test_craftsman_assets_iter_skills_parses_all_skills(self) -> None:
        skills = iter_skills(REPO_ROOT)
        self.assertEqual(len(skills), EXPECTED_SKILL_COUNT)

    def test_synced_lens_references_match_lenses_md_byte_for_byte(self) -> None:
        lenses_md = (REPO_ROOT / "skills" / "craft-review" / LENSES_FILENAME).read_text(encoding="utf-8")
        actual_refs = {
            (path.parent.parent.name, f"references/{path.name}")
            for path in sorted((REPO_ROOT / "skills").glob("*/references/*.md"))
        }
        self.assertEqual(actual_refs, set(EXPECTED_LENS_REFERENCES))

        for (skill_name, rel_path), lens_id in EXPECTED_LENS_REFERENCES.items():
            with self.subTest(skill=skill_name, ref=rel_path):
                ref_path = REPO_ROOT / "skills" / skill_name / rel_path
                expected = lens_playbooks(lenses_md, [lens_id]).encode("utf-8")
                self.assertEqual(ref_path.read_bytes(), expected)

                skill_text = (REPO_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn(f"`{rel_path}`", skill_text)

    def test_standalone_fallbacks_present(self) -> None:
        for skill_name in RUN_FALLBACK_SKILLS:
            with self.subTest(skill=skill_name):
                text = (REPO_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn("ungated", text.lower())

        craft_review = (REPO_ROOT / "skills" / "craft-review" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## Standalone mode", craft_review)
        self.assertIn("`lenses.md`", craft_review)
        self.assertIn("UNGATED", craft_review)

        scope_md = (REPO_ROOT / "skills" / "craftsman" / "scope.md").read_text(encoding="utf-8").strip()
        craftsman_skill = (REPO_ROOT / "skills" / "craftsman" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(scope_md, craftsman_skill)


if __name__ == "__main__":
    unittest.main()
